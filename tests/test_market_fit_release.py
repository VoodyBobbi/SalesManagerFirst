"""Regression cases for product forms, buyer evidence and market ordering."""
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from sales_manager.research.fit_rules import evaluate_fit, semantic_evidence_allowed
from sales_manager.research.market import extract_observations, market_ranking
from sales_manager.research.normalization import matches_product, normalize_product, product_candidates
from sales_manager.research.providers import SearchResult
from sales_manager.research.safe_http import FetchError
from sales_manager.research.service import ResearchService
from sales_manager.storage import Store


def pages(text):
    return [{'url': 'https://buyer.ru/about', 'source_id': 1, 'text': text}]


@pytest.mark.parametrize('canonical,form', [
    ('стол', 'столы'), ('стол', 'столов'), ('стол', 'столами'),
    ('стул', 'стулья'), ('стул', 'стульев'), ('стул', 'стульями'),
    ('шкаф', 'шкафы'), ('шкаф', 'шкафов'), ('шкаф', 'шкафами'),
])
def test_short_furniture_inflections_are_found_in_market(canonical, form):
    text = f'В России за октябрь 2026 года продажи товара {form} выросли на 20%.'
    assert normalize_product(form) == canonical
    assert canonical in product_candidates(text)
    assert matches_product(canonical, form)
    assert extract_observations(canonical, text)[0]['value'] == 20


@pytest.mark.parametrize('canonical,unrelated', [
    ('стол', 'столица'), ('стол', 'столовая'), ('стул', 'стульчак'),
    ('шкаф', 'шкафчик'), ('шкаф', 'шкафной'),
])
def test_short_furniture_does_not_match_longer_words(canonical, unrelated):
    assert not matches_product(canonical, unrelated)
    assert canonical not in product_candidates(unrelated)


@pytest.mark.parametrize('product,text', [
    ('офисное кресло', 'Магазин мебели. В каталоге продаём офисные кресла.'),
    ('картонная коробка', 'Мы производим мебель. В каталоге продаём картонные коробки.'),
    ('стул', 'Гостиница. В каталоге продаём стулья.'),
])
def test_exact_assortment_takes_priority_over_weak_activity(product, text):
    result = evaluate_fit(product, pages(text), {}, 'Россия')
    assert result['status'] == 'FIT'
    assert result['profile']['rule'] == 'documented_assortment'


@pytest.mark.parametrize('product,text', [
    ('кофе', 'Наш партнёр — ресторан «Берёзка». Мы оказываем бухгалтерские услуги.'),
    ('смартфон', 'Наш партнер — магазин электроники «Техномир». Мы занимаемся аудитом.'),
    ('офисное кресло', 'Наш клиент — магазин офисной мебели «Офис». Мы занимаемся аудитом.'),
    ('кофе', 'Наши клиенты:\nГостиница «Орион»'),
    ('кофе', 'Вчера открылась гостиница «Орион». Мы предоставляем бухгалтерские услуги.'),
    ('стул', 'Наш клиент — магазин мебели. В его каталоге продают стулья.'),
])
def test_customer_or_partner_activity_is_not_company_activity(product, text):
    assert evaluate_fit(product, pages(text), {}, 'Россия')['status'] != 'FIT'


def test_own_activity_remains_usable_after_partner_example():
    text = 'Наш партнер — магазин мебели. Наша компания — магазин электроники.'
    assert evaluate_fit('смартфон', pages(text), {}, 'Россия')['status'] == 'FIT'


def test_ai_can_use_own_activity_outside_local_fit_vocabulary():
    quote = 'Печатаем книги и учебные пособия для издательств.'
    assert evaluate_fit('принтер', pages(quote), {}, 'Россия')['status'] == 'INSUFFICIENT_EVIDENCE'
    assert semantic_evidence_allowed('принтер', pages(quote), quote)


