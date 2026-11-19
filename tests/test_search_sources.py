import json
import time
from unittest.mock import Mock, patch

import httpx
import pytest

from sales_manager.research.providers import (KEYED_PROVIDERS, KEYLESS_PROVIDERS, ExaProvider, OverpassProvider, WikidataProvider,
    TavilyProvider, SerperProvider, JinaProvider, MediaWikiProvider, RSSProvider, SearchResult,
    ProviderError, NoSourcesError, QuotaError, RateLimitError, InvalidKeyError, MissingKeyError,
    InvalidResponseError, FirecrawlProvider, check_response, json_http)
from sales_manager.research.extraction import parse_feed, sitemap_links
from sales_manager.research.safe_http import FetchError, FetchResult, SafeFetcher
from sales_manager.research.service import ResearchService
from sales_manager.storage.store import Store


@pytest.fixture
def service(tmp_path):
    store = Store(tmp_path / 'sources.sqlite3')
    keys = {}
    result = ResearchService(store, Mock(get=lambda name, default='': keys.get(name, default)))
    result.test_keys = keys
    yield result
    store.close()


def test_keyless_default_is_real_adapters_no_ai(service):
    sources = service._providers()
    assert set(KEYLESS_PROVIDERS) - {'catalog', 'procurement'} <= set(sources)
    assert not {'tavily', 'serper', 'exa', 'jina'} & set(sources)
    assert service.search_mode() == 'Режим поиска: базовый — без API-ключей'
    product = service.store.ensure_product('кофе')
    available = {'overpass': Mock(search=Mock(return_value=[SearchResult(
        'https://www.openstreetmap.org/node/15', 'Кафе', 'amenity=cafe', 'Россия',
        metadata={'provider': 'overpass', 'source_url': 'https://www.openstreetmap.org/node/15',
                  'segment_verified': True, 'tags': {'amenity': 'cafe'}, 'phones': ['+79991234567']})]))}
    with patch.object(service, '_providers', return_value=available):
        result = service.find_companies(product['id'], ['Россия'])
    assert result['count'] == 1 and result['status'] == 'complete'
    assert service.store.query("SELECT * FROM usage_log WHERE provider='gigachat'") == []


def test_checkbox_cannot_prove_tavily_billing_is_disabled(service):
    service.test_keys['tavily_key'] = 'trial-key'
    service.store.set_setting('search.tavily_enabled', True)
    assert 'tavily' not in service._providers()
    service.store.set_setting('search.free_confirmed', True)
    assert 'tavily' not in service._providers()
    assert service.search_mode() == 'Режим поиска: базовый — без API-ключей'
    assert 'бесплатный' in service.provider_status('tavily')['message']
    with patch.object(service, '_access_request', return_value={}), patch.object(service, '_json') as request:
        with pytest.raises(ProviderError, match='тарифе'):
            service.test_provider('tavily')
        request.assert_not_called()


def test_recent_tavily_billing_proof_is_reused_for_new_search(service):
    from sales_manager.research.access import verify_free_access
    service.test_keys['tavily_key'] = 'trial-key'
    service.store.set_setting('search.tavily_enabled', True)
    proof = verify_free_access('tavily', 'trial-key', lambda *args, **kwargs: {
        'account': {'current_plan': 'Researcher', 'plan_limit': 1000, 'plan_usage': 20,
                    'paygo_limit': 0, 'paygo_usage': 0},
        'key': {'limit': 500, 'usage': 10}})
    service.store.set_setting('search.tavily_free_access', proof)
    provider = TavilyProvider('trial-key', request=Mock(return_value={'results': []}))
    with patch.object(service, '_access_request') as usage_request:
        assert service._search('tavily', provider, 'unique cache-miss query', 'Россия') == []
    usage_request.assert_not_called()


def test_rss_connection_check_can_read_all_eleven_feeds(service):
    html = '<rss><channel><item><title>Новости рынка кофе</title><link>https://feed.example/story</link></item></channel></rss>'
    service.fetcher.fetch_document = Mock(side_effect=lambda url: FetchResult(
        url, 200, html.encode('utf-8'), 'application/rss+xml'))
    provider = RSSProvider(service._document)
    with patch.object(service, '_providers', return_value={'rss': provider}):
        result = service.test_provider('rss')
    assert 'Соединение проверено' in result
    assert service.fetcher.fetch_document.call_count == len(RSSProvider.DEFAULT_URLS) == 11


