"""User decisions, model replacement and recovery of damaged setting values."""
import json

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton

from test_ui_workflow import window, company, offer, MemorySecrets
from sales_manager.ai import AIService
from sales_manager.storage import Store, now
from sales_manager.ui import dialogs
from sales_manager.ui.dialogs import CompanyDialog, OfferDialog


def test_replaced_key_model_matches_connection_fallback(window, monkeypatch):
    window.store.set_setting('ai.model', 'GigaChat-2-Max')
    window.ai_model.addItem('GigaChat-2-Max')
    window.ai_model.setCurrentText('GigaChat-2-Max')
    ai = AIService(window.store, MemorySecrets())
    monkeypatch.setattr(ai, '_request', lambda method, path: (
        {'data': [{'id': 'GigaChat'}, {'id': 'GigaChat-2'}]} if path == '/models'
        else {'status': 'unavailable'}))
    ai.test()
    window.update_ai_status()
    assert window.store.get_setting('ai.model') == 'GigaChat-2'
    assert window.ai_model.currentText() == 'GigaChat-2'


def test_usage_refresh_keeps_valid_unsaved_model_choice(window):
    window.store.set_setting('ai.models', ['GigaChat', 'GigaChat-2'])
    window.store.set_setting('ai.model', 'GigaChat-2')
    window.ai_model.addItem('GigaChat')
    window.ai_model.setCurrentText('GigaChat')
    window.update_ai_status()
    assert window.ai_model.currentText() == 'GigaChat'


@pytest.mark.parametrize('status,a,potential', [
    ('FIT', 1, 0), ('NOT_FIT', None, 0), ('INSUFFICIENT_EVIDENCE', None, 1)])
def test_manual_decision_updates_table_and_keeps_contact_rules(window, qtbot, status, a, potential):
    pid = window.store.ensure_product('Коробки')['id']
    cid = company(window.store, pid)
    window.store.execute("UPDATE company_fit SET status='INSUFFICIENT_EVIDENCE' WHERE company_id=?", (cid,))
    window.store.execute('INSERT INTO permissions(company_id,email,basis,evidence,created_at) VALUES(?,?,?,?,?)',
                         (cid, 'info@buyer.ru', 'Согласие', 'Письмо клиента', now()))
    window.store.execute('INSERT INTO opt_out(email,reason,created_at) VALUES(?,?,?)',
                         ('another@buyer.ru', 'Отказ', now()))
    contacts = window.store.query('SELECT * FROM company_contacts')
    permissions = window.store.query('SELECT * FROM permissions')
    opt_out = window.store.query('SELECT * FROM opt_out')
    window.open_product(pid)
    dialog = CompanyDialog(window.store, pid, cid, window)
    qtbot.addWidget(dialog)
    dialog.fit_status.setCurrentIndex(dialog.fit_status.findData(status))
    dialog.fit_reason.setText('Проверили деятельность компании по её каталогу.')
    qtbot.mouseClick(dialog.fit_save, Qt.MouseButton.LeftButton)
    saved = window.store.one('SELECT * FROM company_fit WHERE company_id=?', (cid,))
    assert (saved['status'], saved['manual'], saved['a'], saved['potential']) == (status, 1, a, potential)
    assert saved['fit_key'].startswith('manual:') and saved['ai_checked_key'] == ''
    assert saved['reason'] == dialog.fit_reason.text()
    assert window.store.query('SELECT * FROM company_contacts') == contacts
    assert window.store.query('SELECT * FROM permissions') == permissions
    assert window.store.query('SELECT * FROM opt_out') == opt_out
    assert window.company_rows[0]['fit_status'] == status
    if status == 'FIT':
        control = window.company_table.cellWidget(0, 3)
        assert any(button.text() == 'УТП' for button in [control] + control.findChildren(QPushButton))
    event = window.store.one("SELECT details FROM audit_events WHERE event='fit.manual_decision'")
    assert json.loads(event['details'])['status'] == status
    dialog.close()


def test_manual_decision_requires_reason_before_any_write(window, qtbot):
    pid = window.store.ensure_product('Коробки')['id']
    cid = company(window.store, pid)
    before = window.store.one('SELECT * FROM company_fit')
    dialog = CompanyDialog(window.store, pid, cid, window)
    qtbot.addWidget(dialog)
    dialog.fit_status.setCurrentIndex(dialog.fit_status.findData('NOT_FIT'))
    qtbot.mouseClick(dialog.fit_save, Qt.MouseButton.LeftButton)
    assert 'Укажите причину' in dialog.fit_notice.text()
    assert window.store.one('SELECT * FROM company_fit') == before
    assert not window.store.one("SELECT id FROM audit_events WHERE event='fit.manual_decision'")
    dialog.fit_status.setCurrentIndex(dialog.fit_status.findData(before['status']))
    dialog.close()


