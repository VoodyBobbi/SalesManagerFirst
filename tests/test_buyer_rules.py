"""Buyer rules operate on saved company passages, without web or model calls."""
import json
from unittest.mock import Mock

import pytest

from sales_manager.ai import AIService
from sales_manager.research.fit_rules import evaluate_fit
from sales_manager.research.normalization import buyer_segments, normalize_product, product_candidates
from sales_manager.research.providers import SearchResult
from sales_manager.research.service import ResearchService
from sales_manager.storage import Store


class Keys:
    def __init__(self, key=''):
        self.key = key

    def get(self, name, default=''):
        return self.key if name == 'gigachat_key' else default


@pytest.fixture
def research(tmp_path):
    store = Store(tmp_path / 'buyers.sqlite3')
    service = ResearchService(store, Keys())
    store.set_setting('search.page_limit', 1)
    yield service
    store.close()


def saved_page(store, text, url='https://buyer.ru/'):
    source_id = store.put_source(url, 'Профиль компании', text)
    return {'url': url, 'title': 'Компания', 'name': 'Компания', 'text': text,
            'source_id': source_id, 'emails': ['office@buyer.ru'], 'phones': [],
            'links': [], 'legal_id': ''}


@pytest.mark.parametrize('product,text', [
    ('Офисные кресла', 'Магазин офисной мебели. Поставляем мебель для офисов по России.'),
    ('Автомобильные фильтры', 'Магазин автозапчастей. Наша компания работает в России.'),
    ('Бумажные полотенца', 'Клининговая компания. Выполняем уборку офисов и помещений в России.'),
    ('Кофе', 'Наше кафе готовит завтраки и обеды для посетителей в России.'),
])
def test_activity_relation_fits_without_product_mention(research, product, text):
    page = saved_page(research.store, text)
    assert normalize_product(product) not in product_candidates(text)
    decision = evaluate_fit(product, [page], {}, 'Россия')
    assert decision['status'] == 'FIT'
    assert not decision['potential']
    fact = decision['profile']['activities'][0]
    assert fact['source_id'] == page['source_id']
    assert fact['quote'] in research.store.one('SELECT text FROM sources WHERE id=?', (page['source_id'],))['text']
    assert 'Связь:' in decision['reason'] and 'Гипотеза:' in decision['reason']


def test_boxes_manufacturer_is_concrete_pending_candidate(research):
    page = saved_page(research.store, 'Мы производим товары для дома и поставляем продукцию магазинам России.')
    decision = evaluate_fit('Упаковочные коробки', [page], {}, 'Россия')
    assert decision['status'] == 'INSUFFICIENT_EVIDENCE' and decision['potential']
    assert decision['profile']['activities'][0]['code'] == 'manufacturer'
    assert 'упаковка' in decision['profile']['relationship']


@pytest.mark.parametrize('text', [
    'Добро пожаловать на наш сайт. Контакты и адрес в России.',
    'Мы разрабатываем сайты для магазинов офисной мебели в России.',
    'Публикуем новости рынка офисной мебели в России.',
])
def test_unknown_or_third_party_industry_is_not_confirmed_buyer(research, text):
    page = saved_page(research.store, text)
    decision = evaluate_fit('Офисные кресла', [page], {}, 'Россия')
    assert decision['status'] == 'INSUFFICIENT_EVIDENCE'
    assert not decision['potential']


def test_absent_product_not_negative_but_explicit_refusal_is(research):
    unknown = saved_page(research.store, 'Наша компания оказывает услуги в России.')
    assert evaluate_fit('Офисные кресла', [unknown], {}, 'Россия')['status'] != 'NOT_FIT'
    refusal = saved_page(research.store, 'Мы не закупаем офисные кресла и не принимаем предложения по этому направлению.')
    result = evaluate_fit('Офисные кресла', [refusal], {}, 'Россия')
    assert result['status'] == 'NOT_FIT'
    assert refusal['text'] in result['reason']


def test_country_is_not_invented_from_search_context(research):
    page = saved_page(research.store, 'Магазин офисной мебели.', url='https://buyer.example/')
    result = evaluate_fit('Офисные кресла', [page], {}, 'Россия')
    assert result['status'] == 'FIT' and result['profile']['country'] == ''


