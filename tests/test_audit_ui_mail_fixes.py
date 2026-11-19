"""Regression cases for delivery snapshots, recovery and foreground lifetimes."""
import json
import smtplib
import threading

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QApplication, QDialog
from shiboken6 import isValid

from sales_manager.core.company_listing import visible_companies
from sales_manager.mail.service import save_offer
from sales_manager.storage import now
from sales_manager.ui import dialogs
from sales_manager.ui.dialogs import OfferDialog
from test_mail import Inbox, mail_case, make_reply
from test_ui_workflow import company, offer, window


def delivery(store, offer_id, status='sent'):
    store.execute('''INSERT INTO outbound_attempts
        (offer_id,idempotency_key,message_id,recipient,status,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?)''',
        (offer_id, 'audit-key', '<audit@sender.test>', 'info@buyer.ru', status, now(), now()))


def test_sent_offer_displays_saved_recipient_when_contact_disappears(window, qtbot):
    pid = window.store.ensure_product('Картон')['id']
    cid = company(window.store, pid, email='sales@buyer.ru')
    oid = offer(window.store, pid, cid)
    window.store.execute("UPDATE offers SET recipient='info@buyer.ru' WHERE id=?", (oid,))
    delivery(window.store, oid)
    window.open_product(pid)

    dialog = OfferDialog(window, cid)
    qtbot.addWidget(dialog)
    assert dialog.delivery_locked()
    assert not dialog.recipient_choice.isEnabled()
    assert dialog.recipient_choice.placeholderText() == 'info@buyer.ru'
    assert 'Письмо уже отправлено' in dialog.notice.text()


def test_saved_correspondence_stays_visible_after_contact_disappears(window):
    pid = window.store.ensure_product('Картон')['id']
    cid = company(window.store, pid, phone='')
    oid = offer(window.store, pid, cid)
    delivery(window.store, oid)
    window.store.execute("INSERT INTO mail_messages(product_id,company_id,message_id,direction,is_reply) "
                         "VALUES(?,?,?,'incoming',1)", (pid, cid, '<reply@buyer.ru>'))
    window.store.execute('DELETE FROM company_contacts WHERE company_id=?', (cid,))
    window.open_product(pid)

    assert visible_companies(window.store, pid) == []
    for mode in ('Отправлено', 'Ответили'):
        window.company_filter.setCurrentText(mode)
        assert [row['id'] for row in window.company_rows] == [cid]
        assert window.company_table.cellWidget(0, 5) is not None


def test_regeneration_failure_leaves_existing_draft_editable(window, qtbot, monkeypatch):
    pid = window.store.ensure_product('Картон')['id']
    cid = company(window.store, pid)
    oid = offer(window.store, pid, cid)
    delivery(window.store, oid, 'failed')
    window.open_product(pid)
    dialog = OfferDialog(window, cid)
    qtbot.addWidget(dialog)
    dialog.body.setPlainText('Отредактированный текст')

    def unavailable(*args, **kwargs):
        raise RuntimeError('GigaChat временно недоступен')

    monkeypatch.setattr(dialogs, 'confirm', lambda *args: True)
    monkeypatch.setattr(window.ai, 'generate_offer', unavailable, raising=False)
    dialog.regenerate.click()
    qtbot.waitUntil(lambda: not window.workers)

    assert dialog.save_button.isEnabled() and dialog.send_button.isEnabled()
    assert dialog.body.toPlainText() == 'Отредактированный текст'
    assert dialog.save()
    assert window.store.one('SELECT status FROM outbound_attempts')['status'] == 'failed'


@pytest.mark.parametrize('status', ['sent', 'sending', 'uncertain'])
def test_sent_or_inflight_offer_cannot_be_edited_even_by_direct_save(window, qtbot, status):
    pid = window.store.ensure_product('Картон')['id']
    cid = company(window.store, pid)
    oid = offer(window.store, pid, cid)
    window.store.execute('UPDATE offers SET approved=1 WHERE id=?', (oid,))
    window.open_product(pid)
    dialog = OfferDialog(window, cid)
    qtbot.addWidget(dialog)
    # A send may finish after the dialog loaded its old draft.
    delivery(window.store, oid, status)
    dialog.set_busy(False)
    assert all(not widget.isEnabled() for widget in
               (dialog.subject, dialog.body, dialog.recipient_choice, dialog.save_button,
                dialog.send_button, dialog.regenerate))
    dialog.body.setPlainText('Замена после отправки')
    assert dialog.save() is False
    with pytest.raises(ValueError, match='отправлено'):
        save_offer(window.store, oid, 'Замена', 'Замена', 'other@buyer.ru')
    saved = window.store.one('SELECT body,approved FROM offers WHERE id=?', (oid,))
    assert saved == {'body': 'Проверенный человеком текст', 'approved': 1}