def test_unsaved_company_assessment_is_protected_when_closing_or_saving_permission(window, qtbot, monkeypatch):
    pid = window.store.ensure_product('Коробки')['id']
    cid = company(window.store, pid)
    dialog = CompanyDialog(window.store, pid, cid, window)
    qtbot.addWidget(dialog)
    dialog.show()
    prompts = []

    def cancel(_parent, _title, _message, action):
        prompts.append(action)
        return False

    monkeypatch.setattr(dialogs, 'confirm', cancel)
    dialog.fit_status.setCurrentIndex(dialog.fit_status.findData('NOT_FIT'))
    dialog.fit_reason.setText('Компания не использует упаковку.')
    dialog.basis.setText('Согласие')
    dialog.evidence.setText('Письмо клиента')
    assert not dialog.close()
    save_permission = next(button for button in dialog.findChildren(QPushButton)
                           if button.text() == 'Сохранить разрешение')
    save_permission.click()
    assert dialog.isVisible()
    assert prompts == ['Закрыть без сохранения оценки', 'Сохранить без оценки']
    assert window.store.query('SELECT * FROM permissions') == []
    assert window.store.one('SELECT status FROM company_fit')['status'] == 'FIT'

    dialog.fit_save.click()
    save_permission.click()
    assert not dialog.isVisible() and len(prompts) == 2
    assert window.store.one('SELECT status FROM company_fit')['status'] == 'NOT_FIT'
    assert window.store.one('SELECT basis FROM permissions')['basis'] == 'Согласие'


def test_switching_permission_recipient_protects_unsaved_changes(window, qtbot, monkeypatch):
    pid = window.store.ensure_product('Коробки')['id']
    cid = company(window.store, pid)
    second_email = 'office@buyer.ru'
    window.store.execute("INSERT INTO company_contacts(company_id,kind,value) VALUES(?,'email',?)", (cid, second_email))
    window.store.execute('INSERT INTO permissions(company_id,email,basis,evidence,created_at) VALUES(?,?,?,?,?)',
                         (cid, second_email, 'Договор', 'Подписанный документ', now()))
    dialog = CompanyDialog(window.store, pid, cid, window)
    qtbot.addWidget(dialog)
    dialog.email.setCurrentText('info@buyer.ru')
    dialog.basis.setText('Новый договор')
    dialog.evidence.setText('Письмо покупателя')
    dialog.optout.setChecked(True)

    choices = iter((False, True))
    monkeypatch.setattr(dialogs, 'confirm', lambda *_: next(choices))
    dialog.email.setCurrentText(second_email)
    assert dialog.email.currentText() == 'info@buyer.ru'
    assert dialog.basis.text() == 'Новый договор'
    assert dialog.evidence.text() == 'Письмо покупателя'
    assert dialog.optout.isChecked()

    dialog.email.setCurrentText(second_email)
    assert dialog.email.currentText() == second_email
    assert dialog.basis.text() == 'Договор'
    assert dialog.evidence.text() == 'Подписанный документ'
    assert not dialog.optout.isChecked()
    assert window.store.query('SELECT email FROM permissions') == [{'email': second_email}]
    assert window.store.query('SELECT * FROM opt_out') == []


def test_manual_decision_clears_approval_only_for_its_product_branch(window):
    first = window.store.ensure_product('Коробки')['id']
    second = window.store.ensure_product('Бумага')['id']
    cid = company(window.store, first)
    window.store.execute('INSERT INTO company_fit(product_id,company_id,status) VALUES(?,?,?)', (second, cid, 'FIT'))
    one = offer(window.store, first, cid)
    two = offer(window.store, second, cid)
    window.store.execute('UPDATE offers SET approved=1')
    window.store.set_company_fit_manual(first, cid, 'NOT_FIT', 'Подтверждена несовместимость.')
    assert window.store.one('SELECT approved FROM offers WHERE id=?', (one,))['approved'] == 0
    assert window.store.one('SELECT approved FROM offers WHERE id=?', (two,))['approved'] == 1
    assert window.store.one('SELECT status FROM company_fit WHERE product_id=?', (second,))['status'] == 'FIT'


