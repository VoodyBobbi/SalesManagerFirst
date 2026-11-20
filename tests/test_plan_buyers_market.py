"""Regressions for the buyers/market plan: fit, branches, marketplaces, mail."""
import json
from datetime import date, datetime, timezone
from unittest.mock import Mock

import pytest

from sales_manager.core.contacts import email_purpose
from sales_manager.core.offer_fields import MAX_SUBJECT_LENGTH
from sales_manager.research.extraction import is_marketplace, marketplace_seller_hint, parse_page
from sales_manager.research.fit_rules import evaluate_fit
from sales_manager.research.market import MARKET_START, extract_observations, market_ranking
from sales_manager.research.normalization import (CONSUMER_ELECTRONICS, IT_EQUIPMENT, branch_identity,
                                                  normalize_product)
from sales_manager.research.providers import SearchResult
from sales_manager.research.service import ResearchService
from sales_manager.storage import Store, now
from sales_manager.storage.migrations import MIGRATIONS, SCHEMA, migrate


@pytest.fixture
def research(tmp_path):
    store = Store(tmp_path / 'plan.sqlite3')
    store.set_setting('search.page_limit', 1)
    service = ResearchService(store, Mock(get=Mock(return_value='')))
    yield service
    store.close()


def test_unknown_product_stays_insufficient():
    page = {'text': 'Добро пожаловать на сайт компании в России.', 'source_id': 1}
    assert evaluate_fit('неизвестный гаджет xyz', [page], {}, 'Россия')['status'] == 'INSUFFICIENT_EVIDENCE'


def test_confirmed_own_use_fits_servers_and_chairs():
    server = {'text': 'Мы эксплуатируем серверы собственной площадки в дата-центре.', 'source_id': 1}
    chair = {'text': 'Оснащаем рабочие места офисными креслами для сотрудников.', 'source_id': 1}
    assert evaluate_fit('сервер', [server], {}, 'Россия')['status'] == 'FIT'
    assert evaluate_fit('офисное кресло', [chair], {}, 'Россия')['status'] == 'FIT'


def test_generic_office_and_it_phrases_need_review():
    office = {'text': 'У нас современный офис и рабочие места для сотрудников.', 'source_id': 1}
    it = {'text': 'Компания развивает ИТ-инфраструктуру и цифровые сервисы.', 'source_id': 1}
    assert evaluate_fit('кофе', [office], {}, 'Россия')['status'] == 'INSUFFICIENT_EVIDENCE'
    assert evaluate_fit('сервер', [it], {}, 'Россия')['status'] == 'INSUFFICIENT_EVIDENCE'


def test_coffee_staff_beverages_rule_is_product_specific():
    page = {'text': 'Обеспечиваем сотрудников кофе и чаем в офисе каждый день.', 'source_id': 1}
    assert evaluate_fit('кофе', [page], {}, 'Россия')['status'] == 'FIT'
    assert evaluate_fit('сервер', [page], {}, 'Россия')['status'] == 'INSUFFICIENT_EVIDENCE'


def test_incidental_mention_is_not_fit():
    page = {'text': 'Рядом находится кафе. Мы продаём компьютеры.', 'source_id': 1}
    assert evaluate_fit('кофе', [page], {}, 'Россия')['status'] == 'INSUFFICIENT_EVIDENCE'


def test_electronics_categories_are_split():
    assert normalize_product('умные часы') in __import__(
        'sales_manager.research.normalization', fromlist=['CATEGORIES']).CATEGORIES[CONSUMER_ELECTRONICS]
    assert normalize_product('сервер') in __import__(
        'sales_manager.research.normalization', fromlist=['CATEGORIES']).CATEGORIES[IT_EQUIPMENT]
    models = ['iPhone 16', 'Samsung Galaxy S25', 'Redmi Buds 5', 'MacBook Pro 14', 'iPad Air']
    keys = {branch_identity(name) for name in models}
    assert len(keys) == 5
    assert all(key.startswith('branch:') for key in keys)


