"""Autonomous scheduling: finite plans, durable replay and bounded parallel I/O."""
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock
from concurrent.futures import ThreadPoolExecutor

import pytest

from sales_manager.research.providers import ProviderError, QuotaError, SearchResult
from sales_manager.research.service import Paused, ResearchService
from sales_manager.research.safe_http import FetchResult, SafeFetcher
from sales_manager.storage import Store


@pytest.fixture
def research(tmp_path):
    store = Store(tmp_path / 'jobs.sqlite3')
    service = ResearchService(store, SimpleNamespace(get=lambda name: ''))
    yield service
    store.close()


def query(provider, text='кофе'):
    return dict(phase='search', provider=provider, query=text, country='Россия', market=True)


def test_failed_source_is_attempted_once_while_healthy_plan_drains(research):
    broken = Mock(search=Mock(side_effect=QuotaError('Бесплатная квота исчерпана')))
    healthy = Mock(search=Mock(return_value=[]))
    tasks = [query(source, text) for text in ('кофе', 'чай', 'посуда') for source in ('rss2', 'gdelt2')]
    result = research._run('market', None, ['Россия'], tasks,
                           {'rss2': broken, 'gdelt2': healthy}, lambda _: None, None)
    assert result['status'] == 'complete' and result['pending'] == 0
    assert broken.search.call_count == 1 and healthy.search.call_count == 3
    assert result['count'] == 0 and result['reason'] == 'sources_exhausted'
    assert research.store.one('PRAGMA integrity_check')['integrity_check'] == 'ok'


def test_cached_response_replayed_after_crash_before_task_commit(research, monkeypatch):
    provider = Mock(search=Mock(return_value=[SearchResult('https://report.example/', 'Рынок кофе')]))
    enqueue = research._enqueue
    crashed = False
    def crash(run_id, payload):
        nonlocal crashed
        if payload['phase'] == 'market_page' and not crashed:
            crashed = True
            raise RuntimeError('Simulated crash before enqueue')
        return enqueue(run_id, payload)
    monkeypatch.setattr(research, '_enqueue', crash)
    with pytest.raises(RuntimeError):
        research._run('market', None, ['Россия'], [query('gdelt')], {'gdelt': provider}, lambda _: None, None)
    run = research.store.one('SELECT id,status FROM job_runs')
    assert run['status'] == 'failed'
    monkeypatch.setattr(research, '_enqueue', enqueue)
    monkeypatch.setattr(research, '_market_evidence', lambda *args: None)
    resumed = research._run('market', None, ['Россия'], [], {'gdelt': provider}, lambda _: None, None)
    assert resumed['run_id'] == run['id'] and resumed['status'] == 'complete'
    calls = [call.args[0] for call in provider.search.call_args_list]
    assert calls.count('кофе') == 1 and len(calls) == len(set(calls))


def test_cached_empty_query_is_reused_across_new_research_runs(research):
    provider = Mock(search=Mock(return_value=[]))
    for _ in range(2):
        result = research._run('market', None, ['Россия'], [query('gdelt')], {'gdelt': provider}, lambda _: None, None)
        assert result['status'] == 'complete'
    calls = [call.args[0] for call in provider.search.call_args_list]
    assert calls.count('кофе') == 1 and len(calls) == len(set(calls))


def test_underrepresented_categories_receive_one_unique_followup_sweep(research, monkeypatch):
    provider = Mock(search=Mock(return_value=[]))
    monkeypatch.setattr(research, '_providers', lambda: {'firecrawl': provider})
    result = research.research_market(['Россия'])
    tasks = [json.loads(row['payload']) for row in research.store.query('SELECT payload FROM job_queue')]
    base = [task for task in tasks if not task.get('followup')]
    extra = [task for task in tasks if task.get('followup')]
    assert base and extra and result['pending'] == 0
    assert len(extra) == len(base)
    assert len({task['query'] for task in tasks}) == len(tasks)
    assert provider.search.call_count == len(tasks)


def test_company_plan_uses_every_web_source_even_when_first_returns_results(research, monkeypatch):
    product = research.store.ensure_product('кофе')
    first = Mock(search=Mock(return_value=[SearchResult('https://one.example/', country='Россия')]))
    second = Mock(search=Mock(return_value=[SearchResult('https://two.example/', country='Россия')]))
    monkeypatch.setattr(research, '_providers', lambda: {'exa': first, 'firecrawl': second})
    monkeypatch.setattr(research, '_company', lambda *args: None)
    result = research.find_companies(product['id'], ['Россия'])
    assert result['status'] == 'complete'
    assert first.search.call_count == second.search.call_count > 0
    pages = research.store.query("SELECT * FROM job_queue WHERE json_extract(payload,'$.phase')='company'")
    assert len(pages) == 2  # repeated source hits do not repeat company crawls