def test_customer_industries_in_furniture_catalog_not_company_activity(research):
    page = saved_page(research.store, 'Магазин офисной мебели. Мебель для гостиниц и ресторанов. '
                      'Наши клиенты — кафе. Бренды и фабрики мебели. Россия.')
    assert evaluate_fit('Офисные кресла', [page], {}, 'Россия')['status'] == 'FIT'
    coffee = evaluate_fit('Кофе', [page], {}, 'Россия')
    assert coffee['status'] == 'INSUFFICIENT_EVIDENCE' and not coffee['potential']


def test_office_queries_include_resale_wholesale_and_workplace_supply():
    segments = buyer_segments('Офисные кресла')
    assert segments == buyer_segments('офисное кресло')
    assert any('офисной мебели' in s for s in segments)
    assert any('оптов' in s for s in segments)
    assert any('оснащение' in s or 'рабочих мест' in s for s in segments)
    assert len(segments) <= 5 and len(segments) == len(set(segments))
    assert any('производитель' in s for s in buyer_segments('Упаковочные коробки'))


def test_obvious_relation_never_calls_gigachat_even_with_key(research):
    research.secrets.key = 'controlled-key'
    research.ai = Mock()
    page = saved_page(research.store, 'Клининговая компания. Уборка офисов в России.')
    research._page = lambda *args: page
    product = research.store.ensure_product('Бумажные полотенца', normalize_product('Бумажные полотенца'))
    research._company(product['id'], SearchResult(page['url'], country='Россия'), False, lambda _: None)
    row = research.store.one('SELECT * FROM company_fit')
    assert row['status'] == 'FIT'
    research.ai.assess_fit.assert_not_called()
    assert research.store.query("SELECT * FROM usage_log WHERE provider='gigachat'") == []


@pytest.mark.parametrize('mode', ['success', 'unavailable', 'invented_quote', 'no_key'])
def test_ambiguous_company_has_one_semantic_attempt_and_preserves_evidence(research, mode):
    research.secrets.key = '' if mode == 'no_key' else 'controlled-key'
    text = 'Мы производим товары для дома и поставляем продукцию магазинам России.'
    page = saved_page(research.store, text)
    research._page = lambda *args: page
    product = research.store.ensure_product('Упаковочные коробки', normalize_product('Упаковочные коробки'))
    ai = AIService(research.store, research.secrets)
    calls = []
    def completion(*args):
        calls.append(args)
        if mode == 'unavailable':
            raise RuntimeError('GigaChat недоступен')
        quote = 'Компания закупает коробки ежедневно.' if mode == 'invented_quote' else text
        value = {'status': 'FIT', 'reason': 'Гипотеза: упаковка подходит для отгрузки продукции.',
                 'source_ids': [page['source_id']], 'evidence_quote': quote}
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(value)}}]}
    ai._completion = completion
    research.ai = ai
    for _ in range(2):
        research._company(product['id'], SearchResult(page['url'], country='Россия'), False, lambda _: None)
    row = research.store.one('SELECT * FROM company_fit')
    assert len(calls) == (0 if mode == 'no_key' else 1)
    # Local rules keep manufacturing as a candidate; semantic review may
    # establish a commercial hypothesis from the saved activity quotation.
    assert row['status'] == ('FIT' if mode == 'success' else 'INSUFFICIENT_EVIDENCE')
    assert bool(row['potential']) == (mode != 'success')
    profile = json.loads(row['activity_profile'])
    assert profile['activities'][0]['quote'] == text
    assert research.store.one('SELECT COUNT(*) n FROM company_contacts')['n'] == 1
    if mode == 'success':
        ai.assess_fit(product['id'], research.store.one('SELECT id FROM companies')['id'])
        assert len(calls) == 1


def test_telephone_only_buyer_remains_saved_without_email(research):
    page = saved_page(research.store, 'Магазин офисной мебели. Россия.')
    page['emails'], page['phones'] = [], ['+79991234567']
    research._page = lambda *args: page
    product = research.store.ensure_product('Офисные кресла', normalize_product('Офисные кресла'))
    research._company(product['id'], SearchResult(page['url'], country='Россия'), False, lambda _: None)
    assert research.store.one('SELECT status FROM company_fit')['status'] == 'FIT'
    assert research.store.one('SELECT kind FROM company_contacts')['kind'] == 'phone'