def test_billing_guard_skips_old_route_and_continues_other_tasks(service):
    service.test_keys['tavily_key'] = 'trial-key'
    service.store.set_setting('search.tavily_enabled', True)
    service.store.set_setting('search.free_confirmed', True)
    tavily = {'phase': 'search', 'provider': 'web', 'query': 'кофе', 'country': 'Россия', 'route': ['tavily']}
    open_task = {'phase': 'search', 'provider': 'gdelt', 'query': 'кофе', 'country': 'Россия'}
    open_source = Mock(search=Mock(return_value=[]))
    result = service._run('companies', None, ['Россия'], [tavily, open_task],
                          {'gdelt': open_source}, lambda _: None, None)
    assert result['status'] == 'complete'
    assert result['billing_blocked_sources'] == ['tavily']
    assert open_source.search.call_count == 1
    skipped = service.store.one("SELECT * FROM job_queue WHERE error LIKE 'Пропущено:%'")
    assert skipped and skipped['status'] == 'done'


def test_saved_queue_continues_after_billing_guard_blocks_source(service):
    task = {'phase': 'search', 'provider': 'web', 'query': 'кофе', 'country': 'Россия', 'route': ['tavily']}
    from sales_manager.research.service import Paused
    first = Mock(search=Mock(side_effect=Paused('Закрытие приложения')))
    saved = service._run('companies', None, ['Россия'], [task], {'tavily': first}, lambda _: None, None)
    assert saved['status'] == 'interrupted'
    service.test_keys['tavily_key'] = 'trial-key'
    service.store.set_setting('search.tavily_enabled', True)
    open_task = {'phase': 'search', 'provider': 'gdelt', 'query': 'кофе', 'country': 'Россия'}
    open_source = Mock(search=Mock(return_value=[]))
    resumed = service._run('companies', None, ['Россия'], [open_task], {'gdelt': open_source}, lambda _: None, None)
    assert resumed['run_id'] == saved['run_id'] and resumed['status'] == 'complete'
    assert open_source.search.call_count == 1
    assert service.store.one("SELECT status FROM job_queue WHERE error LIKE 'Пропущено:%'")['status'] == 'done'


def test_unexpected_company_failure_does_not_stop_remaining_queue(service):
    calls = []
    def process_company(product_id, result, manual, emit):
        calls.append(result.url)
        if len(calls) == 1:
            raise RuntimeError('unexpected company parser failure')
    tasks = [
        {'phase': 'company', 'result': SearchResult('https://first.example/').to_dict(), 'manual': False},
        {'phase': 'company', 'result': SearchResult('https://second.example/').to_dict(), 'manual': False},
    ]
    service._company = process_company

    result = service._run('companies', None, ['Россия'], tasks,
                          {}, lambda _: None, None)

    assert result['status'] == 'complete'
    assert calls == ['https://first.example/', 'https://second.example/']
    rows = service.store.query('SELECT status,error FROM job_queue ORDER BY id')
    assert rows[0]['status'] == 'done' and rows[0]['error'].startswith('RuntimeError:')
    assert rows[1]['status'] == 'done' and rows[1]['error'] == ''


def test_sqlite_failure_still_fails_research_run(service):
    import sqlite3

    provider = Mock(search=Mock(side_effect=sqlite3.OperationalError('database is locked')))
    task = {'phase': 'search', 'provider': 'gdelt', 'query': 'кофе', 'country': 'Россия'}

    with pytest.raises(sqlite3.OperationalError):
        service._run('companies', None, ['Россия'], [task], {'gdelt': provider}, lambda _: None, None)

    assert service.store.one('SELECT status,reason FROM job_runs')['status'] == 'failed'


@pytest.mark.parametrize('name', ['tavily', 'serper', 'exa', 'jina', 'firecrawl'])
def test_keyed_sources_never_search_when_billing_unknown(service, name):
    service.test_keys[name + '_key'] = 'test-key'
    service.store.set_setting('search.' + name + '_enabled', True)
    service.store.set_setting('search.free_confirmed', True)
    assert name not in service._providers()
    assert name in service.billing_blocked_sources()


def test_anonymous_firecrawl_cannot_send_account_credentials(service):
    provider = service._providers()['firecrawl']
    request = Mock(return_value={'data': {'web': []}})
    provider.request = request
    assert provider.search('кофе', 'Россия') == []
    assert 'headers' not in request.call_args.kwargs