def test_successful_send_keeps_offer_controls_locked(window, qtbot, monkeypatch):
    pid = window.store.ensure_product('Картон')['id']
    cid = company(window.store, pid)
    oid = offer(window.store, pid, cid)
    window.open_product(pid)
    window.store.execute('INSERT INTO mail_accounts(id,email,connected) VALUES(1,?,1)', ('owner@sender.test',))
    window.store.execute('INSERT INTO permissions(company_id,email,basis,evidence,created_at) VALUES(?,?,?,?,?)',
                         (cid, 'info@buyer.ru', 'Согласие', 'Документ', now()))
    monkeypatch.setattr(dialogs, 'confirm', lambda *args: True)
    def send(*args, **kwargs):
        delivery(window.store, oid)
        return 'Отправлено'
    monkeypatch.setattr(window.mail, 'send_offer', send, raising=False)
    dialog = OfferDialog(window, cid)
    qtbot.addWidget(dialog)
    dialog.send()
    qtbot.waitUntil(lambda: not window.workers)
    assert not dialog.save_button.isEnabled()
    assert not dialog.regenerate.isEnabled()
    assert not dialog.subject.isEnabled()
    assert window.store.one('SELECT approved FROM offers WHERE id=?', (oid,))['approved'] == 1


def test_empty_successful_imap_search_is_not_an_error(mail_case):
    store, mail, _, _, _, _ = mail_case
    inbox = Inbox(b'')
    original = inbox.uid
    inbox.uid = lambda command, *args: ('OK', [None]) if command == 'search' else original(command, *args)
    mail._imap = lambda *args: inbox
    assert mail.sync() == 'Почта обновлена'


@pytest.mark.parametrize('header', ['Delivered-To', 'X-Original-To'])
def test_bcc_reply_accepts_display_name_in_delivery_header(mail_case, header):
    store, mail, _, oid, _, _ = mail_case
    mail.send_offer(oid)
    outgoing = store.one('SELECT * FROM mail_messages')
    message = make_reply(outgoing['message_id'])
    del message['To']
    message[header] = 'Owner <owner@sender.test>'
    account = store.one('SELECT * FROM mail_accounts')
    assert mail._match_reply(message, account)['id'] == outgoing['id']


def test_retry_updates_history_date_without_duplicating_message(mail_case, monkeypatch):
    store, mail, smtp, oid, _, _ = mail_case
    def refuse(*args, **kwargs):
        raise smtplib.SMTPRecipientsRefused({'buyer@client.ru': (550, b'refused')})
    smtp.send_message = refuse
    with pytest.raises(RuntimeError):
        mail.send_offer(oid)
    store.execute("UPDATE mail_messages SET sent_at='2000-01-01T00:00:00+00:00'")
    monkeypatch.setattr(smtp, 'send_message', lambda *args, **kwargs: {})
    assert mail.send_offer(oid) == 'Отправлено'
    rows = store.query('SELECT sent_at FROM mail_messages')
    assert len(rows) == 1 and rows[0]['sent_at'].startswith(now()[:10])


def test_rejected_automatic_candidate_can_be_found_and_reconsidered(window):
    pid = window.store.ensure_product('Картон')['id']
    cid = company(window.store, pid)
    window.store.execute("UPDATE company_fit SET status='NOT_FIT' WHERE company_id=?", (cid,))
    window.open_product(pid)
    assert window.company_rows == []
    window.company_filter.setCurrentText('Не подходят')
    assert [row['id'] for row in window.company_rows] == [cid]
    window.store.set_company_fit_manual(pid, cid, 'FIT', 'Проверено вручную')
    window.company_filter.setCurrentText('Все')
    assert [row['id'] for row in window.company_rows] == [cid]


