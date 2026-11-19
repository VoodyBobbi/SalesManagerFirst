"""Keep current cited AI evidence across layout changes, never across contradictions."""
import json
from unittest.mock import Mock

import pytest

from sales_manager.research.fit_rules import evaluate_fit
from sales_manager.research.providers import SearchResult
from sales_manager.research.service import ResearchService
from sales_manager.storage import Store, now


@pytest.fixture
def accepted_ai(tmp_path):
    store = Store(tmp_path / 'fit.sqlite3')
    secrets = Mock(get=Mock(return_value=''))
    service = ResearchService(store, secrets)
    service.ai = Mock()
    service.ai.assess_fit.side_effect = ValueError('Лимит токенов')
    store.set_setting('search.page_limit', 1)
    product = store.ensure_product('Кофе')
    text = 'В нашем каталоге кофе для организаций.'

    def page(value):
        sid = store.put_source('https://buyer.ru/', 'Покупатель', value)
        return dict(url='https://buyer.ru/', title='Покупатель', name='Покупатель', text=value,
                    source_id=sid, emails=['info@buyer.ru'], phones=[], legal_id='', links=[])

    first = page(text)
    service._page = lambda *args: first
    result = SearchResult(first['url'], country='Россия')
    service._company(product['id'], result, False, lambda _: None)
    old = store.one('SELECT * FROM company_fit')
    reason = 'Гипотеза: возможна закупка кофе. Основание: «' + text + '»'
    store.execute('UPDATE company_fit SET reason=?,ai_checked_key=?', (reason, 'accepted-ai'))
    store.execute('INSERT INTO offers(product_id,company_id,subject,body,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                  (product['id'], old['company_id'], 'Тема', 'Правка пользователя', now(), now()))
    yield store, service, product['id'], result, page, text, reason, first['source_id']
    store.close()


@pytest.mark.parametrize('key', ['', 'key-with-exhausted-quota'])
def test_layout_only_refresh_preserves_accepted_ai_fit_without_call(accepted_ai, key):
    store, service, pid, result, page, text, reason, old_sid = accepted_ai
    changed = page('Новости компании.\nВ нашем каталоге\nкофе для организаций.')
    assert evaluate_fit('Кофе', [changed], {}, 'Россия')['status'] == 'INSUFFICIENT_EVIDENCE'
    service._page = lambda *args: changed
    service.secrets.get = Mock(return_value=key)
    for _ in range(2):
        service._company(pid, result, False, lambda _: None)
    fit = store.one('SELECT * FROM company_fit')
    assert fit['status'] == 'FIT'
    assert fit['reason'] == reason
    assert {old_sid, changed['source_id']} <= set(json.loads(fit['source_ids']))
    assert store.one('SELECT body FROM offers')['body'] == 'Правка пользователя'
    service.ai.assess_fit.assert_not_called()
    assert store.query("SELECT * FROM audit_events WHERE event='fit_evidence_changed'") == []


@pytest.mark.parametrize('new_text,expected', [
    ('Сведения о компании обновлены. Россия.', 'INSUFFICIENT_EVIDENCE'),
    ('В нашем каталоге кофе для организаций. Мы не закупаем кофе.', 'NOT_FIT'),
    ('В нашем каталоге кофе для организаций. Компания ликвидирована.', 'NOT_FIT'),
    ('Мы не занимаемся продажей кофе. Россия.', 'INSUFFICIENT_EVIDENCE'),
])
def test_new_evidence_cannot_silently_keep_outdated_ai_fit(accepted_ai, new_text, expected):
    store, service, pid, result, page, text, reason, old_sid = accepted_ai
    current = page(new_text)
    service._page = lambda *args: current
    service._company(pid, result, False, lambda _: None)
    fit = store.one('SELECT * FROM company_fit')
    assert fit['status'] == expected
    assert store.one('SELECT body FROM offers')['body'] == 'Правка пользователя'
    audit = store.one("SELECT details FROM audit_events WHERE event='fit_evidence_changed'")
    details = json.loads(audit['details'])
    assert details['previous_reason'] == reason
    assert old_sid in details['previous_source_ids']
    assert store.one('SELECT text FROM sources WHERE id=?', (old_sid,))['text'] == text
    service._company(pid, result, False, lambda _: None)
    assert len(store.query("SELECT * FROM audit_events WHERE event='fit_evidence_changed'")) == 1


def test_quote_on_a_different_page_does_not_transfer_old_ai_decision(accepted_ai):
    store, service, pid, result, page, text, reason, old_sid = accepted_ai
    current = page('В нашем каталоге\nкофе для организаций.')
    current['url'] = 'https://buyer.ru/new-page'
    # A new page may need its own assessment even when wording overlaps.
    service._page = lambda *args: current
    service._company(pid, result, False, lambda _: None)
    assert store.one('SELECT status FROM company_fit')['status'] == 'INSUFFICIENT_EVIDENCE'


def test_cosmetic_refresh_keeps_personalized_ai_reason_even_when_rules_still_fit(accepted_ai):
    store, service, pid, result, page, text, reason, old_sid = accepted_ai
    current = page('Новости. ' + text)
    service._page = lambda *args: current
    service._company(pid, result, False, lambda _: None)
    assert store.one('SELECT reason FROM company_fit')['reason'] == reason
