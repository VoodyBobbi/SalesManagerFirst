import re
import sqlite3
import threading

import httpx
import pytest
from PySide6.QtCore import QLockFile

from sales_manager.core import diagnostics
from sales_manager.ui import app
from sales_manager.ui.workers import Worker
from test_ui_workflow import window


def test_worker_error_code_matches_redacted_traceback(tmp_path, monkeypatch):
    logger = diagnostics.logging.Logger('test-diagnostics')
    monkeypatch.setattr(diagnostics, 'logger', logger)
    diagnostics.configure_diagnostics(tmp_path)
    credential = 'example-private-api-key'

    def operation(_emit):
        try:
            raise ValueError('Authorization: ' + credential)
        except ValueError as exc:
            raise KeyError('private request body') from exc

    worker = Worker(operation, context='Исследование', run_id=42, company_id=8)
    messages = []
    worker.signals.failed.connect(messages.append)
    try:
        worker.run()
    finally:
        for handler in logger.handlers:
            handler.close()
    error_id = re.search(r'\b[0-9A-F]{8}\b', messages[0]).group()
    logged = (tmp_path / 'logs' / 'app.log').read_text(encoding='utf-8')
    for entry in (error_id, 'run_id=42 company_id=8', 'Traceback (KeyError)', 'Traceback (ValueError)', 'operation'):
        assert entry in logged
    assert credential not in logged and 'private request body' not in logged
    assert credential not in messages[0] and 'private request body' not in messages[0]


@pytest.mark.parametrize(('error', 'message'), [
    (sqlite3.OperationalError('database is locked'), 'Ошибка локальной базы данных'),
    (httpx.ConnectError('secret-url'), 'Проверьте подключение'),
    (RuntimeError('unexpected English error'), 'Операция не завершена'),
])
def test_humanize_error_uses_safe_russian_guidance(error, message):
    text = diagnostics.humanize_error(error, 'ABCDEF12')
    assert message in text and 'ABCDEF12' in text
    assert str(error) not in text


def test_damaged_lock_can_be_recovered(qapp, tmp_path, monkeypatch):
    path = tmp_path / 'application.lock'
    path.write_text('broken', encoding='utf-8')
    answers = []
    monkeypatch.setattr(app, 'confirm', lambda *args: answers.append(args[-1]) or True)
    lock = QLockFile(str(path))
    assert app.acquire_lock(lock)
    assert answers == ['Снять блокировку']
    lock.unlock()


def test_running_instance_lock_is_preserved(qapp, tmp_path, monkeypatch):
    path = str(tmp_path / 'application.lock')
    running = QLockFile(path)
    assert running.tryLock(0)
    monkeypatch.setattr(app, 'confirm', lambda *args: pytest.fail('Asked to remove an active lock'))
    messages = []
    monkeypatch.setattr(app.QMessageBox, 'information', lambda *args: messages.append(args[-1]))
    assert not app.acquire_lock(QLockFile(path))
    assert 'PID' in messages[0]
    assert running.isLocked()
    running.unlock()


def test_lock_created_during_recovery_dialog_is_preserved(qapp, tmp_path, monkeypatch):
    path = tmp_path / 'application.lock'
    path.write_text('broken', encoding='utf-8')
    running = QLockFile(str(path))

    def confirm_after_other_start(*args):
        path.unlink()
        assert running.tryLock(0)
        return True

    monkeypatch.setattr(app, 'confirm', confirm_after_other_start)
    monkeypatch.setattr(app.QMessageBox, 'information', lambda *args: None)
    try:
        assert not app.acquire_lock(QLockFile(str(path)))
        assert running.isLocked() and path.exists()
    finally:
        running.unlock()


def test_force_close_is_offered_only_after_wait_and_needs_confirmation(window, qtbot, monkeypatch):
    gate = threading.Event()
    window.submit(lambda _emit: gate.wait(5), busy_message='Операция')
    exit_calls = []
    monkeypatch.setattr(app.os, '_exit', exit_calls.append)
    try:
        window.close()
        assert not window.force_close_button.isVisible()
        window._close_started -= 10
        window._finish_close()
        assert window.force_close_button.isVisible()
        monkeypatch.setattr(app, 'confirm', lambda *args: False)
        window.force_close_button.click()
        assert not exit_calls
        monkeypatch.setattr(app, 'confirm', lambda *args: True)
        window.force_close_button.click()
        assert exit_calls == [0]
    finally:
        gate.set()
    qtbot.waitUntil(lambda: not window.isVisible())