@pytest.mark.parametrize('text,quote', [
    ('Наш клиент — типография. Печатаем книги.', 'Наш клиент — типография.'),
    ('Наш партнер — магазин электроники «Техномир».', 'Магазин электроники «Техномир».'),
    ('Наши клиенты:\nПечатаем книги и учебные пособия.', 'Печатаем книги и учебные пособия.'),
    ('Добро пожаловать на наш сайт!', 'Добро пожаловать на наш сайт!'),
    ('Мы печатаем книги.', 'Мы печатаем газеты.'),
])
def test_ai_rejects_third_party_generic_or_missing_quotes(text, quote):
    assert not semantic_evidence_allowed('принтер', pages(text), quote)


@pytest.mark.parametrize('refusal', [
    'Мы не закупаем принтеры.', 'Компания ликвидирована.',
    'Принтеры отсутствуют в ассортименте.',
])
def test_ai_checks_explicit_refusals_outside_its_quote(refusal):
    quote = 'Печатаем книги и учебные пособия для издательств.'
    assert not semantic_evidence_allowed('принтер', pages(quote) + pages(refusal), quote)


def test_abbreviated_year_does_not_inherit_previous_products_period():
    text = ('В России за январь 2025 года продажи ноутбуков выросли на 2%. '
            'В России за октябрь 2026 г. продажи смартфонов выросли на 20%.')
    row = extract_observations('смартфон', text)[0]
    assert row['period'] == '2026-10' and row['country'] == 'Россия'
    assert 'октябрь 2026 г.' in row['excerpt']


def test_abbreviated_city_keeps_fact_country_and_date():
    text = 'В России в г. Москва за октябрь 2026 года продажи смартфонов выросли на 20%.'
    row = extract_observations('смартфон', text)[0]
    assert row['excerpt'] == text
    assert (row['period'], row['country'], row['value']) == ('2026-10', 'Россия', 20)


@pytest.fixture
def market_service(tmp_path):
    store = Store(tmp_path / 'market.sqlite3')
    service = ResearchService(store, Mock(get=lambda name, default='': default))
    service._page = Mock(side_effect=FetchError('Isolated test'))
    yield service
    store.close()


def add_report(service, text, url, published='2026-11-16T12:00:00+00:00'):
    service._market_evidence(SearchResult(url, text, '', 'Россия', published), lambda _: None)


def ranked(service):
    return market_ranking(service.store, ['Россия'], as_of=datetime(2026, 11, 17, tzinfo=timezone.utc))


def test_independent_publications_rank_before_alphabet_without_demand_percent(market_service):
    add_report(market_service, 'В России в 2026 году спрос на кофе растёт.', 'https://coffee.example/a')
    add_report(market_service, 'В России в 2026 году спрос на смартфоны растёт.', 'https://mobile.example/a')
    add_report(market_service, 'В России в 2026 году зафиксирован рост спроса на смартфоны.', 'https://analyst.example/b')
    result = ranked(market_service)
    assert [row['name'] for row in result['rows']] == ['смартфон', 'кофе']
    assert all(row['demand_percent'] is None for row in result['rows'])
    assert 'независимые публикации' in result['caption']


def test_reprints_and_same_publisher_cannot_inflate_independent_publications(market_service):
    phone = 'В России в 2026 году спрос на смартфоны растёт.'
    add_report(market_service, phone, 'https://mobile.example/a')
    for number in range(5):
        add_report(market_service, phone, f'https://reprint{number}.example/a')
    add_report(market_service, 'В России в 2026 году зафиксирован рост спроса на смартфоны.', 'https://mobile.example/b')
    add_report(market_service, 'В России в 2026 году спрос на кофе растёт.', 'https://coffee.example/a')
    add_report(market_service, 'В России в 2026 году зафиксирован рост спроса на кофе.', 'https://analyst.example/b')
    assert [row['name'] for row in ranked(market_service)['rows']] == ['кофе', 'смартфон']


def test_publication_freshness_breaks_equal_independent_publisher_count(market_service):
    add_report(market_service, 'В России в 2026 году спрос на кофе растёт.', 'https://coffee.example/a', '2026-11-10T12:00:00+00:00')
    add_report(market_service, 'В России в 2026 году спрос на смартфоны растёт.', 'https://mobile.example/a', '2026-11-16T12:00:00+00:00')
    assert [row['name'] for row in ranked(market_service)['rows']] == ['смартфон', 'кофе']