def test_key_edit_burst_reads_statuses_once(window, monkeypatch, qtbot):
    calls = []
    monkeypatch.setattr(window.research, 'provider_status', lambda name: calls.append(name) or {}, raising=False)
    control = window.source_keys['tavily']
    for value in ('t', 'ta', 'tav', 'tavi', 'tavily'):
        control.setText(value)
    assert calls == []
    qtbot.waitUntil(lambda: bool(calls))
    assert len(calls) == len(window.source_status)
    assert not window.search_usage_timer.isActive()


def test_status_refresh_uses_current_fields_without_reading_saved_credentials(window, monkeypatch):
    from sales_manager.research.service import ResearchService
    window.research = ResearchService(window.store, window.secrets)
    window.source_keys['tavily'].setText('new-unverified-key')
    window.source_enabled['tavily'].setChecked(True)

    def unexpected_read(*args, **kwargs):
        pytest.fail('A status refresh must not read Windows credentials')

    monkeypatch.setattr(window.secrets, 'get', unexpected_read)
    window.update_search_usage()
    assert window.source_status['tavily'].text() != 'Работает'
    assert not window.search_usage_timer.isActive()


def test_empty_mail_account_does_not_block_unrelated_settings(window):
    window.store.execute("INSERT INTO mail_accounts(id,email,updated_at) VALUES(1,'','preserved')")
    window.mail_busy = True
    assert window.save_settings()
    assert window.store.one('SELECT updated_at FROM mail_accounts')['updated_at'] == 'preserved'
    window.mail_busy = False


def test_destroyed_dialog_does_not_destroy_reusable_busy_overlay(window, qtbot):
    owner = QDialog(window)
    owner.show()
    gate = threading.Event()
    window.submit(lambda emit: gate.wait(3), owner=owner, busy_message='Операция')
    overlay = window.busy_overlay
    owner.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    try:
        assert not isValid(owner)
        assert isValid(overlay) and overlay.parentWidget() is window
    finally:
        gate.set()
        qtbot.waitUntil(lambda: not window.workers)
    assert overlay.isHidden()
    overlay.start('Следующая операция', window)
    assert overlay.isVisible()
    overlay.stop()


def test_close_during_operation_explains_wait_in_visible_overlay(window, qtbot):
    gate = threading.Event()
    window.submit(lambda emit: gate.wait(3), busy_message='Долгая операция')
    try:
        window.close()
        assert window.busy_overlay.message.text() == 'Завершаем текущую операцию…'
        assert window.action_notice.text() == 'Завершаем текущую операцию…'
    finally:
        gate.set()
        qtbot.waitUntil(lambda: not window.workers)


def test_manual_research_does_not_switch_to_old_startup_context(window, monkeypatch, qtbot):
    old = window.store.execute('INSERT INTO job_runs(kind,status,countries,created_at,updated_at) VALUES(?,?,?,?,?)',
                               ('market', 'interrupted', json.dumps(['Беларусь']), now(), now()))
    window.store.execute('INSERT INTO job_queue(run_id,task_key,payload,status) VALUES(?,?,?,?)',
                         (old, 'old', '{}', 'pending'))
    calls = []
    monkeypatch.setattr(window.research, 'research_market', lambda *args: calls.append('manual') or {'status': 'complete'}, raising=False)
    monkeypatch.setattr(window.research, 'resume_market', lambda *args: calls.append('old') or {'status': 'complete'}, raising=False)
    window.countries = ['Россия']
    window.start_market()
    qtbot.waitUntil(lambda: not window.workers)
    window.recover_research()
    assert calls == ['manual']
    assert window.countries == ['Россия']
    assert not window.recovery_timer.isActive()


def test_startup_recovery_still_restores_saved_context(window, monkeypatch, qtbot):
    old = window.store.execute('INSERT INTO job_runs(kind,status,countries,created_at,updated_at) VALUES(?,?,?,?,?)',
                               ('market', 'interrupted', json.dumps(['Беларусь']), now(), now()))
    window.store.execute('INSERT INTO job_queue(run_id,task_key,payload,status) VALUES(?,?,?,?)',
                         (old, 'old', '{}', 'pending'))
    calls = []
    monkeypatch.setattr(window.research, 'research_market', lambda *args: None, raising=False)
    monkeypatch.setattr(window.research, 'resume_market', lambda run_id, *args: calls.append(run_id) or {'status': 'interrupted'}, raising=False)
    window.recover_research()
    qtbot.waitUntil(lambda: not window.workers)
    assert calls == [old]
    assert window.countries == ['Беларусь']
