"""Allowed feed discovery and official no-key measurements, with no live network."""
from unittest.mock import Mock

import pytest

from sales_manager.research.providers import (ComtradeProvider, InvalidResponseError, ProviderError,
                                            RSSProvider, KEYLESS_PROVIDERS)
from sales_manager.research.safe_http import FetchError


def trade_row(**changes):
    row = {'reporterCode': 398, 'period': '2025', 'cmdCode': '0901', 'flowCode': 'M',
           'partnerCode': 0, 'primaryValue': 47805677.89, 'netWgt': 4730375.81,
           'classificationCode': 'H6'}
    return {**row, **changes}


def test_official_trade_is_imports_with_exact_market_period_and_two_distinct_units():
    request = Mock(return_value={'data': [trade_row()], 'error': ''})
    result = ComtradeProvider(request, year=2025).search('кофе', 'Казахстан')
    assert len(result) == 1
    assert result[0].metadata['official_statistics'] is True
    assert result[0].metadata['product'] == 'кофе'
    measurements = result[0].metadata['measurements']
    assert [(m['metric'], m['value'], m['unit']) for m in measurements] == [
        ('imports', 47805677.89, 'USD'), ('imports', 4730375.81, 'kg')]
    assert all(m['country'] == 'Казахстан' and m['period'] == '2025' for m in measurements)
    assert all(m['source_url'] == result[0].url for m in measurements)
    assert all(str(m['value']) in m['excerpt'] for m in measurements)
    request.assert_called_once()
    params = request.call_args.kwargs['params']
    assert params['cmdCode'] == '0901' and params['partnerCode'] == 0
    assert params['maxRecords'] <= 10
    assert 'headers' not in request.call_args.kwargs


@pytest.mark.parametrize('change', [{'reporterCode': 643}, {'period': '2024'},
                                  {'cmdCode': '0902'}, {'flowCode': 'X'}, {'partnerCode': 156}])
def test_trade_response_for_other_market_goods_period_or_flow_not_relabelled(change):
    request = Mock(return_value={'data': [trade_row(**change)]})
    assert ComtradeProvider(request, year=2025).search('кофе', 'Казахстан') == []


def test_unknown_product_never_borrowed_from_broad_commodity_code():
    request = Mock()
    assert ComtradeProvider(request, year=2025).search('Офисные кресла', 'Россия') == []
    assert ComtradeProvider(request, year=2025).search('кофе', 'Неизвестная страна') == []
    request.assert_not_called()


def test_broad_categories_do_not_silently_select_one_comtrade_product():
    request = Mock(return_value={'data': []})
    assert ComtradeProvider(request, year=2025).search('электроника', 'Россия') == []
    assert ComtradeProvider(request, year=2025).search('продукты питания', 'Россия') == []
    request.assert_not_called()


@pytest.mark.parametrize('product,code', [('кофе', '0901'), ('чай', '0902'), ('смартфон', '851713')])
def test_each_supported_comtrade_product_has_its_own_exact_query(product, code):
    request = Mock(return_value={'data': []})
    assert ComtradeProvider(request, year=2025).search(product, 'Россия') == []
    request.assert_called_once()
    assert request.call_args.kwargs['params']['cmdCode'] == code


@pytest.mark.parametrize('number', [None, True, -1, '12', float('nan'), float('inf')])
def test_invalid_trade_numbers_never_become_measurements(number):
    request = Mock(return_value={'data': [trade_row(primaryValue=number, netWgt=number)]})
    assert ComtradeProvider(request, year=2025).search('кофе', 'Казахстан') == []


def test_empty_and_bad_trade_responses_are_distinct():
    request = Mock(return_value={'data': []})
    assert ComtradeProvider(request, year=2025).search('кофе', 'Казахстан') == []
    request.return_value = {'unexpected': []}
    with pytest.raises(InvalidResponseError):
        ComtradeProvider(request, year=2025).search('кофе', 'Казахстан')


def test_comtrade_is_keyless_market_only_source(tmp_path):
    from sales_manager.storage import Store
    from sales_manager.research.service import ResearchService
    store = Store(tmp_path / 'source.sqlite3')
    service = ResearchService(store, Mock(get=lambda *args: ''))
    assert 'comtrade' in KEYLESS_PROVIDERS
    assert 'comtrade' not in service.available_sources('market')
    assert 'comtrade' not in service.available_sources('companies')
    assert isinstance(service._providers()['comtrade'], ComtradeProvider)
    store.close()


