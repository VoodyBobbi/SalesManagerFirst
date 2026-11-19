"""Regression coverage for durable research, identities and provider checks."""
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import pytest

from sales_manager.core.company_listing import visible_companies
from sales_manager.research.normalization import CATEGORIES, branch_identity
from sales_manager.research.providers import InvalidKeyError, RateLimitError, ProviderError, SearchResult
from sales_manager.research.quota import fetch_quota
from sales_manager.research.safe_http import FetchError
from sales_manager.research.service import Paused, ResearchService, current_demand_excerpt, now
from sales_manager.storage import Store


@pytest.fixture
def service(tmp_path):
    store = Store(tmp_path / 'research.sqlite3')
    obj = ResearchService(store, Mock(get=lambda *_: ''))
    yield obj
    store.close()


def run_row(service, kind='market', product_id=None):
    return service.store.execute('INSERT INTO job_runs(kind,product_id,status,countries,created_at,updated_at) '
                                 'VALUES(?,?,?,?,?,?)',
                                 (kind, product_id, 'running', '["Россия"]', now(), now()))


def saved_candidate(service, name='Компания', country='Россия', email='sales@company.ru'):
    product = service.store.ensure_product('кофе')
    company_id = service._save_company(name, 'company.ru', 'https://company.ru/', country, '', False)
    source_id = service.store.put_source('https://company.ru/', name, 'Мы работаем в России. Факт деятельности компании.')
    profile = {'activities': [{'code': 'office', 'quote': 'Факт деятельности компании.', 'source_id': source_id}],
               'country': country}
    service.store.execute('''INSERT INTO company_fit(product_id,company_id,status,reason,source_ids,
        potential,activity_profile,fit_key,checked_at) VALUES(?,?,'INSUFFICIENT_EVIDENCE',?,?,1,?,'former-key',?)''',
        (product['id'], company_id, 'Сохранённый кандидат', json.dumps([source_id]), json.dumps(profile), now()))
    service.store.execute("INSERT INTO company_contacts(company_id,kind,value,source_id) VALUES(?,'email',?,?)",
                          (company_id, email, source_id))
    return product['id'], company_id, source_id, profile


def test_transient_fetch_failure_preserves_candidate_and_exact_evidence(service):
    product_id, company_id, source_id, profile = saved_candidate(service)
    with patch.object(service, '_page', side_effect=FetchError('Временный сбой')):
        # The failed response has no title or geographical evidence.
        service._company(product_id, SearchResult('https://company.ru/', country='Россия'), False, lambda _: None)
    row = service.store.one('SELECT * FROM company_fit WHERE company_id=?', (company_id,))
    assert row['status'] == 'INSUFFICIENT_EVIDENCE' and row['potential'] == 1
    assert row['reason'] == 'Сохранённый кандидат'
    assert json.loads(row['source_ids']) == [source_id]
    assert json.loads(row['activity_profile']) == profile
    assert row['fit_key'] == 'former-key'
    assert len(visible_companies(service.store, product_id)) == 1
    assert service.store.one('SELECT COUNT(*) AS n FROM companies')['n'] == 1


def test_unknown_country_reuses_unique_company_and_keeps_known_country(service):
    first = service._save_company('Компания', 'company.ru', 'https://company.ru/', 'Россия', '123', False)
    assert service._save_company('Компания', 'company.ru', 'https://company.ru/', '', '123', False) == first
    assert service.store.one('SELECT country FROM companies WHERE id=?', (first,))['country'] == 'Россия'


def test_unknown_country_does_not_merge_ambiguous_multinational_brand(service):
    first = service._save_company('Бренд', 'brand.com', 'https://brand.com/', 'Россия', '', False)
    second = service._save_company('Бренд', 'brand.com', 'https://brand.com/', 'Казахстан', '', False)
    unidentified = service._save_company('Бренд', 'brand.com', 'https://brand.com/', '', '', False)
    assert len({first, second, unidentified}) == 3


@pytest.mark.parametrize('name,canonical', [('Офисные кресла', 'офисное кресло'), ('Смартфоны', 'смартфон')])
def test_market_reuses_plural_user_workspace(service, name, canonical):
    product = service.store.ensure_product(name, branch_identity(name))
    period = datetime.now(timezone.utc).strftime('%Y-%m')
    title = f'В России за {period} продажи {name.lower()} выросли на 20%.'
    with patch.object(service, '_page', side_effect=FetchError('Недоступен')):
        service._market_evidence(SearchResult('https://report.ru/article', title, '', 'Россия'), lambda _: None)
    assert service.store.one('SELECT COUNT(*) AS n FROM products')['n'] == 1
    observation = service.store.one('SELECT product_id FROM market_observations')
    assert observation['product_id'] == product['id']
    assert product['normalized'] == canonical


def test_gdelt_market_plans_one_geographically_neutral_query_per_category(service):
    with patch.object(service, '_providers', return_value={'gdelt': Mock()}), patch.object(service, '_run') as run:
        service.research_market(['Россия', 'Казахстан', 'Беларусь'])
    tasks = run.call_args.args[3]
    assert len(tasks) == len(CATEGORIES)
    assert all(task['country'] == '' and task['provider'] == 'gdelt' for task in tasks)


