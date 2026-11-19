"""Saved market jobs report real adapter coverage and finish only with no work left."""
import json
import threading
import time
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from sales_manager.research import service as module
from sales_manager.research.access import verify_free_access
from sales_manager.research.providers import ProviderError, SearchResult
from sales_manager.research.registry import ADAPTERS, AdapterSpec, market_source_names
from sales_manager.research.reporting import market_summary
from sales_manager.research.service import Paused, ResearchService
from sales_manager.storage import Store, now


class Keys:
    def __init__(self, **values):
        self.values = values

    def get(self, name, default=''):
        return self.values.get(name, default)


class Answering:
    def __init__(self, batches=()):
        self.batches = list(batches)
        self.calls = []

    def search(self, query, country, limit=10):
        self.calls.append((query, country))
        if not self.batches:
            return []
        result = self.batches.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class Feed:
    last_errors = {}

    def __init__(self, results=()):
        self.results = results
        self.calls = 0

    def discover(self, country, limit=1000):
        self.calls += 1
        return self.results


@pytest.fixture
def market_case(tmp_path, monkeypatch):
    store = Store(tmp_path / 'market.sqlite3')
    research = ResearchService(store, Keys())
    for name in ADAPTERS:
        store.set_setting(f'search.{name}_enabled', False)
    # No test may accidentally contact a real account or public search endpoint.
    monkeypatch.setattr(module, 'json_http', Mock(side_effect=AssertionError('Unexpected real HTTP request')))
    yield store, research
    store.close()


def saved_page_loader(store, texts):
    def page(url, country):
        text = texts[url]
        sid = store.put_source(url, 'Рыночный отчёт', text, datetime.now(timezone.utc).isoformat())
        return dict(url=url, source_id=sid, text=text, market_text=text,
                    title='Рыночный отчёт', name='Издатель', links=[], emails=[], phones=[], legal_id='')
    return page


def goods_results(goods, texts, offset=0):
    stamp = datetime.now(timezone.utc)
    output = []
    for index, good in enumerate(goods, offset):
        url = f'https://publisher{index}.test/report'
        text = f'Продажи {good} в России за {stamp:%Y-%m} составили {1000 + index} штук.'
        texts[url] = text
        output.append(SearchResult(url, f'Продажи {good} в России', text,
                                   'Россия', stamp.isoformat()))
    return output


def assert_short_summary(result):
    message = result['message']
    assert f"Использовано источников: {result['sources_used']}/{result['sources_total']}" in message
    assert all(technical not in message for technical in ('GDELT', 'RSS', 'HTTP', '429', 'Firecrawl'))


def test_registry_counts_market_capabilities_and_new_adapter_automatically(market_case, monkeypatch):
    store, research = market_case
    assert set(market_source_names()) == {'tavily', 'serper', 'exa', 'firecrawl', 'jina', 'gdelt', 'rss', 'procurement'}

    class AddedAdapter:
        def __init__(self, request):
            self.calls = []

        def search(self, query, country, limit=10):
            self.calls.append(query)
            return []

    monkeypatch.setitem(ADAPTERS, 'added_api', AdapterSpec(AddedAdapter, frozenset({'market'})))
    store.set_setting('search.added_api_enabled', True)
    result = research.research_market(['Россия'])
    assert result['sources_total'] == 9
    assert result['sources_used'] == 1
    assert result['successful_sources'] == ['added_api']
    assert_short_summary(result)


def test_repeated_successes_count_once_and_disabled_sources_do_not_count(market_case, monkeypatch):
    store, research = market_case
    provider = Answering()
    monkeypatch.setattr(research, '_providers', lambda: {'gdelt': provider})
    result = research.research_market(['Россия'])
    assert len(provider.calls) > 1
    assert len(provider.calls) == len(set(provider.calls))
    assert result['sources_used'] == 1 and result['sources_total'] == 8
    assert store.one('SELECT COUNT(*) n FROM job_source_responses')['n'] == 1
    assert_short_summary(result)


def test_connection_error_is_not_source_success_and_job_finishes(market_case, monkeypatch):
    store, research = market_case
    provider = Answering([ProviderError('HTTP 429 GDELT unavailable')])
    monkeypatch.setattr(research, '_providers', lambda: {'gdelt': provider})
    result = research.research_market(['Россия'])
    assert result['status'] == 'complete'
    assert result['pending'] == 0 and result['sources_used'] == 0
    assert store.one('SELECT COUNT(*) n FROM job_source_responses')['n'] == 0
    assert 'Доступные источники исчерпаны' in result['message']
    assert store.query('SELECT message FROM job_diagnostics')
    assert_short_summary(result)