def test_retail_defaults_replace_only_previous_application_default():
    assert len(RSSProvider.DEFAULT_URLS) == 11
    assert 'cbr.ru' not in ' '.join(RSSProvider.DEFAULT_URLS)
    assert RSSProvider.effective_urls(RSSProvider.LEGACY_DEFAULT_URLS) == RSSProvider.DEFAULT_URLS
    assert RSSProvider.effective_urls(RSSProvider.PREVIOUS_DEFAULT_URLS) == RSSProvider.DEFAULT_URLS
    custom = ['https://organization.example/press.xml']
    assert RSSProvider.effective_urls(custom) == custom
    assert RSSProvider.effective_urls([]) == []


def test_rss_category_reads_actual_goods_and_deduplicates_feed_items():
    feed = '''<rss><channel><item><title>В России продажи смартфонов выросли</title>
    <link>https://www.retail.ru/news/phones</link><description>Продажи iPhone и Xiaomi.</description>
    </item></channel></rss>'''
    fetch = Mock(side_effect=lambda url: {'url': url, 'text': feed})
    result = RSSProvider(fetch).search('электроника', 'Россия')
    assert len(result) == 1
    assert result[0].url == 'https://www.retail.ru/news/phones'
    assert result[0].metadata['source_type'] == 'industry_publication'
    assert fetch.call_count == len(RSSProvider.DEFAULT_URLS)


def feed_item(url, title='Рынок товаров России', description='Данные исследования'):
    return f'<item><title>{title}</title><link>{url}</link><description>{description}</description></item>'


def test_broad_rss_discovery_reads_more_than_three_feeds_once_and_keeps_publishers():
    urls = [f'https://publisher{number}.example/rss.xml' for number in range(5)]
    def fetch(url):
        number = urls.index(url)
        items = ''.join(feed_item(f'https://publisher{number}.example/news/{i}') for i in range(3))
        return {'url': url, 'text': '<rss><channel>' + items + '</channel></rss>'}
    read = Mock(side_effect=fetch)
    provider = RSSProvider(read, urls)
    results = provider.discover('Россия', limit=5)
    assert [result.url for result in results] == [f'https://publisher{i}.example/news/0' for i in range(5)]
    assert provider.last_attempted == provider.last_responded == 5
    assert not provider.last_errors
    assert read.call_count == 5
    again = provider.discover('Казахстан', limit=8)
    assert len(again) == 8 and all(result.country == 'Казахстан' for result in again)
    assert all(result.country == 'Россия' for result in results)
    assert read.call_count == 5


def test_one_failed_feed_does_not_hide_other_feed_results_or_repeat_the_failure():
    urls = ['https://offline.example/rss', 'https://working.example/rss']
    def fetch(url):
        if 'offline' in url:
            raise FetchError('Сайт временно недоступен')
        return {'url': url, 'text': '<rss><channel>' + feed_item('https://working.example/news', 'Продажи кофе в России') + '</channel></rss>'}
    read = Mock(side_effect=fetch)
    provider = RSSProvider(read, urls)
    assert len(provider.discover('Россия')) == 1
    assert provider.last_responded == 1
    assert provider.last_errors == {urls[0]: 'Сайт временно недоступен'}
    assert len(provider.search('кофе', 'Россия')) == 1
    assert read.call_count == 2


def test_all_rss_failures_and_a_successful_empty_feed_are_different():
    failing = RSSProvider(Mock(side_effect=FetchError('Нет доступа')), ['https://offline.example/rss'])
    with pytest.raises(ProviderError):
        failing.discover('Россия')
    assert failing.last_responded == 0 and failing.last_errors
    empty = RSSProvider(Mock(return_value={'url': 'https://empty.example/rss', 'text': '<rss><channel/></rss>'}), ['https://empty.example/rss'])
    assert empty.discover('Россия') == []
    assert empty.last_responded == 1 and not empty.last_errors


def test_rss_discovery_retains_namespaced_unicode_and_deduplicates_urls():
    feed = '<rss xmlns="http://backend.userland.com/rss2"><channel>' + feed_item('https://publisher.example/news', 'В России выросли продажи кресел', 'Спрос на офисную мебель') + '</channel></rss>'
    read = Mock(side_effect=lambda url: {'url': url, 'text': feed})
    provider = RSSProvider(read, ['https://first.example/rss', 'https://second.example/rss'])
    result = provider.discover('Россия')
    assert len(result) == 1
    assert result[0].title == 'В России выросли продажи кресел'
    assert result[0].snippet == 'Спрос на офисную мебель'
    assert result[0].metadata['feed_url'] == 'https://first.example/rss'


def test_feed_list_is_bounded_at_sixteen_unique_addresses():
    read = Mock(side_effect=lambda url: {'url': url, 'text': '<rss><channel/></rss>'})
    urls = ['https://first.example/rss'] * 3 + [f'https://publisher{i}.example/rss' for i in range(20)]
    provider = RSSProvider(read, urls)
    assert provider.discover('Россия') == []
    assert read.call_count == provider.last_attempted == 16
