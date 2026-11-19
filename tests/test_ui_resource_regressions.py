"""Worker ownership, explicit recipient choice and inexpensive product editing."""
import sqlite3
import threading

import pytest

from test_ui_workflow import window, company, offer
from sales_manager.ui.dialogs import OfferDialog
from sales_manager.storage import now


@pytest.mark.parametrize('fail', [False, True])
def test_worker_releases_its_connection_before_reporting_completion(window, qtbot, fail):
    window.pool.setMaxThreadCount(1)
    main_connection = window.store.db
    opened = []

    def operation(emit):
        opened.append((threading.get_ident(), window.store.db))
        window.store.set_setting('worker.saved', True)
        if fail:
            raise ValueError('Synthetic failure')
        return 'ok'

    window.submit(operation, failed=lambda message: None)
    qtbot.waitUntil(lambda: not window.workers and window.pool.activeThreadCount() == 0)
    assert window.store.db is main_connection
    assert window.store.get_setting('worker.saved') is True
    assert opened[0][0] != threading.get_ident()
    checked = []

    def probe(emit):
        assert threading.get_ident() == opened[0][0]
        assert not hasattr(window.store._local, 'connection')
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            opened[0][1].execute('SELECT 1')
        assert window.store.db is not opened[0][1]
        checked.append(True)

    window.submit(probe)
    qtbot.waitUntil(lambda: not window.workers and window.pool.activeThreadCount() == 0)
    assert checked == [True]


def test_editing_new_product_does_not_write_or_rebuild_each_character(window, monkeypatch):
    pid = window.store.ensure_product('Кофе')['id']
    window.open_product(pid)
    refreshes = []
    monkeypatch.setattr(window, 'refresh_companies', lambda: refreshes.append(True))
    window.branches.lineEdit().setText('Н')
    assert window.product_id is None
    before = window.store.db.total_changes
    for text in ['Но', 'Ноу', 'Ноут', 'Ноутб', 'Ноутбу', 'Ноутбук']:
        window.branches.lineEdit().setText(text)
    assert window.store.db.total_changes == before
    assert refreshes == [True]
    new_id = window.add_product()
    assert new_id != pid and window.product_id == new_id
    assert window.store.get_setting('product.active') == new_id
    assert window.store.one('SELECT COUNT(*) n FROM products')['n'] == 2


def test_invalid_product_replaces_success_notification_with_error(window):
    window.guide('Предыдущая операция завершена.', tone='success')
    window.branches.setEditText('алкоголь')

    assert window.add_product() is None
    assert window.action_notice.property('tone') == 'error'
    assert 'категория не поддерживается' in window.action_notice.text()


def test_reentering_corrected_model_keeps_existing_branch_and_companies(window):
    legacy = window.store.ensure_product('Redmi Buds 5', 'branch:redmi buds 5')
    cid = company(window.store, legacy['id'])
    window.branches.setEditText('REDMI BUDS 5')

    assert window.add_product() == legacy['id']
    assert [row['id'] for row in window.company_rows] == [cid]
    assert window.store.one('SELECT COUNT(*) n FROM products')['n'] == 1


def test_missing_saved_recipient_requires_explicit_replacement(window, qtbot):
    pid = window.store.ensure_product('Кофе')['id']
    cid = company(window.store, pid, email='sales@buyer.ru')
    oid = offer(window.store, pid, cid)
    window.store.execute("UPDATE offers SET recipient='old@buyer.ru',approved=1 WHERE id=?", (oid,))
    window.store.execute('INSERT INTO mail_accounts(id,email,connected,updated_at) VALUES(1,?,1,?)', ('owner@gmail.com', now()))
    window.open_product(pid)
    dialog = OfferDialog(window, cid)
    qtbot.addWidget(dialog)
    assert dialog.recipient_choice.count() == 1
    assert dialog.recipient == ''
    assert 'old@buyer.ru' in dialog.notice.text()
    assert 'Выберите' in dialog.notice.text()
    assert window.store.one('SELECT approved FROM offers')['approved'] == 0
    dialog.send()
    assert 'Выберите адрес' in dialog.notice.text()
    assert not window.workers
    assert window.store.query('SELECT * FROM outbound_attempts') == []
    dialog.recipient_choice.setCurrentIndex(0)
    assert dialog.recipient == 'sales@buyer.ru'
    assert 'новый получатель' in dialog.notice.text()
    assert dialog.save()
    assert window.store.one('SELECT recipient,approved FROM offers') == {'recipient': 'sales@buyer.ru', 'approved': 0}


def test_keyed_controls_explain_unavailability_without_activation_checkbox(window, monkeypatch, qtbot):
    assert window.free_confirmed.isHidden()
    calls = []
    monkeypatch.setattr(window.research, 'test_provider', lambda name: calls.append(name) or 'Бесплатный режим не подтверждён.', raising=False)
    for name in ('tavily', 'serper', 'exa', 'jina'):
        assert window.source_enabled[name].isEnabled()
        assert window.source_checks[name].isEnabled()
        window.source_keys[name].setText('synthetic-key')
        window.free_confirmed.setChecked(True)
        window.test_search(name)
        qtbot.waitUntil(lambda: not window.workers, timeout=5000)
        assert calls[-1] == name
    assert len(calls) == 4


def test_firecrawl_remains_available_without_key_and_blocked_with_one(window):
    assert window.source_checks['firecrawl'].isEnabled()
    window.source_keys['firecrawl'].setText('synthetic-key')
    assert window.source_checks['firecrawl'].isEnabled()
    assert 'бесплатного' in window.source_checks['firecrawl'].toolTip()
    window.source_keys['firecrawl'].clear()
    assert window.source_checks['firecrawl'].isEnabled()