def test_gdelt_failure_does_not_block_rss_or_erase_results(market_case, monkeypatch):
    store, research = market_case
    texts = {}
    feed = Feed(goods_results(['смартфон', 'ноутбук', 'принтер', 'телевизор'], texts))
    failed = Answering([ProviderError('HTTP 429')])
    monkeypatch.setattr(research, '_providers', lambda: {'rss': feed, 'gdelt': failed})
    monkeypatch.setattr(research, '_page', saved_page_loader(store, texts))
    result = research.research_market(['Россия'])
    assert feed.calls == 1 and len(failed.calls) == 1
    assert result['count'] == 4
    assert result['successful_sources'] == ['rss']
    assert result['status'] == 'complete' and result['pending'] == 0
    assert_short_summary(result)


def test_four_goods_do_not_stop_remaining_categories(market_case, monkeypatch):
    store, research = market_case
    texts = {}
    first = goods_results(['смартфон', 'ноутбук', 'принтер', 'телевизор'], texts)
    second = goods_results(['пылесос', 'офисное кресло', 'стол', 'шкаф', 'холодильник', 'самокат'], texts, offset=4)
    provider = Answering([first, second])
    monkeypatch.setattr(research, '_providers', lambda: {'gdelt': provider})
    monkeypatch.setattr(research, '_page', saved_page_loader(store, texts))
    result = research.research_market(['Россия'])
    assert len(provider.calls) >= 2
    assert result['count'] == 10
    assert result['status'] == 'complete' and result['pending'] == 0
    assert len(research.market_ranking(['Россия'])['rows']) == 10
    assert all(row['source_ids'] for row in research.market_ranking(['Россия'])['rows'])
    assert_short_summary(result)


def test_seven_confirmed_goods_stay_seven_when_work_is_exhausted(market_case, monkeypatch):
    store, research = market_case
    texts = {}
    feed = Feed(goods_results(['смартфон', 'ноутбук', 'принтер', 'телевизор', 'пылесос', 'стол', 'шкаф'], texts))
    monkeypatch.setattr(research, '_providers', lambda: {'rss': feed})
    monkeypatch.setattr(research, '_page', saved_page_loader(store, texts))
    result = research.research_market(['Россия'])
    assert result['count'] == 7 and result['pending'] == 0
    assert result['status'] == 'complete'
    assert 'Найдено товаров: 7' in result['message']
    assert store.one('SELECT COUNT(*) n FROM products')['n'] == 7
    assert_short_summary(result)


def test_successful_sources_and_work_survive_restart_resume(market_case, monkeypatch):
    store, research = market_case
    feed = Feed()
    failed = Answering([Paused('Process stopped')])
    monkeypatch.setattr(research, '_providers', lambda: {'rss': feed, 'gdelt': failed})
    first = research.research_market(['Россия'])
    assert first['sources_used'] == 1 and first['status'] == 'interrupted'
    path = store.path
    store.close()
    restarted_store = Store(path)
    try:
        restarted = ResearchService(restarted_store, Keys())
        restarted_store.execute("DELETE FROM settings WHERE key='search.gdelt_status'")  # The provider cooldown has elapsed.
        recovered = Answering()
        monkeypatch.setattr(restarted, '_providers', lambda: {'rss': feed, 'gdelt': recovered})
        summary = market_summary(restarted_store, first['run_id'], ['Россия'])
        assert summary['sources_used'] == 1
        resumed = restarted.research_market(['Россия'])
        assert resumed['run_id'] == first['run_id']
        assert resumed['sources_used'] == 2 and resumed['pending'] == 0
        assert resumed['status'] == 'complete'
        assert feed.calls == 1
        assert resumed['successful_sources'] == ['gdelt', 'rss']
    finally:
        restarted_store.close()


@pytest.mark.parametrize('state', ['complete', 'complete_partial'])
def test_pending_work_can_never_be_reported_complete(market_case, state):
    store, research = market_case
    run = store.execute('INSERT INTO job_runs(kind,status,countries,created_at,updated_at) VALUES(?,?,?,?,?)',
                        ('market', state, json.dumps(['Россия'], ensure_ascii=False), now(), now()))
    store.execute("INSERT INTO job_queue(run_id,task_key,payload,status) VALUES(?,?,?,?)", (run, 'pending', '{}', 'pending'))
    result = market_summary(store, run, ['Россия'])
    assert result['status'] == 'interrupted' and result['pending'] == 1
    assert 'Поиск завершён' not in result['message']