def test_tavily_basic_has_no_answer_raw_content_or_auto_upgrade():
    request = Mock(return_value={'results': [{'url': 'https://real.example/', 'content': 'Факт'}]})
    result = TavilyProvider('key', request).search('кофе Россия', 'Россия', 100)
    assert result[0].metadata['provider'] == 'tavily'
    body = request.call_args.kwargs['json']
    assert body['search_depth'] == 'basic' and body['auto_parameters'] is False
    assert body['include_answer'] is False and body['include_raw_content'] is False
    assert body['max_results'] == 10 and 'api_key' not in body


def test_serper_and_jina_use_documented_headers_and_compact_output():
    request = Mock(return_value={'organic': [{'link': 'https://real.example/', 'snippet': 'Факт'}]})
    assert SerperProvider('key', request).search('кофе', 'Казахстан')[0].url == 'https://real.example/'
    assert request.call_args.args[1] == 'https://google.serper.dev/search'
    assert request.call_args.kwargs['json']['gl'] == 'kz'
    request.return_value = {'data': [{'url': 'https://real.example/', 'description': 'Факт'}]}
    assert JinaProvider('key', request).search('кофе', 'Россия')
    assert request.call_args.kwargs['headers']['X-Respond-With'] == 'no-content'
    with pytest.raises(MissingKeyError):
        JinaProvider('', request).search('кофе', 'Россия')


@pytest.mark.parametrize('code,body,expected', [(401, {}, InvalidKeyError),
    (432, {}, QuotaError), (433, {}, QuotaError), (402, {}, QuotaError),
    (429, {'error': 'Insufficient credits'}, QuotaError), (429, {}, RateLimitError),
    (503, {}, ProviderError), (200, {'error': {'code': 'maxlag'}}, RateLimitError)])
def test_error_states(code, body, expected):
    with pytest.raises(expected):
        check_response(code, body, {'Retry-After': '120'})


def test_rate_limit_not_quota_and_retry_after_respected(service):
    with pytest.raises(RateLimitError) as error:
        check_response(429, {}, {'retry-after': '180'})
    assert error.value.status == 'Временное ограничение'
    provider = Mock(search=Mock(side_effect=error.value))
    with pytest.raises(RateLimitError):
        service._search('gdelt', provider, 'кофе', 'Россия')
    state = service.provider_status('gdelt')
    assert state['retry_until'] >= time.time() + 179
    with pytest.raises(ProviderError):
        service._search('gdelt', provider, 'кофе', 'Россия')
    assert provider.search.call_count == 1
    assert 'gdelt' not in service.available_sources()


@pytest.mark.parametrize('failure', [QuotaError('Бесплатный лимит исчерпан'), ProviderError('Недоступен')])
def test_web_quota_or_unavailable_falls_back_once(service, failure):
    first = Mock(search=Mock(side_effect=failure))
    second = Mock(search=Mock(return_value=[SearchResult('https://real.example/')]))
    task = {'phase': 'search', 'provider': 'web', 'query': 'кофе', 'country': 'Россия',
            'route': ['tavily', 'serper'], 'next_provider': 0}
    messages = []
    with patch.object(service, '_enqueue') as enqueue:
        service._handle(1, 1, task, {'tavily': first, 'serper': second}, messages.append)
    assert second.search.call_count == 1 and enqueue.call_count == 1
    assert any(str(failure) in m for m in messages)
    assert service.provider_status('tavily')['status'] in ('Нет ключа', failure.status)


def test_no_sources_does_not_create_empty_job(service):
    for name in list(KEYED_PROVIDERS) + list(KEYLESS_PROVIDERS):
        service.store.set_setting('search.' + name + '_enabled', False)
    with pytest.raises(NoSourcesError, match='Включите хотя бы один'):
        service.research_market(['Россия'])
    product = service.store.ensure_product('кофе')
    with pytest.raises(NoSourcesError):
        service.find_companies(product['id'], ['Россия'])
    assert service.store.query('SELECT * FROM job_runs') == []
    with patch.object(service, '_page', side_effect=FetchError('Недоступен')):
        result = service.find_companies(product['id'], ['Россия'], manual_url='https://real.example/')
    assert result['status'] == 'complete'
    assert service.store.one('SELECT * FROM company_fit')['manual'] == 1