def test_tavily_market_groups_countries_instead_of_multiplying_search_credits(service):
    provider = Mock(search=Mock(return_value=[]))
    countries = ['Россия', 'Казахстан', 'Беларусь']
    with patch.object(service, '_providers', return_value={'tavily': provider}):
        result = service.research_market(countries)
    tasks = [json.loads(row['payload']) for row in service.store.query('SELECT payload FROM job_queue')]
    assert result['status'] == 'complete'
    assert len(tasks) == len(CATEGORIES) * 2  # one base and one followup per category
    assert provider.search.call_count == len(tasks)
    assert all(task['country'] == '' and task['selected_countries'] == countries for task in tasks)
    assert all(all(country in task['query'] for country in countries) for task in tasks)
    assert len({task['query'] for task in tasks}) == len(tasks)


def test_grouped_market_search_only_saves_geographically_supported_selected_facts(service):
    period = datetime.now(timezone.utc).strftime('%Y-%m')
    text = (f'В России за {period} продано 100 смартфонов. '
            f'В Китае за {period} продано 200 смартфонов.')
    result = SearchResult('https://report.ru/article', text, '', '', metadata={
        'official_statistics': True, 'product': 'смартфон', 'selected_countries': ['Россия']})
    service._market_evidence(result, lambda _: None)
    observations = service.store.query('SELECT country,value FROM market_observations')
    assert observations and {item['country'] for item in observations} == {'Россия'}
    assert any(item['value'] == 100 for item in observations)


def test_cancellation_check_does_not_consume_or_reject_last_allowed_request(service, monkeypatch):
    service._limit = 1
    monkeypatch.setattr(service.fetcher, '_check_robots', lambda _: None)
    monkeypatch.setattr(service.fetcher, '_request', lambda *a, **kw:
                        (200, {'content-type': 'text/html'}, '<html>Контакты</html>'.encode()))
    assert service._page('https://company.ru/', 'Россия')['text'] == 'Контакты'
    assert service._requests == 1
    with pytest.raises(Paused):
        service._page('https://another-company.ru/', 'Россия')


def test_status_snapshot_uses_keys_without_credential_manager_reads(service):
    service.secrets.get = Mock(side_effect=AssertionError('Unexpected Credential Manager read'))
    keys = {'tavily': 'unsaved-key', 'exa': '', 'firecrawl': '', 'serper': '', 'jina': ''}
    statuses = service.provider_statuses(['tavily', 'exa', 'rss'], keys=keys)
    assert set(statuses) == {'tavily', 'exa', 'rss'}
    assert statuses['exa']['status'] == 'Нет ключа'
    service.search_mode(keys=keys)
    service.billing_blocked_sources(keys=keys)
    service.secrets.get.assert_not_called()


def test_gdelt_reprints_are_not_enqueued_again_for_another_search_country(service):
    run_id = run_row(service)
    for country in ('Россия', 'Казахстан'):
        result = SearchResult('https://report.ru/article', country=country, metadata={'provider': 'gdelt'})
        service._enqueue(run_id, {'phase': 'market_page', 'result': result.to_dict()})
    assert service.store.one('SELECT COUNT(*) AS n FROM job_queue')['n'] == 1


def test_concurrent_request_accounting_extends_each_batch_once(service):
    service._active_run_id, service._batch_size, service._limit = 1, 10, 10
    start = threading.Barrier(8)
    def worker():
        start.wait()
        for _ in range(25):
            service._checkpoint()
            service._take_request()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: worker(), range(8)))
    assert service._requests == 200
    assert service._limit in (200, 210)


def batch_rows(service):
    run_id = run_row(service)
    for host in ('first.ru', 'second.ru'):
        service._enqueue(run_id, {'phase': 'market_page', 'result': SearchResult('https://' + host + '/').to_dict()})
    return run_id, service.store.query('SELECT * FROM job_queue ORDER BY id')


def test_bad_page_is_isolated_and_other_page_completes(service):
    run_id, rows = batch_rows(service)
    def read(result, emit):
        if 'first.ru' in result.url:
            raise ValueError('Broken parser input')
    with patch.object(service, '_market_evidence', side_effect=read):
        assert service._market_page_batch(run_id, rows, lambda _: None)
    rows = service.store.query('SELECT status,error FROM job_queue ORDER BY id')
    assert all(row['status'] == 'done' for row in rows)
    assert 'ValueError' in rows[0]['error'] and not rows[1]['error']