def test_user_pause_preserves_queued_work_without_success(market_case, monkeypatch):
    store, research = market_case
    provider = Answering()
    monkeypatch.setattr(research, '_providers', lambda: {'gdelt': provider})
    cancel = threading.Event()
    cancel.set()
    result = research.research_market(['Россия'], cancel=cancel)
    assert result['status'] == 'interrupted' and result['pending'] > 0
    assert result['sources_used'] == 0 and provider.calls == []


def test_comtrade_is_never_called_or_enqueued_by_market(market_case, monkeypatch):
    store, research = market_case
    imports = Answering()
    monkeypatch.setattr(research, '_providers', lambda: {'rss': Feed(), 'comtrade': imports})
    result = research.research_market(['Россия'])
    assert imports.calls == []
    assert result['sources_used'] == 1 and result['sources_total'] == 8
    assert all(json.loads(row['payload']).get('provider') != 'comtrade'
               for row in store.query("SELECT payload FROM job_queue"))


def test_new_verified_api_participates_in_next_search(market_case, monkeypatch):
    store, research = market_case
    feed = Feed()
    # Keep provider construction real: adding a credential alone must not add a
    # service, but saving a freshly verified free plan must make it usable.
    monkeypatch.setattr(module, 'RSSProvider', lambda *args: feed)
    store.set_setting('search.rss_enabled', True)
    first = research.research_market(['Россия'])
    assert first['sources_used'] == 1
    account = {'account': {'current_plan': 'Researcher', 'plan_limit': 1000,
                           'plan_usage': 0, 'paygo_limit': 0, 'paygo_usage': 0},
               'key': {'limit': 1000, 'usage': 0}}
    research.secrets.values['tavily_key'] = 'controlled-key'
    store.set_setting('search.tavily_enabled', True)
    assert 'tavily' not in research._providers()
    proof = verify_free_access('tavily', 'controlled-key', Mock(return_value=account))
    store.set_setting('search.tavily_free_access', proof)
    monkeypatch.setattr(research, '_access_request', Mock(return_value=account))
    searches = Mock(return_value={'results': []})
    monkeypatch.setattr(research, '_json', searches)
    second = research.research_market(['Россия'])
    assert searches.called
    assert all(call.args[:2] == ('POST', 'https://api.tavily.com/search') for call in searches.call_args_list)
    assert second['sources_used'] == 2
    assert second['successful_sources'] == ['rss', 'tavily']


def test_unknown_billing_key_is_never_sent_to_a_search_endpoint(market_case, monkeypatch):
    store, research = market_case
    monkeypatch.setattr(module, 'RSSProvider', lambda *args: Feed())
    store.set_setting('search.rss_enabled', True)
    for name in ('tavily', 'serper', 'exa', 'firecrawl', 'jina'):
        store.set_setting(f'search.{name}_enabled', True)
        research.secrets.values[name + '_key'] = 'synthetic-key'
    monkeypatch.setattr(research, '_access_request', Mock(return_value={}))
    search = Mock(side_effect=AssertionError('Unverified key reached search'))
    monkeypatch.setattr(research, '_json', search)
    result = research.research_market(['Россия'])
    assert result['sources_used'] == 1 and result['successful_sources'] == ['rss']
    search.assert_not_called()


def test_expired_verified_access_can_be_refreshed_before_background_search(market_case, monkeypatch):
    store, research = market_case
    account = {'account': {'current_plan': 'Researcher', 'plan_limit': 1000,
                          'plan_usage': 0, 'paygo_limit': 0, 'paygo_usage': 0},
               'key': {'limit': 1000, 'usage': 0}}
    key = 'controlled-key'
    research.secrets.values['tavily_key'] = key
    store.set_setting('search.tavily_enabled', True)
    proof = verify_free_access('tavily', key, Mock(return_value=account))
    proof.update(checked_at=time.time() - 1000, expires_at=time.time() - 400)
    store.set_setting('search.tavily_free_access', proof)
    assert 'tavily' not in research._providers()
    assert 'tavily' in research.available_sources('market')
    monkeypatch.setattr(research, '_access_request', Mock(return_value=account))
    search = Mock(return_value={'results': []})
    monkeypatch.setattr(research, '_json', search)
    result = research.research_market(['Россия'])
    assert search.called and result['sources_used'] == 1
    assert result['successful_sources'] == ['tavily']
