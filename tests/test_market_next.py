"""Quantities, dates, adapter capabilities and reported market demand."""
import json
from unittest.mock import Mock

import pytest

from sales_manager.research.market import extract_observations, period_from
from sales_manager.research.normalization import normalize_product, product_candidates
from sales_manager.research.extraction import parse_page
from sales_manager.research.providers import GDELTProvider, RSSProvider
from sales_manager.research.registry import ADAPTERS, AdapterSpec, market_source_names


@pytest.mark.parametrize('month', ['май', 'мая', 'мае', 'маю', 'маем'])
def test_may_reporting_period_preserved(month):
    text = f'В России в {month} 2026 года продано 500 смартфонов.'
    assert period_from(text) == '2026-05'
    rows = extract_observations('смартфон', text)
    assert [(row['value'], row['unit'], row['period']) for row in rows] == [(500, 'units', '2026-05')]


@pytest.mark.parametrize('currency,unit', [
    ('долларов', 'USD'), ('USD', 'USD'), ('$', 'USD'),
    ('евро', 'EUR'), ('EUR', 'EUR'), ('€', 'EUR'),
])
def test_sales_currency_is_typed_not_items(currency, unit):
    rows = extract_observations('смартфон',
        f'В России за октябрь 2026 года продажи смартфонов составили 500 {currency}.')
    assert [(row['metric'], row['value'], row['unit']) for row in rows] == [('sales', 500, unit)]


@pytest.mark.parametrize('amount,unit', [('$500', 'USD'), ('€500', 'EUR')])
def test_prefix_currency_is_a_sales_amount(amount, unit):
    rows = extract_observations('смартфон',
        f'В России за октябрь 2026 года продажи смартфонов составили {amount}.')
    assert [(row['value'], row['unit']) for row in rows] == [(500, unit)]


@pytest.mark.parametrize('phrase', [
    'продано 500 чехлов для смартфонов',
    'в 500 магазинах продажи смартфонов выросли',
    '500 долларов за смартфон при росте продаж',
    'продано 500 штук чехлов для смартфонов',
    'смартфоны продавались за 500 долларов',
    'продажи смартфонов выросли при цене 500 евро',
])
def test_accessories_store_counts_and_prices_never_become_phone_sales(phrase):
    rows = extract_observations('смартфон', f'В России за октябрь 2026 года {phrase}.')
    assert not any(row['measured'] for row in rows)


@pytest.mark.parametrize('product,phrase', [
    ('смартфон', 'продано 500 новых смартфонов'),
    ('офисное кресло', 'продано 500 новых офисных кресел'),
    ('система хранения данных', 'продано 500 систем хранения данных'),
    ('картонная коробка', 'продано 500 картонных коробок'),
])
def test_immediate_product_noun_preserves_valid_counts(product, phrase):
    rows = extract_observations(product, f'В России за октябрь 2026 года {phrase}.')
    assert [(row['value'], row['unit']) for row in rows] == [(500, 'units')]


@pytest.mark.parametrize('value', [None, 123, {}, [], '', '   '])
def test_invalid_schema_name_falls_back_to_website_identity(value):
    html = '<title>Компания Покупатель — Главная</title><script type="application/ld+json">' + json.dumps(
        {'@type': 'Organization', 'name': value}) + '</script>'
    assert parse_page(html, 'https://buyer.test/')['name'] == 'Компания Покупатель'


def test_valid_organization_name_and_og_fallback():
    html = '<title>Главная</title><meta property="og:site_name" content="Сеть магазинов"><script type="application/ld+json">{"@type":"Organization","name":null}</script>'
    assert parse_page(html, 'https://buyer.test/')['name'] == 'Сеть магазинов'
    assert parse_page('<script type="application/ld+json">{"@type":"Organization","name":"Покупатель"}</script>',
                      'https://buyer.test/')['name'] == 'Покупатель'


def test_registry_excludes_company_enrichment_and_imports(monkeypatch):
    assert len(ADAPTERS) == 13
    assert set(market_source_names()) == {'tavily', 'serper', 'exa', 'firecrawl', 'jina', 'gdelt', 'rss', 'procurement'}
    monkeypatch.setitem(ADAPTERS, 'newsearch', AdapterSpec(object, frozenset({'market'}), True))
    assert 'newsearch' in market_source_names()
    assert len(market_source_names()) == 9


def test_gdelt_uses_translated_search_without_claiming_publisher_is_market():
    request = Mock(return_value={'articles': [{'url': 'https://paper.test/news', 'title': 'Статистика продаж',
                                              'sourcecountry': 'Belarus', 'seendate': '20261117T120000Z'}]})
    result = GDELTProvider(request).search('бытовая техника спрос новости Россия', 'Россия')
    query = request.call_args.kwargs['params']['query']
    assert 'appliances OR refrigerators' in query
    assert 'demand OR sales' in query
    assert 'sourcelang:russian' in query
    assert 'sourcecountry:' not in query
    assert result[0].metadata['source_country'] == 'Belarus'
    assert result[0].country == 'Россия'  # Search context only; extraction still validates the article.


def test_gdelt_does_not_call_english_index_with_untranslated_words():
    request = Mock(return_value={'articles': []})
    source = GDELTProvider(request)
    assert source.search('клапан обратного давления', 'Россия') == []
    request.assert_not_called()
    source.search('рынок кофе Россия', 'Россия')
    assert 'coffee' in request.call_args.kwargs['params']['query']


def test_resumed_market_plan_drops_the_old_year_filter(tmp_path, monkeypatch):
    from sales_manager.research.service import ResearchService
    from sales_manager.storage import Store

    store = Store(tmp_path / 'market.sqlite')
    try:
        service = ResearchService(store, Mock())
        search = Mock(return_value=[])
        monkeypatch.setattr(service, '_search', search)
        provider = Mock()
        task = {'phase': 'search', 'market': True, 'provider': 'tavily',
                'query': 'спрос на кофе (2025 OR 2026)', 'country': 'Россия'}
        service._handle(1, None, task, {'tavily': provider}, lambda message: None)
        search.assert_called_once_with('tavily', provider, 'спрос на кофе', 'Россия', limit=10)
    finally:
        store.close()


def test_rss_extracts_product_candidates_once_per_entry(monkeypatch):
    from sales_manager.research import normalization
    from sales_manager.research.providers import SearchResult

    extract = Mock(wraps=normalization.product_candidates)
    monkeypatch.setattr(normalization, 'product_candidates', extract)
    source = RSSProvider(Mock())
    entry = SearchResult('https://news.example/paper', 'Бумага', 'растёт спрос')
    monkeypatch.setattr(source, 'discover', lambda *args, **kwargs: [entry])
    source.search('канцелярия и офис', 'Россия')
    assert [call.args for call in extract.call_args_list].count(('бумага растёт спрос',)) == 1


@pytest.mark.parametrize('text', [
    'В России покупатели чаще покупали кофе.',
    'В России смартфоны стали самыми популярными товарами.',
    'В России лидерами продаж стали пылесосы.',
])
def test_reported_preferences_preserved_as_indirect_without_invented_numbers(text):
    product = product_candidates(text)[0]
    rows = extract_observations(product, text)
    assert rows and all(not row['measured'] and row['value'] is None for row in rows)


@pytest.mark.parametrize('name,expected', [('Пылесос Xiaomi Redmi', 'пылесос'), ('Samsung Galaxy', 'смартфон'), ('iPhone 17', 'смартфон')])
def test_explicit_product_type_precedes_brand(name, expected):
    assert normalize_product(name) == expected
    if expected != 'смартфон':
        assert 'смартфон' not in product_candidates(name)