def test_exhausted_sources_finish_and_new_provider_can_start_new_search(service):
    first = Mock(search=Mock(side_effect=QuotaError('Лимит исчерпан')))
    task = {'phase': 'search', 'provider': 'web', 'query': 'кофе', 'country': 'Россия', 'route': ['tavily']}
    result = service._run('companies', None, ['Россия'], [task], {'tavily': first}, lambda _: None, None)
    assert result['status'] == 'complete' and result['reason'] == 'sources_exhausted'
    assert service.store.one('SELECT * FROM job_queue')['status'] == 'done'
    second = Mock(search=Mock(return_value=[]))
    result2 = service._run('companies', None, ['Россия'], [task], {'serper': second}, lambda _: None, None)
    assert result2['run_id'] != result['run_id'] and result2['status'] == 'complete'
    assert second.search.called


def test_empty_is_success_not_quota_and_bad_schema_is_not_success(service):
    request = Mock(return_value={'results': []})
    provider = TavilyProvider('key', request)
    assert provider.search('кофе', 'Россия') == []
    request.return_value = {'unexpected': []}
    with pytest.raises(InvalidResponseError):
        provider.search('кофе', 'Россия')


def test_firecrawl_auth_required_never_counts_as_success():
    with patch('sales_manager.research.providers.httpx.Client') as factory:
        response = factory.return_value.__enter__.return_value.stream.return_value.__enter__.return_value
        response.status_code = 401
        response.iter_bytes.return_value = iter([b'{"error":"Unauthorized"}'])
        response.headers = {}
        with pytest.raises(MissingKeyError):
            FirecrawlProvider().search('кофе', 'Россия')


def test_mediawiki_references_not_claimed_official():
    request = Mock(return_value={'query': {'pages': {'1': {'title': 'Факт',
        'fullurl': 'https://ru.wikipedia.org/wiki/Test', 'extract': 'Описание',
        'extlinks': [{'*': 'https://reference.example/'}]}}}})
    result = MediaWikiProvider(request).search('кофе', 'Россия')[0]
    assert result.metadata['discovery_urls'] == ['https://reference.example/']
    assert 'website' not in result.metadata


def test_rss_atom_parsing_and_cache_safe_content():
    xml = '''<rss><channel><item><title>Спрос на кофе вырос на 10%</title>
       <link>https://report.example/coffee</link><description>Рост продаж кофе</description>
       <pubDate>2026-11-10</pubDate></item></channel></rss>'''
    result = RSSProvider(Mock(return_value={'url': 'https://feed.example/', 'text': xml}),
                         ['https://feed.example/']).search('кофе', 'Россия')
    assert result[0].metadata['feed_url'] == 'https://feed.example/'
    assert RSSProvider(Mock(return_value={'url': 'https://feed.example/', 'text': xml}),
                       ['https://feed.example/']).search('ноутбук', 'Россия') == []
    atom = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Товар</title><link href="https://real.example/"/></entry></feed>'
    assert parse_feed(atom, 'https://feed.example/')[0].url == 'https://real.example/'
    with pytest.raises(InvalidResponseError):
        parse_feed('<!DOCTYPE x [<!ENTITY e SYSTEM "file:///private">]><rss/>', 'https://feed.example/')


def test_sitemap_only_same_site_useful_links_no_entities():
    xml = '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://real.example/contact</loc></url><url><loc>http://localhost/contact</loc></url></urlset>'
    assert sitemap_links(xml, 'https://real.example/') == ['https://real.example/contact']
    assert sitemap_links('<!DOCTYPE x><urlset/>', 'https://real.example/') == []


def test_robots_429_is_not_treated_as_allowed():
    fetcher = SafeFetcher()
    with patch.object(fetcher, '_raw', return_value=FetchResult('https://real.example/robots.txt', 429, b'')):
        with pytest.raises(FetchError, match='robots'):
            fetcher._check_robots('https://real.example/contact')


def test_source_cooldown_and_results_survive_reopen(service):
    service._record_status('gdelt', RateLimitError('Подождите', retry_after=300))
    product = service.store.ensure_product('кофе')
    path = service.store.path
    service.store.close()
    reopened = Store(path)
    try:
        restored = ResearchService(reopened, service.secrets)
        assert restored.provider_status('gdelt')['status'] == 'Временное ограничение'
        assert reopened.one('SELECT * FROM products')['id'] == product['id']
    finally:
        reopened.close()


