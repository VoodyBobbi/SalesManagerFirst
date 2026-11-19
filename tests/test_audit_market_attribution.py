from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from sales_manager.research.market import extract_observations, market_ranking
from sales_manager.research.normalization import product_candidates
from sales_manager.research.providers import SearchResult
from sales_manager.research.service import ResearchService
from sales_manager.storage import Store


@pytest.fixture
def market():
    store = Store(':memory:')
    try:
        yield ResearchService(store, Mock())
    finally:
        store.close()


def publish(service, text, published_at):
    result = SearchResult('https://news.example/report', 'Рыночный отчёт', text, 'Россия',
                          published_at, {'official_statistics': True})
    service._market_evidence(result, lambda _: None)


@pytest.mark.parametrize('connector', [', но', ' и'])
def test_two_metric_types_never_create_a_shared_scale(market, connector):
    stamp = datetime.now(timezone.utc)
    text = f'В России за {stamp:%Y-%m} продажи кофе выросли на 8%{connector} объём заказов чая вырос на 60%.'
    publish(market, text, stamp.isoformat())
    rows = market_ranking(market.store, ['Россия'], as_of=stamp)['rows']
    assert {r['name']: [(m['metric'], m['value']) for m in r['measurements']] for r in rows} == {
        'кофе': [('sales_growth', 8)], 'чай': [('orders_growth', 60)],
    }
    assert all(r['demand_percent'] is None for r in rows)


@pytest.mark.parametrize('claim,expected', [
    ('продажи кофе выросли на 20% до 40% рынка', [20]),
    ('доля продаж кофе выросла с 20% до 30%', []),
    ('доля продаж кофе выросла на 15 процентных пунктов', []),
    ('рост продаж кофе составил 12%', [12]),
    ('на 12% выросли продажи кофе', [12]),
])
def test_only_actual_percent_change_of_sales_is_rankable(claim, expected):
    text = f'В России за 2026-11 {claim}.'
    facts = extract_observations('кофе', text)
    assert [(fact['metric'], fact['value']) for fact in facts] == [
        ('sales_growth', value) for value in expected]


@pytest.mark.parametrize('claim', [
    'всего продано 100 смартфонов и ноутбуков',
    'всего продано 100 смартфонов, ноутбуков и планшетов',
    'совокупные продажи смартфонов и ноутбуков выросли на 20%',
])
def test_joint_quantity_is_never_assigned_to_one_product(claim):
    text = f'В России за 2026-11 {claim}.'
    for name in product_candidates(text):
        assert all(not fact['measured'] for fact in extract_observations(name, text))


def test_individually_counted_goods_keep_their_own_values():
    text = 'В России за 2026-11 продано 500 смартфонов и 300 ноутбуков.'
    assert [(o['metric'], o['value']) for o in extract_observations('смартфон', text)] == [('sales', 500)]
    assert [(o['metric'], o['value']) for o in extract_observations('ноутбук', text)] == [('sales', 300)]


@pytest.mark.parametrize('claim', [
    'продажи чехлов для смартфонов выросли на 20%',
    'продали 100 чехлов для новых смартфонов',
    'спрос на аксессуары к смартфонам вырос',
    'продажи смартфонных чехлов выросли на 20%',
])
def test_accessory_demand_is_not_device_demand(claim):
    assert extract_observations('смартфон', f'В России за 2026-11 {claim}.') == []


def test_qualifier_and_separate_words_do_not_create_two_products():
    text = 'В России за 2026-11 продажи плёнки для теплиц выросли на 20% по данным упаковочной отрасли.'
    assert 'упаковочная пленка' not in product_candidates(text)
    assert extract_observations('теплица', text) == []
    assert extract_observations('упаковочная пленка', text) == []


def test_coordinated_accessory_targets_are_not_goods_sold():
    text = 'В России за 2026-11 выросли продажи чехлов для смартфонов, ноутбуков и планшетов.'
    assert product_candidates(text) == ['ноутбук', 'планшет', 'смартфон']
    assert all(not extract_observations(product, text) for product in product_candidates(text))


def test_independent_goods_after_qualified_product_are_kept():
    text = 'В России за 2026-11 продали кофе для офисов, чай и молоко.'
    assert all(extract_observations(product, text) for product in ('кофе', 'чай', 'молоко'))


@pytest.mark.parametrize('phrase,expected', [
    ('продажи кофе из Казахстана выросли на 20%', []),
    ('продажи российского кофе выросли на 20%', []),
    ('продажи выращенного в Казахстане кофе выросли на 20%', []),
    ('продажи кофе производства Казахстана выросли на 20%', []),
    ('продажи кофе из России и Казахстана выросли на 20%', []),
    ('продажи кофе из Казахстана в России выросли на 20%', ['Россия']),
    ('продажи выращенного в Казахстане кофе в России выросли на 20%', ['Россия']),
    ('российские покупатели увеличили покупки кофе из Казахстана на 20%', ['Россия']),
])
def test_origin_is_not_the_place_of_sale(phrase, expected):
    facts = extract_observations('кофе', f'За 2026-11 {phrase}.')
    assert [fact['country'] for fact in facts] == expected


def test_old_share_as_sales_is_revalidated_without_deleting_the_record(market):
    stamp = datetime.now(timezone.utc)
    text = f'В России за {stamp:%Y-%m} доля продаж кофе выросла с 20% до 30%.'
    store = market.store
    product = store.ensure_product('кофе')
    source = store.put_source('https://news.example/old', 'Старый отчёт', text, stamp.isoformat())
    store.execute('''INSERT INTO market_observations
        (product_id,source_id,metric,value,unit,period,country,published_at,excerpt,measured,event_key,created_at)
        VALUES(?,?,'sales_growth',30,'percent',?,'Россия',?,?,1,'old-share-as-growth',?)''',
        (product['id'], source, f'{stamp:%Y-%m}', stamp.isoformat(), text, stamp.isoformat()))
    assert market_ranking(store, ['Россия'], as_of=stamp)['rows'] == []
    assert store.one("SELECT value FROM market_observations WHERE event_key='old-share-as-growth'") == {'value': 30}