def test_market_pages_run_concurrently_on_distinct_hosts_and_close_connections(research, monkeypatch):
    barrier = threading.Barrier(3, timeout=5)
    threads, closed, guard = set(), set(), threading.Lock()
    original_close = research.store.close
    def read(result, emit):
        research.store.one('SELECT 1')  # create the worker's own connection
        with guard:
            threads.add(threading.get_ident())
        barrier.wait()
    def close():
        with guard:
            closed.add(threading.get_ident())
        original_close()
    monkeypatch.setattr(research, '_market_evidence', read)
    monkeypatch.setattr(research.store, 'close', close)
    tasks = [dict(phase='market_page', result=SearchResult(f'https://host{i}.example/report', country='Россия').to_dict()) for i in range(3)]
    result = research._run('market', None, ['Россия'], tasks, {}, lambda _: None, None)
    assert result['status'] == 'complete' and result['pending'] == 0
    assert len(threads) == 3 and threads <= closed


def test_shutdown_interrupt_preserves_queue_and_next_run_uses_same_id(research):
    provider = Mock(search=Mock(side_effect=[Paused('Shutdown'), []]))
    first = research._run('companies', None, ['Россия'], [query('gdelt')], {'gdelt': provider}, lambda _: None, None)
    assert first['status'] == 'interrupted'
    assert research.store.one('SELECT status FROM job_queue')['status'] == 'pending'
    second = research._run('companies', None, ['Россия'], [], {'gdelt': provider}, lambda _: None, None)
    assert first['run_id'] == second['run_id'] and second['status'] == 'complete'


def test_parallel_reads_share_one_robots_load_and_respect_origin_delay(monkeypatch):
    fetcher = SafeFetcher()
    reads, waits = [], []
    def robots(*args, **kwargs):
        reads.append(args[0])
        return FetchResult(args[0], 200, b'User-agent: *\nCrawl-delay: 1')
    # A fixed monotonic instant guarantees the second reservation must wait;
    # no wall-clock delay is needed for this coordination check.
    monkeypatch.setattr(fetcher, '_raw', robots)
    monkeypatch.setattr('sales_manager.research.safe_http.time.monotonic', lambda: 100.)
    monkeypatch.setattr('sales_manager.research.safe_http.time.sleep', waits.append)
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(fetcher._check_robots, ['https://same.example/a', 'https://same.example/b', 'https://same.example/c']))
    assert reads == ['https://same.example/robots.txt']
    assert waits == [1., 1.]


def test_gdelt_followups_use_product_translations_and_do_not_repeat_base_query(research, monkeypatch):
    provider = Mock(search=Mock(return_value=[]))
    monkeypatch.setattr(research, '_providers', lambda: {'gdelt': provider})
    result = research.research_market(['Россия'])
    tasks = [json.loads(row['payload']) for row in research.store.query('SELECT payload FROM job_queue')]
    extra = [task for task in tasks if task.get('followup')]
    assert result['status'] == 'complete' and extra
    from sales_manager.research.providers import GDELTProvider
    assert all(task['query'] in GDELTProvider.GOODS for task in extra)
    assert len({task['query'] for task in tasks}) == len(tasks)


def test_explicit_market_recovery_selects_requested_run_among_same_countries(research, monkeypatch):
    signature = json.dumps(['Россия'], ensure_ascii=False)
    from sales_manager.storage import now
    run_ids = [research.store.execute('INSERT INTO job_runs(kind,status,countries,created_at,updated_at) VALUES(?,?,?,?,?)',
        ('market', 'interrupted', signature, now(), now())) for _ in range(2)]
    provider = Mock(search=Mock(return_value=[]))
    monkeypatch.setattr(research, '_providers', lambda: {'gdelt': provider})
    result = research.resume_market(run_ids[0], ['Россия'])
    assert result['run_id'] == run_ids[0] and result['status'] == 'complete'
    assert research.store.one('SELECT status FROM job_runs WHERE id=?', (run_ids[1],))['status'] == 'interrupted'


def test_configured_sources_include_cooldown_so_ui_can_start_exhaustion_summary(research, monkeypatch):
    import time
    monkeypatch.setattr(research, '_providers', lambda: {'gdelt': Mock()})
    research.store.set_setting('search.gdelt_status', dict(status='Ограничение', retry_until=time.time() + 60))
    assert research.available_sources('market') == []
    assert research.configured_sources('market') == ['gdelt']
