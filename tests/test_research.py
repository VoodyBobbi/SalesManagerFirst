import json
import socket
import threading
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import pytest

from sales_manager.research import ResearchService, normalize_product
from sales_manager.research.extraction import parse_page
from sales_manager.research.providers import FirecrawlProvider, OverpassProvider, ProviderError, SearchResult, WikidataProvider, NoSourcesError
from sales_manager.research.service import Paused, current_demand_excerpt
from sales_manager.research.normalization import CATEGORIES
from sales_manager.research.safe_http import FetchError, FetchResult, SafeFetcher, public_addresses, validate_url
from sales_manager.storage.store import Store


@pytest.fixture
def service(tmp_path):
    store = Store(tmp_path / 'test.sqlite3')
    obj = ResearchService(store, Mock(get=lambda name, default='': 'test-key'))
    yield obj
    store.close()


@pytest.mark.parametrize('name', ['iPhone 17 Pro', 'Samsung Galaxy S26', 'Xiaomi Redmi Note', 'смартфоны'])
def test_phone_brands_are_one_product(name):
    assert normalize_product(name) == 'смартфон'


def test_unrelated_products_remain_distinct():
    assert normalize_product('ноутбук') != normalize_product('смартфон')
    with pytest.raises(ValueError):
        normalize_product('боеприпасы')


@pytest.mark.parametrize('url', ['file:///C:/Windows/win.ini', 'http://user:pass@example.org',
                                'http://localhost/a', 'https://host.local/', 'http://example.com:2375',
                                'http://good.example\\@127.0.0.1/'])
def test_forbidden_url_forms(url):
    with pytest.raises(FetchError):
        validate_url(url)


@pytest.mark.parametrize('address', ['127.0.0.1', '10.0.0.1', '169.254.169.254', '::1',
                                   '::ffff:127.0.0.1', 'fd00::1', '2001:db8::1', '100.64.1.1'])
def test_dns_rejects_private_and_reserved_ipv4_ipv6(address):
    with patch.object(socket, 'getaddrinfo', return_value=[(0, 0, 0, '', (address, 443))]):
        with pytest.raises(FetchError):
            public_addresses('example.org', 443)


def test_ipv6_public_address_and_mixed_dns():
    with patch.object(socket, 'getaddrinfo', return_value=[(0, 0, 0, '', ('2606:4700:4700::1111', 443))]):
        assert public_addresses('example.org', 443) == ['2606:4700:4700::1111']
    with patch.object(socket, 'getaddrinfo', return_value=[(0, 0, 0, '', ('1.1.1.1', 443)), (0, 0, 0, '', ('::1', 443))]):
        with pytest.raises(FetchError):
            public_addresses('example.org', 443)


def test_redirect_to_private_network_is_checked_before_connect():
    fetcher = SafeFetcher()
    with patch.object(fetcher, '_request', return_value=(302, {'location': 'http://localhost/admin'}, b'')) as request:
        with pytest.raises(FetchError):
            fetcher._raw('https://example.org/', 100)
        assert request.call_count == 1


def test_redirect_robots_is_rechecked():
    fetcher = SafeFetcher()
    with patch.object(fetcher, '_check_robots') as robots, patch.object(fetcher, '_request', side_effect=[
        (302, {'location': 'https://other.example/path'}, b''),
        (200, {'content-type': 'text/plain'}, b'hello')]):
        assert fetcher.fetch('https://example.org/').content == b'hello'
        assert [c.args[0] for c in robots.call_args_list] == ['https://example.org/', 'https://other.example/path']


def test_robots_disallow_and_large_crawl_delay_are_respected():
    fetcher = SafeFetcher()
    with patch.object(fetcher, '_raw', return_value=FetchResult('https://example.org/robots.txt', 200,
        b'User-agent: *\nDisallow: /private\nCrawl-delay: 120')):
        with pytest.raises(FetchError, match='robots'):
            fetcher._check_robots('https://example.org/private')
        with pytest.raises(FetchError, match='длительную'):
            fetcher._check_robots('https://example.org/public')


def test_html_contacts_sources_and_scripts():
    page = parse_page('''<title>Аргон | Контакты</title><script>alert('x')</script>
        <a href="mailto:sales@argon.ru">Почта</a><a href="tel:8 (999) 123-45-67">Телефон</a>
        <p>ИНН 1234567890</p><a href="/contacts">Контакты</a>
        <a href="https://other.ru/contacts">Чужой сайт</a>''', 'https://argon.ru/', 'Россия')
    assert page['emails'] == ['sales@argon.ru']
    assert page['phones'] == ['+79991234567']
    assert page['legal_id'] == '1234567890'
    assert page['links'] == ['https://argon.ru/contacts']
    assert 'alert' not in page['text']


def test_firecrawl_keyless_omits_authorization_and_avoids_llm_options():
    request = Mock(return_value={'success': True, 'data': {'web': [{'url': 'https://example.org', 'title': 'Real'}]}})
    provider = FirecrawlProvider(request=request)
    assert len(provider.search('кофе', 'Россия')) == 1
    assert 'headers' not in request.call_args.kwargs
    body = request.call_args.kwargs['json']
    assert body['highlights'] is False and 'scrapeOptions' not in body


