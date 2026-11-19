from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from sales_manager.research.market import extract_observations, market_evidence
from sales_manager.research.normalization import branch_identity, normalize_product, product_candidates
from sales_manager.research.providers import SearchResult
from sales_manager.research.service import ResearchService
from sales_manager.storage import Store


@pytest.mark.parametrize(('model', 'product'), [
    ('Redmi Buds 5', 'наушники'),
    ('Redmi Pad Pro', 'планшет'),
    ('RedmiBook 16', 'ноутбук'),
    ('Redmi Watch 5', 'умные часы'),
    ('Xiaomi Pad 6', 'планшет'),
    ('POCO Pad', 'планшет'),
    ('Xiaomi Redmi Note 13', 'смартфон'),
    ('Redmi 12C', 'смартфон'),
    ('POCO X7 Pro', 'смартфон'),
])
def test_model_families_match_names_and_market_mentions(model, product):
    assert normalize_product(model) == product
    # Exact electronics models keep their own company/UTP workspace.
    assert branch_identity(model) == 'branch:' + model.casefold()
    text = f'В России за октябрь 2026 года продажи {model} выросли на 20%.'
    assert product_candidates(text) == [product]
    assert [(row['metric'], row['value']) for row in extract_observations(product, text)] == [('sales_growth', 20)]


def test_distinct_redmi_families_in_the_same_report():
    assert product_candidates('Продажи Redmi Buds 5 и Redmi Note 13') == ['наушники', 'смартфон']
    assert product_candidates('Новый каталог Redmi и POCO') == []


@pytest.mark.parametrize(('product', 'form'), [
    ('зубная паста', 'зубной пасты'),
    ('зубная паста', 'зубную пасту'),
    ('станок', 'станков'),
    ('игрушка', 'игрушек'),
    ('коляска', 'колясок'),
    ('палатка', 'палаток'),
    ('розетка', 'розеток'),
    ('тележка', 'тележек'),
    ('упаковочная пленка', 'упаковочных плёнок'),
    ('краски для рисования', 'красок для рисования'),
    ('семена', 'семян'),
])
def test_explicit_inflections_preserve_market_data(product, form):
    text = f'В России за октябрь 2026 года продажи {form} выросли на 20%.'
    assert product in product_candidates(text)
    assert [(row['metric'], row['value']) for row in extract_observations(product, text)] == [('sales_growth', 20)]


@pytest.mark.parametrize('subject', ['кофейни', 'кофейных аппаратов', 'кофемашин'])
def test_coffee_not_inferred_from_related_words(subject):
    text = f'В России за октябрь 2026 года вырос спрос на {subject}.'
    assert 'кофе' not in product_candidates(text)
    assert extract_observations('кофе', text) == []


def test_standalone_coffee_matches_the_market_fact():
    text = 'В России за октябрь 2026 года продажи кофе выросли на 20%.'
    assert product_candidates(text) == ['кофе']
    assert [(row['metric'], row['value']) for row in extract_observations('кофе', text)] == [('sales_growth', 20)]


@pytest.mark.parametrize('places', ['в России и Казахстане', 'в России, в Казахстане', 'в России, Казахстане и Беларуси'])
def test_common_measurement_never_becomes_a_country_measurement(places):
    text = f'За октябрь 2026 года {places} продажи кофе суммарно выросли на 20%.'
    assert extract_observations('кофе', text, country='Россия') == []


def test_separate_country_value_survives_grouped_measurement():
    text = ('За октябрь 2026 года продажи кофе в России и Казахстане выросли на 20%, '
            'а продажи кофе в Беларуси выросли на 15%.')
    assert [(row['country'], row['value']) for row in extract_observations('кофе', text)] == [('Беларусь', 15)]


def test_independent_market_clauses_reach_both_rankings(tmp_path):
    date = datetime.now(timezone.utc)
    russian_fact = f'За {date:%Y-%m} в России продажи кофе выросли на 20%'
    kazakh_fact = 'а в Казахстане продажи кофе выросли на 15%.'
    full_excerpt = f'{russian_fact}, {kazakh_fact}'
    result = SearchResult('https://news.example/coffee', 'Рыночный отчёт', full_excerpt, 'Россия',
                          date.isoformat(), {'official_statistics': True})
    store = Store(tmp_path / 'market.sqlite3')
    try:
        service = ResearchService(store, Mock())
        service._market_evidence(result, lambda _: None)
        for country, value, excerpt in [('Россия', 20, russian_fact), ('Казахстан', 15, kazakh_fact)]:
            row, = service.market_ranking([country])['rows']
            measure, = row['measurements']
            assert (measure['country'], measure['value'], measure['excerpt']) == (country, value, excerpt)
            source, = market_evidence(store, row['id'], [country])
            assert source['excerpts'] == [excerpt]

        # Old revisions saved the entire two-country sentence for each number.
        store.execute("UPDATE market_observations SET excerpt=?, event_key='old:' || id", (full_excerpt,))
        store.execute("UPDATE market_observation_sources SET excerpt=?", (full_excerpt,))
        service._market_evidence(result, lambda _: None)
        row, = service.market_ranking(['Россия', 'Казахстан'])['rows']
        assert {(measure['country'], measure['value'], measure['excerpt']) for measure in row['measurements']} == {
            ('Россия', 20, russian_fact), ('Казахстан', 15, kazakh_fact),
        }
        assert len(row['measurements']) == 2
    finally:
        store.close()