def test_marketplace_is_hint_only_and_excluded_from_fit(research):
    assert is_marketplace('https://www.ozon.ru/product/1')
    assert marketplace_seller_hint('Товар', 'Продавец: Ромашка Трейд', 'https://www.wildberries.ru/1') == 'Ромашка Трейд'
    pid = research.store.ensure_product('кофе')['id']
    calls = []
    research._enqueue = lambda run_id, payload: calls.append(payload)
    research._handle(1, pid, {
        'phase': 'company',
        'result': SearchResult('https://www.ozon.ru/product/1', 'Кофе', 'Продавец: Альфа Кофе', 'Россия').to_dict(),
        'manual': False,
    }, {}, lambda _: None)
    assert calls and calls[0]['phase'] == 'search'
    assert 'Альфа Кофе' in calls[0]['query']
    assert research.store.query('SELECT * FROM companies') == []


def test_three_branches_keep_separate_cards(research):
    pid = research.store.ensure_product('кофе')['id']
    html = '''
    <title>Сеть Альфа</title>
    <script type="application/ld+json">
    {"@type":"Organization","name":"Альфа",
     "department":[
       {"@type":"LocalBusiness","name":"Филиал Север","address":{"@type":"PostalAddress","addressLocality":"Москва","streetAddress":"Северная 1"},
        "email":"north@alpha.ru"},
       {"@type":"LocalBusiness","name":"Филиал Юг","address":{"@type":"PostalAddress","addressLocality":"Сочи","streetAddress":"Южная 2"},
        "email":"south@alpha.ru"},
       {"@type":"LocalBusiness","name":"Филиал Восток","address":{"@type":"PostalAddress","addressLocality":"Казань","streetAddress":"Восточная 3"},
        "email":"east@alpha.ru"}
     ]}
    </script>
    <p>Наше кафе работает в трёх городах.</p>
    '''
    page = parse_page(html, 'https://alpha.ru/', 'Россия')
    page['source_id'] = research.store.put_source('https://alpha.ru/', page['title'], page['text'])
    research._page = Mock(return_value=page)
    research._company(pid, SearchResult('https://alpha.ru/', country='Россия'), False, lambda _: None)
    rows = research.store.query('SELECT branch_name, address FROM companies ORDER BY id')
    assert len(rows) >= 3
    assert len({row['branch_name'] or row['address'] for row in rows if row['branch_name'] or row['address']}) >= 3


def test_hq_email_marked_and_subject_limit():
    assert email_purpose('info@buyer.ru')[1] == 'Общий'
    assert MAX_SUBJECT_LENGTH == 180


def test_market_window_stays_open_after_2026_and_accepts_partial_period(research):
    assert MARKET_START == date(2025, 1, 1)
    text = 'В России за январь–март 2027 продажи кофе выросли на 12%.'
    rows = extract_observations('кофе', text, 'Россия', '2027-02-01')
    assert rows and rows[0]['period']
    source = research.store.put_source('https://market.example/a', 'Отчёт', text, '2027-02-01')
    product = research.store.ensure_product('кофе')
    for item in rows:
        research.store.execute(
            'INSERT INTO market_observations(product_id,source_id,metric,value,unit,period,country,published_at,excerpt,measured,event_key,created_at) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (product['id'], source, item['metric'], item['value'], item['unit'], item['period'], item['country'],
             item.get('published_at', ''), item['excerpt'], int(item.get('measured', 1)), item['event_key'], now()))
        research.store.execute(
            'INSERT INTO market_observation_sources(observation_id,source_id,excerpt,published_at) VALUES(?,?,?,?)',
            (research.store.one('SELECT id FROM market_observations ORDER BY id DESC')['id'], source,
             item['excerpt'], item.get('published_at', '')))
    assert not market_ranking(research.store, ['Россия'], as_of=datetime(2027, 1, 31, tzinfo=timezone.utc))['rows']
    ranking = market_ranking(research.store, ['Россия'], as_of=datetime(2027, 2, 1, tzinfo=timezone.utc))
    assert json.loads(json.dumps(ranking))['latest_publication'] == '2027-02-01'
    assert any(row['name'] == 'кофе' for row in ranking['rows'])