def test_unconfirmed_keys_are_not_used(service):
    service.store.set_setting('search.exa_enabled', True)
    service.store.set_setting('search.firecrawl_enabled', True)
    providers = service._providers()
    assert 'exa' not in providers
    assert 'firecrawl' not in providers  # A saved key is not proof that billing is disabled.
    service.secrets.get = Mock(return_value='')
    assert service._providers()['firecrawl'].key == ''


def test_overpass_preserves_country_and_source_contact():
    request = Mock(return_value={'elements': [{'type': 'node', 'id': 123,
            'tags': {'name': 'Кафе Реальное', 'amenity': 'cafe', 'phone': '+79991234567'}}]})
    result = OverpassProvider(request).search('кофе', 'Россия')[0]
    assert result.metadata['source_url'] == 'https://www.openstreetmap.org/node/123'
    assert result.metadata['phones'] == ['+79991234567']
    assert '"ISO3166-1"="RU"' in request.call_args.kwargs['data']['data']


def test_wikidata_does_not_invent_missing_websites():
    request = Mock(side_effect=[{'query': {'search': [{'title': 'Q100'}]}},
                              {'entities': {'Q100': {'id': 'Q100', 'claims': {'P17': [{'mainsnak': {'datavalue': {'value': {'id': 'Q159'}}}}]}}}}])
    assert WikidataProvider(request).search('кофе', 'Россия') == []


def test_company_with_only_phone_visible_and_manual_without_contact_preserved(service):
    product = service.store.ensure_product('кофе')
    service._company(product['id'], SearchResult('https://www.openstreetmap.org/node/5', 'Кафе', 'amenity=cafe', 'Россия',
        metadata={'provider': 'overpass', 'source_url': 'https://www.openstreetmap.org/node/5',
                  'segment_verified': True, 'tags': {'amenity': 'cafe'}, 'phones': ['+79991234567']}), False, lambda _: None)
    assert service.store.one('SELECT * FROM company_fit')['status'] == 'FIT'
    assert service.store.one('SELECT * FROM company_contacts')['kind'] == 'phone'
    with patch.object(service, '_page', side_effect=FetchError('Недоступен')):
        service.find_companies(product['id'], ['Россия'], manual_url='https://manual.example/', emit=lambda _: None)
    assert service.store.one('SELECT * FROM company_fit WHERE manual=1')['status'] == 'INSUFFICIENT_EVIDENCE'


def test_company_dedup_does_not_merge_different_legal_entities(service):
    a = service._save_company('Компания', 'example.ru', 'https://example.ru/', 'Россия', '1111111111', False)
    b = service._save_company('Компания', 'example.ru', 'https://example.ru/', 'Россия', '2222222222', False)
    assert a != b
    assert service._save_company('Компания', 'example.ru', 'https://example.ru/', 'Россия', '1111111111', False) == a


def test_market_vocabulary_does_not_prepopulate_fake_top(service):
    with patch.object(service, '_providers', return_value={}):
        with pytest.raises(NoSourcesError, match='Нет доступных источников'):
            service.research_market(['Россия'])
    assert service.store.query('SELECT * FROM market_signals') == []
    assert service.store.query('SELECT * FROM products') == []
    assert service.store.query('SELECT * FROM job_runs') == []


def test_market_needs_explicit_evidence_and_groups_brands(service):
    with patch.object(service, '_page', side_effect=FetchError('Не удалось открыть')):
        period = datetime.now(timezone.utc).strftime('%Y-%m')
        service._market_evidence(SearchResult('https://report.example/r',
            f'В России за {period} продажи iPhone увеличились на 20%', '', 'Россия',
            datetime.now(timezone.utc).isoformat()), lambda _: None)
        assert service.store.one('SELECT * FROM products')['name'] == 'смартфон'
        service._market_evidence(SearchResult('https://report.example/x', 'Каталог ноутбуков', 'Ноутбук доступен в каталоге', 'Россия'), lambda _: None)
    assert service.store.one('SELECT COUNT(*) n FROM market_signals')['n'] == 1


def test_pause_persists_queue_and_resume_reuses_same_run(service):
    stopped = threading.Event()
    stopped.set()
    provider = Mock(search=Mock(return_value=[]))
    with patch.object(service, '_providers', return_value={'firecrawl': provider}):
        first = service.research_market(['Россия'], cancel=stopped)
        assert first['status'] == 'interrupted'
        assert not provider.search.called
        second = service.research_market(['Россия'])
    assert first['run_id'] == second['run_id']
    assert second['status'] == 'complete'
    assert service.store.one("SELECT COUNT(*) n FROM job_queue WHERE status!='done'")['n'] == 0