@pytest.mark.parametrize('factory', [ExaProvider, FirecrawlProvider, OverpassProvider, WikidataProvider])
def test_malformed_legacy_source_response_is_not_empty_success(factory):
    request = Mock(return_value={})
    provider = factory('key', request) if factory in (ExaProvider, FirecrawlProvider) else factory(request)
    with pytest.raises(InvalidResponseError):
        provider.search('кофе', 'Россия')


def test_generic_rate_exhaustion_is_not_credit_exhaustion():
    with pytest.raises(RateLimitError):
        check_response(429, {'error': 'rate limit exhausted'})


def test_changed_key_still_requires_billing_proof(service):
    service.test_keys['tavily_key'] = 'old-key'
    service.store.set_setting('search.tavily_enabled', True)
    service.store.set_setting('search.free_confirmed', True)
    service._record_status('tavily', InvalidKeyError('Неверный ключ', retry_after=86400))
    assert 'tavily' not in service.available_sources()
    service.test_keys['tavily_key'] = 'new-key'
    assert 'tavily' not in service.available_sources()
    assert 'old-key' not in json.dumps(service.store.query('SELECT * FROM settings'))


def test_source_check_uses_actual_provider_not_cache(service):
    with patch('sales_manager.research.service.json_http', return_value={'elements': []}) as request:
        service.test_provider('overpass')
        service._provider_clock.clear()
        service.test_provider('overpass')
        assert request.call_count == 2
    assert service._force_refresh is False


def test_public_catalog_is_explicit_configured_source_and_checked(service):
    service.store.set_setting('search.catalog_enabled', True)
    assert 'catalog' not in service._providers()
    service.store.set_setting('search.catalog_urls', ['https://catalog.example/companies'])
    assert 'catalog' in service._providers()
    page = {'url': 'https://catalog.example/companies', 'title': 'Компании', 'text': 'Кофе',
            'external_links': ['https://buyer.example/']}
    with patch.object(service, '_page', return_value=page) as fetch:
        assert 'проверено' in service.test_provider('catalog')
    assert fetch.call_args.args[0] == 'https://catalog.example/companies'


def test_directory_discovery_never_makes_directory_a_buyer(service):
    task = {'phase': 'discovery', 'url': 'https://publication.example/story', 'country': 'Россия',
            'query': 'кофе', 'result': SearchResult('https://publication.example/story',
             metadata={'discovery_urls': ['https://buyer.example/', 'http://localhost/']}).to_dict()}
    with patch.object(service, '_page', side_effect=FetchError('Недоступен')), patch.object(service, '_enqueue') as enqueue:
        service._handle(1, 1, task, {}, lambda _: None)
    assert enqueue.call_count == 1
    payload = enqueue.call_args.args[1]
    assert payload['result']['url'] == 'https://buyer.example/'
    assert payload['result']['metadata']['discovered_from'] == task['url']
    assert service.store.query('SELECT * FROM companies') == []


def test_sitemap_contact_discovered_in_first_company_pass_without_paid_search(service):
    product = service.store.ensure_product('кофе')
    service.store.set_setting('search.page_limit', 2)
    def page(url, country):
        contact = url.endswith('/contacts')
        text = 'Магазин кофе в России. Контакты.'
        return {'url': url, 'title': 'Кофе', 'name': 'Компания Кофе', 'text': text,
                'source_id': service.store.put_source(url, 'Кофе', text),
                'links': [], 'emails': ['sales@buyer.ru'] if contact else [], 'phones': [], 'legal_id': ''}
    sitemap = {'url': 'https://buyer.ru/sitemap.xml',
               'text': '<urlset><url><loc>https://buyer.ru/contacts</loc></url></urlset>'}
    with patch.object(service, '_page', side_effect=page), patch.object(service, '_document', return_value=sitemap), patch.object(service, '_json') as api:
        service._company(product['id'], SearchResult('https://buyer.ru/', country='Россия'), False, lambda _: None)
    assert service.store.one('SELECT * FROM company_contacts')['value'] == 'sales@buyer.ru'
    assert service.store.one('SELECT * FROM company_fit')['status'] == 'FIT'
    api.assert_not_called()


def test_rss_rfc_date_becomes_iso_for_staleness_check():
    xml = '<rss><channel><item><title>Спрос на кофе</title><link>https://real.example/</link><pubDate>Tue, 10 Nov 2026 10:00:00 GMT</pubDate></item></channel></rss>'
    assert parse_feed(xml, 'https://feed.example/')[0].published_at == '2026-11-10T10:00:00+00:00'
