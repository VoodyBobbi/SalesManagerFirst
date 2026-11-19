"""Regression cases found by the independent project audit, using local facts."""
from datetime import datetime, timezone

import pytest

from sales_manager.research.fit_rules import evaluate_fit
from sales_manager.research.market import extract_observations, market_ranking
from sales_manager.storage import Store, now


@pytest.mark.parametrize('unit', ['килограммов', 'кг', 'тонн', 'литров', 'упаковок', 'метров'])
def test_mass_volume_and_containers_are_not_counted_goods(unit):
    text = f'В России за октябрь 2026 года продано 100 {unit} кофе.'
    facts = extract_observations('кофе', text, 'Россия')
    assert not any(fact['measured'] and fact['unit'] == 'units' for fact in facts)
    assert any(not fact['measured'] for fact in facts)


def test_structured_count_cannot_mislabel_a_mass_quantity():
    text = 'В России за октябрь 2026 года продано 100 килограммов кофе.'
    metadata = {'measurements': [dict(metric='sales', unit='units', value=100,
                                     period='2026-10', country='Россия', excerpt=text)]}
    facts = extract_observations('кофе', text, 'Россия', metadata=metadata)
    assert not any(fact['measured'] for fact in facts)


def test_saved_wrong_count_no_longer_enters_market_ranking(tmp_path):
    store = Store(tmp_path / 'legacy-market.sqlite3')
    try:
        stamp = datetime.now(timezone.utc)
        period = stamp.strftime('%Y-%m')
        text = f'В России за {period} продано 100 килограммов кофе.'
        product = store.ensure_product('кофе')
        source = store.put_source('https://source.ru/report', 'Отчёт', text, stamp.isoformat())
        store.execute('''INSERT INTO market_observations(product_id,source_id,metric,value,unit,
            period,country,published_at,excerpt,measured,event_key,created_at)
            VALUES(?,?,'sales',100,'units',?,'Россия',?,?,1,'old-count',?)''',
                      (product['id'], source, period, stamp.isoformat(), text, now()))
        assert not market_ranking(store, ['Россия'])['rows']
        assert store.one('SELECT COUNT(*) AS n FROM market_observations')['n'] == 1
    finally:
        store.close()


@pytest.mark.parametrize('text', [
    'Мы сервисный центр по ремонту обуви в Москве.',
    'Мы сервисный центр по ремонту автомобилей в России.',
    'Мы сервисный центр по ремонту кофемашин.',
    'Наш сервисный центр помогает клиентам.',
])
def test_generic_service_center_does_not_establish_electronics_trade(text):
    result = evaluate_fit('смартфон', [{'text': text, 'source_id': 1}], {}, 'Россия')
    assert result['status'] == 'INSUFFICIENT_EVIDENCE'
    assert not any(fact['code'] == 'electronics' for fact in result['profile']['activities'])


@pytest.mark.parametrize('text', [
    'Наш сервисный центр ремонтирует смартфоны.',
    'Сервисный центр по ремонту компьютеров и ноутбуков в России.',
])
def test_electronics_service_center_still_has_a_confirmed_activity(text):
    assert evaluate_fit('смартфон', [{'text': text, 'source_id': 1}], {}, 'Россия')['status'] == 'FIT'


@pytest.mark.parametrize('text', [
    'Мы не занимаемся продажей смартфонов.',
    'Продажа смартфонов не осуществляется.',
    'Смартфоны отсутствуют в нашем ассортименте.',
    'В нашем ассортименте нет смартфонов.',
    'Мы не продаём смартфоны в нашем магазине.',
    'Мы не занимаемся оптовой продажей смартфонов.',
    'Смартфоны отсутствуют в продаже.',
    'Продажа электроники не осуществляется.',
])
def test_denied_assortment_is_not_positive_trade_evidence(text):
    assert evaluate_fit('смартфон', [{'text': text, 'source_id': 1}], {}, '')['status'] != 'FIT'


@pytest.mark.parametrize('text', [
    'В нашем ассортименте смартфоны.',
    'Мы продаём не только смартфоны, но и ноутбуки.',
    'У нас нет кафе. В нашем магазине продаём смартфоны.',
])
def test_positive_trade_and_unrelated_negative_remain_supported(text):
    assert evaluate_fit('смартфон', [{'text': text, 'source_id': 1}], {}, '')['status'] == 'FIT'