@pytest.mark.parametrize('opt_out', [False, True])
def test_manual_fit_keeps_send_permission_and_opt_out_guards(window, qtbot, opt_out):
    pid = window.store.ensure_product('Коробки')['id']
    cid = company(window.store, pid)
    offer(window.store, pid, cid)
    window.store.set_company_fit_manual(pid, cid, 'FIT', 'Каталог проверен пользователем.')
    window.store.execute('INSERT INTO mail_accounts(id,email,connected,updated_at) VALUES(1,?,1,?)',
                         ('owner@gmail.com', now()))
    if opt_out:
        window.store.execute('INSERT INTO permissions(company_id,email,basis,evidence,created_at) VALUES(?,?,?,?,?)',
                             (cid, 'info@buyer.ru', 'Согласие', 'Ранее полученное письмо', now()))
        window.store.execute('INSERT INTO opt_out(email,reason,created_at) VALUES(?,?,?)',
                             ('info@buyer.ru', 'Отказ', now()))
    window.open_product(pid)
    dialog = OfferDialog(window, cid)
    qtbot.addWidget(dialog)
    dialog.send()
    if opt_out:
        assert 'сохранён запрет на письма' in dialog.notice.text()
        assert dialog.permission_button.isHidden()
    else:
        assert 'разрешения на контакт' in dialog.notice.text()
    assert not window.workers
    assert window.store.query('SELECT * FROM outbound_attempts') == []
    dialog.close()


@pytest.mark.parametrize('key,expected', [('semantic-v6:checked', 'FIT'), ('legacy-check', 'INSUFFICIENT_EVIDENCE')])
def test_restart_distinguishes_new_semantic_fit_from_legacy_local_decisions(tmp_path, key, expected):
    path = tmp_path / 'fit.sqlite3'
    store = Store(path)
    pid = store.ensure_product('Коробки')['id']
    cid = store.execute('INSERT INTO companies(name,created_at) VALUES(?,?)', ('Компания', now()))
    quote = 'Наша компания хранит и доставляет мебель для своих клиентов.'
    sid = store.put_source('https://buyer.ru/about', text=quote)
    store.execute('INSERT INTO company_fit(product_id,company_id,status,reason,source_ids,ai_checked_key) VALUES(?,?,?,?,?,?)',
                  (pid, cid, 'FIT', 'Гипотеза: упаковка мебели. Основание: «' + quote + '»', json.dumps([sid]), key))
    store.execute("DELETE FROM settings WHERE key IN ('fit.legacy_ai_reviewed_v4','fit.relationship_reviewed_v5')")
    store.close()
    reopened = Store(path)
    try:
        assert reopened.one('SELECT status FROM company_fit')['status'] == expected
    finally:
        reopened.close()


def test_invalid_setting_uses_default_and_preserves_original_once(tmp_path):
    store = Store(tmp_path / 'settings.sqlite3')
    try:
        store.execute('INSERT INTO settings(key,value) VALUES(?,?)', ('research.countries', '{broken'))
        assert store.get_setting('research.countries', ['Россия']) == ['Россия']
        assert store.get_setting('research.countries', ['Россия']) == ['Россия']
        assert store.one('SELECT value FROM settings WHERE key=?', ('research.countries',))['value'] == '{broken'
        events = store.query("SELECT details FROM audit_events WHERE event='settings.invalid_json'")
        assert len(events) == 1
        assert json.loads(events[0]['details']) == {'key': 'research.countries', 'value': '{broken'}
    finally:
        store.close()


def test_invalid_setting_recovery_does_not_commit_callers_transaction(tmp_path):
    store = Store(tmp_path / 'settings.sqlite3')
    try:
        store.execute('INSERT INTO settings(key,value) VALUES(?,?)', ('broken', '{broken'))
        store.db.execute('BEGIN IMMEDIATE')
        store.db.execute('INSERT INTO settings(key,value) VALUES(?,?)', ('uncommitted', 'true'))
        assert store.get_setting('broken', False) is False
        store.db.rollback()
        assert store.get_setting('uncommitted') is None
        assert not store.one("SELECT id FROM audit_events WHERE event='settings.invalid_json'")
    finally:
        store.close()


def test_startup_recovers_corrupt_legacy_marker_without_losing_settings(tmp_path):
    from sales_manager.storage.migrations import MIGRATIONS

    path = tmp_path / 'settings.sqlite3'
    store = Store(path)
    store.set_setting('sender.name', 'Сохранённая компания')
    store.execute('UPDATE settings SET value=? WHERE key=?', ('{broken', 'fit.relationship_reviewed_v5'))
    store.close()
    reopened = Store(path)
    try:
        assert reopened.get_setting('sender.name') == 'Сохранённая компания'
        event = reopened.one("SELECT details FROM audit_events WHERE event='settings.invalid_json'")
        assert json.loads(event['details']) == {'key': 'fit.relationship_reviewed_v5', 'value': '{broken'}
        assert reopened.one('PRAGMA user_version')['user_version'] == max(MIGRATIONS)
    finally:
        reopened.close()