def test_single_bad_page_does_not_abort_later_healthy_search(service):
    tasks = [
        {'phase': 'market_page', 'result': SearchResult('https://broken.ru/article').to_dict()},
        {'phase': 'search', 'provider': 'healthy', 'query': 'кофе', 'country': 'Россия', 'market': True},
    ]
    healthy = Mock(search=Mock(return_value=[]))
    with patch.object(service, '_market_evidence', side_effect=ValueError('Broken page')):
        result = service._run('market', None, ['Россия'], tasks, {'healthy': healthy}, lambda _: None, None)
    assert result['status'] == 'complete'
    healthy.search.assert_called_once()
    assert service.store.one("SELECT COUNT(*) n FROM job_queue WHERE status!='done'")['n'] == 0
    assert 'ValueError' in service.store.one("SELECT error FROM job_queue WHERE json_extract(payload,'$.phase')='market_page'")['error']


def test_demand_excerpt_keeps_quarter_abbreviation_inside_reporting_sentence():
    text = 'В России продажи смартфонов за 1 кв. 2026 года выросли на 20%.'
    assert current_demand_excerpt('смартфон', text) == text


def test_pause_takes_precedence_over_another_workers_storage_failure(service):
    run_id, rows = batch_rows(service)
    def read(result, emit):
        if 'first.ru' in result.url:
            raise sqlite3.OperationalError('Storage failed')
        raise Paused('Закрытие')
    with patch.object(service, '_market_evidence', side_effect=read):
        with pytest.raises(Paused):
            service._market_page_batch(run_id, rows, lambda _: None)
    assert service.store.one("SELECT status FROM job_queue WHERE id=?", (rows[1]['id'],))['status'] == 'pending'


def test_failed_source_tasks_retry_when_same_run_is_resumed(service):
    product = service.store.ensure_product('кофе')
    tasks = [{'phase': 'search', 'provider': source, 'query': query, 'country': 'Россия'}
             for source, query in [('gdelt', 'one'), ('gdelt', 'two'), ('overpass', 'control')]]
    gdelt = Mock(search=Mock(side_effect=ProviderError('Временный сбой')))
    control = Mock(search=Mock(side_effect=Paused('Закрытие')))
    providers = {'gdelt': gdelt, 'overpass': control}
    first = service._run('companies', product['id'], ['Россия'], tasks, providers, lambda _: None, None)
    assert first['status'] == 'interrupted'
    assert gdelt.search.call_count == 1
    gdelt.search.side_effect, gdelt.search.return_value = None, []
    control.search.side_effect, control.search.return_value = None, []
    # The persisted cooldown has also elapsed before the explicit resume.
    service.store.set_setting('search.gdelt_status', {})
    second = service._run('companies', product['id'], ['Россия'], tasks, providers, lambda _: None, None)
    assert second['run_id'] == first['run_id']
    assert second['status'] == 'complete' and gdelt.search.call_count == 3
    assert service.store.one("SELECT COUNT(*) n FROM job_queue WHERE status!='done'")['n'] == 0


def test_company_count_matches_shared_visibility_for_unknown_country_and_fake_email(service):
    product_id, company_id, _, _ = saved_candidate(service, country='', email='name@domain.com')
    visible = visible_companies(service.store, product_id)
    assert len(visible) == 1
    assert visible[0]['fit_status'] == 'INSUFFICIENT_EVIDENCE' and not visible[0]['email']
    service.store.execute("UPDATE company_contacts SET value='sales@company.ru' WHERE company_id=?", (company_id,))
    result = service._run('companies', product_id, ['Россия'], [], {}, lambda _: None, None)
    assert result['count'] == len(visible_companies(service.store, product_id)) == 1


def test_quota_supports_unlimited_key_with_account_limit():
    request = Mock(return_value={'account': {'plan_limit': 1000, 'plan_usage': 120},
                                'key': {'limit': None, 'usage': 10}})
    assert fetch_quota('tavily', 'key', request) == 880


@pytest.mark.parametrize('error', [InvalidKeyError('Неверный ключ'), RateLimitError('Подождите', retry_after=33)])
def test_account_verification_error_is_not_hidden_by_missing_billing_proof(service, error):
    service.secrets.get = Mock(side_effect=lambda name, *_: 'key' if name == 'tavily_key' else '')
    service.store.set_setting('search.tavily_enabled', True)
    with patch('sales_manager.research.service.json_http', side_effect=error):
        with pytest.raises(type(error)):
            service.test_provider('tavily')
    status = service.provider_status('tavily')
    assert status['status'] == error.status and status['retry_until'] > 0


def test_tavily_connection_test_uses_one_usage_request_and_no_search_credit(service):
    service.secrets.get = Mock(side_effect=lambda name, *_: 'key' if name == 'tavily_key' else '')
    service.store.set_setting('search.tavily_enabled', True)
    response = {'account': {'current_plan': 'Researcher', 'plan_limit': 1000, 'plan_usage': 120,
                            'paygo_limit': 0, 'paygo_usage': 0}, 'key': {'limit': None, 'usage': 10}}
    with patch('sales_manager.research.service.json_http', return_value=response) as request:
        assert 'Соединение проверено' in service.test_provider('tavily')
    request.assert_called_once_with('GET', 'https://api.tavily.com/usage', headers={'Authorization': 'Bearer key'})
    assert service.provider_status('tavily')['quota'] == 880
