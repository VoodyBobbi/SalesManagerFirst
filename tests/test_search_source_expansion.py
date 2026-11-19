"""Published feed expansion and product kinds independent of a shared brand."""
from unittest.mock import Mock

import pytest

from sales_manager.research.normalization import normalize_product, product_candidates
from sales_manager.research.providers import RSSProvider


@pytest.mark.parametrize('name,kind', [
    ('Samsung Galaxy S26', 'смартфон'),
    ('Samsung Galaxy Tab S10', 'планшет'),
    ('Samsung Galaxy Watch 8', 'умные часы'),
    ('Samsung Galaxy Buds 3', 'наушники'),
    ('Пылесос Dyson V15', 'пылесос'),
])
def test_shared_brand_does_not_collapse_product_kinds(name, kind):
    assert normalize_product(name) == kind
    assert product_candidates(name) == [kind]


def test_brand_alone_is_not_evidence_of_a_product_kind():
    assert normalize_product('Фен Dyson Supersonic') != 'пылесос'
    assert 'пылесос' not in product_candidates('Продажи фенов Dyson выросли в России.')
    assert 'смартфон' not in product_candidates('Samsung Galaxy Book и Galaxy Ring')


def test_previous_defaults_upgrade_but_custom_feeds_are_preserved():
    assert RSSProvider.effective_urls(RSSProvider.RELEASE_140_URLS) == RSSProvider.DEFAULT_URLS
    custom = RSSProvider.RELEASE_140_URLS + ['https://my.example/feed']
    assert RSSProvider.effective_urls(custom) == custom


def test_additional_feeds_are_loaded_once_and_failures_do_not_hide_later_feeds():
    from sales_manager.research.safe_http import FetchError
    urls = [f'https://source{i}.example/feed' for i in range(11)]

    def document(url):
        if url == urls[0]:
            raise FetchError('Source unavailable')
        return {'url': url, 'text': '<rss><channel><item>'
                '<title>В России выросли продажи планшетов</title>'
                f'<link>{url}/article</link></item></channel></rss>'}

    fetch = Mock(side_effect=document)
    provider = RSSProvider(fetch, urls)
    result = provider.discover('Россия', limit=100)
    assert len(result) == 10
    assert fetch.call_count == 11
    assert provider.last_responded == 10
    assert provider.last_attempted == 11
    assert provider.last_errors == {urls[0]: 'Source unavailable'}
    provider.search('планшет', 'Россия')
    assert fetch.call_count == 11