def test_legacy_route_uses_other_sources_after_primary_success(service):
    primary = Mock(search=Mock(return_value=[SearchResult('https://real.example/', 'Компания')]))
    fallback = Mock(search=Mock(return_value=[]))
    task = {'phase': 'search', 'provider': 'web', 'route': ['exa', 'firecrawl'],
            'next_provider': 0, 'query': 'ноутбук магазин Россия', 'country': 'Россия'}
    with patch.object(service, '_enqueue') as enqueue:
        service._handle(1, 1, task, {'exa': primary, 'firecrawl': fallback}, lambda _: None)
    primary.search.assert_called_once()
    fallback.search.assert_called_once()
    assert enqueue.call_count == 1
    assert task['successful_providers'] == ['exa', 'firecrawl']


def test_empty_primary_permits_one_compatible_fallback(service):
    primary = Mock(search=Mock(return_value=[]))
    fallback = Mock(search=Mock(return_value=[SearchResult('https://real.example/', 'Компания')]))
    task = {'phase': 'search', 'provider': 'web', 'route': ['exa', 'firecrawl'],
            'next_provider': 0, 'query': 'кофе ресторан Россия', 'country': 'Россия'}
    with patch.object(service, '_enqueue'):
        service._handle(1, 1, task, {'exa': primary, 'firecrawl': fallback}, lambda _: None)
    primary.search.assert_called_once()
    fallback.search.assert_called_once()
    assert task['selected_provider'] == 'firecrawl'


def test_pending_fallback_position_survives_pause(service):
    primary = Mock(search=Mock(side_effect=ProviderError('Исчерпана квота')))
    fallback = Mock(search=Mock(side_effect=Paused('Пауза')))
    providers = {'exa': primary, 'firecrawl': fallback}
    task = {'phase': 'search', 'provider': 'web', 'route': ['exa', 'firecrawl'],
            'next_provider': 0, 'query': 'кофе', 'country': 'Россия'}
    first = service._run('route-test', None, ['Россия'], [task], providers, lambda _: None, None)
    assert first['status'] == 'interrupted'
    payload = json.loads(service.store.one('SELECT * FROM job_queue')['payload'])
    assert payload['next_provider'] == 1
    fallback.search.side_effect = None
    fallback.search.return_value = []
    second = service._run('route-test', None, ['Россия'], [task], providers, lambda _: None, None)
    assert second['run_id'] == first['run_id'] and second['status'] == 'complete'
    assert primary.search.call_count == 1


def test_unavailable_primary_not_hammered_for_other_queries(service):
    primary = Mock(search=Mock(side_effect=ProviderError('Недоступен')))
    fallback = Mock(search=Mock(return_value=[]))
    tasks = [{'phase': 'search', 'provider': 'web', 'route': ['exa', 'firecrawl'],
              'next_provider': 0, 'query': query, 'country': 'Россия'} for query in ('кофе', 'ноутбук')]
    result = service._run('route-block-test', None, ['Россия'], tasks, {'exa': primary, 'firecrawl': fallback}, lambda _: None, None)
    assert result['status'] == 'complete'
    assert primary.search.call_count == 1
    assert fallback.search.call_count == 2


def test_market_routes_web_once_and_sweeps_categories_before_next_country(service):
    with patch.object(service, '_providers', return_value={'exa': Mock(), 'firecrawl': Mock(), 'gdelt': Mock()}), patch.object(service, '_run', return_value={}) as run:
        service.research_market(['Россия', 'Казахстан'])
    tasks = run.call_args.args[3]
    web_tasks = [task for task in tasks if task['provider'] != 'gdelt']
    assert {task['country'] for task in web_tasks[:6]} == {'Россия', 'Казахстан'}
    assert all(task['country'] == '' for task in tasks if task['provider'] == 'gdelt')
    assert {task['provider'] for task in tasks} == {'exa', 'firecrawl', 'gdelt'}
    assert len(tasks) == len(CATEGORIES) * (2 * 2 + 1)
    assert len({(t['provider'],t['query'],t['country']) for t in tasks}) == len(tasks)
    assert not any(t['provider'] == 'web' for t in tasks)


@pytest.mark.parametrize('title', ['Спрос на смартфоны упал на 10%',
                                  'Продажи смартфонов снизились на 10%',
                                  'Спрос на смартфоны снижается на 10%',
                                  'Продажи смартфонов падают на 10%',
                                  'Динамика продаж смартфонов −10%',
                                  'Прогноз: спрос на смартфоны вырастет на 20%',
                                  'Ожидается увеличение продаж смартфонов на 20%',
                                  'Спрос на смартфоны может увеличиться на 20%'])
def test_negative_or_forecast_demand_not_promoted_to_current_top(service, title):
    with patch.object(service, '_page', side_effect=FetchError('Недоступен')):
        service._market_evidence(SearchResult('https://report.example/test', title, '', 'Россия'), lambda _: None)
    assert service.store.query('SELECT * FROM market_signals') == []


def test_actual_growth_can_be_kept_separately_from_forecast():
    assert current_demand_excerpt('смартфон', 'Продажи смартфонов выросли на 20%. Прогноз обещает рост продаж смартфонов на 40%.') == 'Продажи смартфонов выросли на 20%.'
    assert not current_demand_excerpt('смартфон', 'Спрос на смартфоны упал на 10%. Продажи ноутбуков выросли на 20%.')
