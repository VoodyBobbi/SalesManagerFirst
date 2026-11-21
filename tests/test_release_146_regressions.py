from datetime import datetime, timezone
from email.message import EmailMessage
import sqlite3
from threading import Event, Timer

import pytest

from sales_manager.ai.service import _numeric_warnings
from sales_manager.mail.service import MailCancelled, MailService
from sales_manager.research.extraction import parse_page
from sales_manager.research.market import extract_observations, normalize_search_date
from sales_manager.research.safe_http import RobotsRules
from sales_manager.storage.migrations import MIGRATIONS
from sales_manager.storage.store import Store


def test_old_database_preserves_all_uncertain_message_ids(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    with sqlite3.connect(path) as db:
        for version in (1, 2, 3):
            db.executescript(MIGRATIONS[version])
        db.execute('PRAGMA user_version=3')
        db.execute("INSERT INTO offers(id,subject,body,created_at,updated_at) VALUES(1,'s','b','t','t')")
        insert = '''INSERT INTO outbound_attempts(offer_id,idempotency_key,message_id,recipient,status,created_at,updated_at)
                    VALUES(1,?,?,'office@coffee-shop.ru',?,'t','t')'''
        db.execute(insert, ('1', '<one>', 'sending'))
        db.execute(insert, ('2', '<two>', 'uncertain'))
    db.close()
    store = Store(path)
    try:
        assert store.query('SELECT message_id,status FROM outbound_attempts ORDER BY id') == [
            {'message_id': '<one>', 'status': 'uncertain'},
            {'message_id': '<two>', 'status': 'uncertain'},
        ]
        with pytest.raises(sqlite3.IntegrityError):
            store.execute(insert, ('3', '<three>', 'sending'))
    finally:
        store.close()


def test_contact_ownership_and_nested_branch():
    page = parse_page(
        '<h2>Контакты партнёра</h2><div><p>partner@partner-shop.ru</p></div>'
        '<h2>Наши контакты</h2><div><p><span>office</span><b>@</b>coffee-shop.ru</p></div>'
        '<section class="branch"><h2>Филиал</h2><div><ul><li>Мы не закупаем кофе</li></ul></div></section>',
        'https://coffee-shop.ru/')
    assert page['emails'] == ['office@coffee-shop.ru']
    assert page['excluded_emails'] == ['partner@partner-shop.ru']
    assert len(page['branches']) == 1
    assert 'не закупаем кофе' not in page['organization_text']


def test_delimited_and_english_sales():
    values = extract_observations('смартфон', 'Продажи смартфонов в России за 2026 год – 5000 штук.')
    assert [(item['period'], item['value']) for item in values] == [('2026', 5000)]
    values = extract_observations('смартфон', 'iPhone sales in Russia grew by 20% in October 2026.')
    assert [(item['period'], item['value']) for item in values] == [('2026-10', 20)]


def test_period_is_local_to_measurement():
    values = extract_observations('смартфон', 'Продажи смартфонов в России за 2025 год — 5000 штук и за 2026 год — 6000 штук.')
    assert [(item['period'], item['value']) for item in values] == [('2025', 5000), ('2026', 6000)]
    values = extract_observations('смартфон', 'Продажи смартфонов в России составили 5000 штук в 2025 году и 6000 штук в 2026 году.')
    assert [(item['period'], item['value']) for item in values] == [('2025', 5000), ('2026', 6000)]
    values = extract_observations('смартфон', 'Компания основана в 2025 году. Продажи смартфонов в России составили 5000 штук.')
    assert not any(item['measured'] for item in values)


def test_measured_october_is_not_a_forecast():
    text = 'В октябре продажи смартфонов в России выросли на 20%, а в ноябре спрос может снизиться.'
    values = extract_observations('смартфон', text, published_at='2026-11-16')
    assert [(item['period'], item['value']) for item in values] == [('2026-10', 20)]


def test_absolute_and_relative_search_dates():
    today = datetime(2026, 11, 21, tzinfo=timezone.utc)
    assert normalize_search_date('Nov 16, 2026', today) == '2026-11-16'
    assert normalize_search_date('5 days ago', today) == '2026-11-16'


def test_money_scales_compare_canonical_amount():
    card = {'sources': [{'id': 1, 'text': 'Выручка 1 млн рублей.'}]}
    draft = {'subject': 'Предложение', 'body': 'Выручка 1 000 000 рублей.', 'reason': 'Совместная работа', 'source_ids': [1]}
    assert _numeric_warnings(draft, card) == []


def test_robots_uses_only_its_own_product_token():
    rules = RobotsRules('User-agent: Manager\nDisallow: /\n\nUser-agent: *\nAllow: /')
    assert rules.can_fetch('SalesManagerFirst/1.0', 'https://example.org/catalog')


def test_opt_out_word_order_and_cancel_connect():
    assert MailService._possible_opt_out(EmailMessage(), 'Больше не пишите.')
    service = MailService(None, None)
    cancel, finish = Event(), Event()
    timer = Timer(0.05, cancel.set)
    timer.start()
    try:
        with service._cancellable(cancel), pytest.raises(MailCancelled):
            service._connect(lambda: finish.wait(2) and None)
    finally:
        finish.set()
        timer.join()