def test_market_source_parse_cache_is_content_keyed_and_numbers_use_russian_format(monkeypatch):
    from sales_manager.research import market

    parser = Mock(return_value=[])
    monkeypatch.setattr(market, 'extract_observations', parser)
    try:
        for content in ('оригинал', 'оригинал', 'исправленная публикация'):
            market._parsed_market_text('кофе', content, 'Россия', '2027-02-01')
        assert parser.call_count == 2
        assert market._format_number(1234.5) == '1 234,5'
    finally:
        market._parsed_market_text.cache_clear()


def test_sqlite_upgrade_preserves_ids_permissions_offers_and_mail(tmp_path):
    path = tmp_path / 'legacy-v5.sqlite3'
    connection = __import__('sqlite3').connect(path)
    scripts = [MIGRATIONS[version] for version in range(1, 6)]
    connection.executescript('\n'.join(scripts) + '\nPRAGMA user_version=5;')
    stamp = now()
    connection.execute("INSERT INTO products(id,name,normalized,created_at) VALUES(7,'кофе','кофе',?)", (stamp,))
    connection.execute("INSERT INTO companies(id,name,domain,url,country,legal_id,manual,created_at) "
                       "VALUES(11,'Альфа','alpha.ru','https://alpha.ru/','Россия','',0,?)", (stamp,))
    connection.execute("INSERT INTO company_fit(product_id,company_id,status,reason,source_ids,manual,potential,activity_profile,fit_key,checked_at) "
                       "VALUES(7,11,'FIT','Вручную','[]',1,0,'{}','manual:keep',?)", (stamp,))
    connection.execute("INSERT INTO company_contacts(company_id,kind,value,checked_at) VALUES(11,'email','info@alpha.ru',?)", (stamp,))
    connection.execute("INSERT INTO permissions(company_id,email,basis,evidence,created_at) VALUES(11,'info@alpha.ru','Согласие','Письмо',?)", (stamp,))
    connection.execute("INSERT INTO offers(id,product_id,company_id,subject,body,approved,created_at,updated_at) "
                       "VALUES(21,7,11,'Тема','Текст УТП',1,?,?)", (stamp, stamp))
    connection.execute("INSERT INTO outbound_attempts(id,offer_id,idempotency_key,message_id,recipient,status,created_at,updated_at) "
                       "VALUES(31,21,'key','<msg@test>','info@alpha.ru','sent',?,?)", (stamp, stamp))
    connection.execute("INSERT INTO mail_messages(product_id,company_id,message_id,direction,sender,recipient,subject,body,sent_at,is_reply) "
                       "VALUES(7,11,'<msg@test>','outgoing','me@mail.ru','info@alpha.ru','Тема','Текст УТП',?,0)", (stamp,))
    connection.execute("INSERT INTO mail_messages(product_id,company_id,message_id,in_reply_to,direction,sender,recipient,subject,body,sent_at,is_reply) "
                       "VALUES(7,11,'<reply@test>','<msg@test>','incoming','info@alpha.ru','me@mail.ru','Re: Тема','Ответ',?,1)", (stamp,))
    connection.commit()
    migrate(connection)
    assert connection.execute('PRAGMA user_version').fetchone()[0] == max(MIGRATIONS)
    connection.close()
    store = Store(path)
    try:
        assert store.one('SELECT id FROM products')['id'] == 7
        company = store.one('SELECT * FROM companies WHERE id=11')
        assert company['branch_name'] == '' and 'identity_key' in company.keys()
        assert store.one('SELECT status,fit_key FROM company_fit') == {'status': 'FIT', 'fit_key': 'manual:keep'}
        assert store.one('SELECT basis FROM permissions')['basis'] == 'Согласие'
        assert store.one('SELECT body FROM offers')['body'] == 'Текст УТП'
        assert store.one('SELECT status FROM outbound_attempts')['status'] == 'sent'
        assert store.one("SELECT body FROM mail_messages WHERE is_reply=1")['body'] == 'Ответ'
    finally:
        store.close()