@pytest.mark.parametrize(('tail', 'kazakhstan'), [
    ('а в Казахстане — на 15%.', 15),
    ('Казахстан — 15%.', 15),
    ('а в Казахстане продажи кофе снизились.', None),
    ('а в Казахстане годом ранее продажи кофе выросли на 15%.', None),
])
def test_separate_country_clauses_keep_only_grounded_amounts(market, tail, kazakhstan):
    stamp = datetime.now(timezone.utc)
    report = f'За {stamp:%Y-%m} в России продажи кофе выросли на 20%, {tail}'
    publish(market, report, stamp.isoformat())
    russian, = market_ranking(market.store, ['Россия'], as_of=stamp)['rows']
    assert [measure['value'] for measure in russian['measurements']] == [20]
    other = market_ranking(market.store, ['Казахстан'], as_of=stamp)['rows']
    if kazakhstan is None:
        assert other == []
    else:
        measure, = other[0]['measurements']
        assert (measure['value'], measure['excerpt']) == (kazakhstan, report)


@pytest.mark.parametrize('tail', ['отдельно в России продажи кофе выросли на 12%', 'а в России — на 12%'])
def test_combined_market_number_does_not_enter_separate_country_ranking(market, tail):
    stamp = datetime.now(timezone.utc)
    report = f'Россия и Казахстан: за {stamp:%Y-%m} суммарные продажи кофе выросли на 20%, {tail}.'
    publish(market, report, stamp.isoformat())
    russian, = market_ranking(market.store, ['Россия'], as_of=stamp)['rows']
    assert [measure['value'] for measure in russian['measurements']] == [12]
    assert market_ranking(market.store, ['Казахстан'], as_of=stamp)['rows'] == []


def test_country_ellipsis_cannot_inherit_from_unrelated_measurements():
    report = ('За 2026-11 в России продажи кофе выросли на 20%, продажи чая — на 30%, '
              'а в Казахстане — на 15%.')
    assert all(o['country'] != 'Казахстан' for product in ('кофе', 'чай')
               for o in extract_observations(product, report))


def test_year_range_spacing_does_not_mean_decline():
    reports = [f'В России за {years} годы продажи кофе выросли на 20%.'
               for years in ('2025 – 2026', '2025–2026')]
    for report in reports:
        fact, = extract_observations('кофе', report)
        assert (fact['period'], fact['value']) == ('2025/2026', 20)
    decline = 'В России за 2025 – 2026 изменение продаж кофе составило −10%.'
    assert extract_observations('кофе', decline) == []


@pytest.mark.parametrize(('report', 'qualified'), [
    ('продажи кофе выросли на 20%, а цены на чай выросли на 15%', 'кофе'),
    ('доля продаж кофе достигла 30%, а продажи чая выросли на 20%', 'чай'),
])
def test_price_and_share_cannot_borrow_demand_from_another_product(market, report, qualified):
    stamp = datetime.now(timezone.utc)
    publish(market, f'В России за {stamp:%Y-%m} {report}.', stamp.isoformat())
    row, = market_ranking(market.store, ['Россия'], as_of=stamp)['rows']
    assert (row['name'], row['measurements'][0]['value']) == (qualified, 20)


@pytest.mark.parametrize('claim', [
    'всего продано 100 смартфонов и чехлов',
    'суммарные продажи чехлов и смартфонов выросли на 20%',
    'совокупные продажи смартфонов и дополнительных чехлов выросли на 20%',
])
def test_aggregate_with_unlisted_product_does_not_become_phone_measurement(claim):
    facts = extract_observations('смартфон', f'В России за 2026-11 {claim}.')
    assert not any(fact['measured'] for fact in facts)


def test_individual_phone_count_survives_counted_unlisted_goods():
    fact, = extract_observations('смартфон', 'В России за 2026-11 продано 100 смартфонов и 30 чехлов.')
    assert (fact['metric'], fact['value']) == ('sales', 100)


@pytest.mark.parametrize('legacy_key', [False, True])
def test_reprinted_body_with_new_headline_keeps_one_independent_report(market, legacy_key):
    stamp = datetime.now(timezone.utc)
    tea = 'В России заметно вырос спрос на чай.'
    coffee = 'В России заметно вырос спрос на кофе.'
    first = SearchResult('https://one.example/tea', 'Первый заголовок', tea, 'Россия', stamp.isoformat())
    copy = SearchResult('https://two.example/tea', 'Другой заголовок', tea, 'Россия', stamp.isoformat())
    independent = SearchResult('https://three.example/coffee', 'Спрос на кофе', coffee, 'Россия', stamp.isoformat())
    market._page = Mock(side_effect=lambda url, _: {'market_text':
        {result.url: result.title + '\n' + result.snippet for result in (first, copy, independent)}[url]})
    market._market_evidence(first, lambda _: None)
    if legacy_key:
        market.store.execute("UPDATE market_observations SET event_key='old-headline-key'")
    for result in (copy, independent):
        market._market_evidence(result, lambda _: None)
    rows = market_ranking(market.store, ['Россия'], as_of=stamp)['rows']
    assert [(row['name'], row['count']) for row in rows] == [('кофе', 1), ('чай', 2)]
    saved = market.store.one('''SELECT COUNT(*) n FROM market_observations o
        JOIN products p ON p.id=o.product_id WHERE p.name='чай' ''')
    assert saved == {'n': 2 if legacy_key else 1}
