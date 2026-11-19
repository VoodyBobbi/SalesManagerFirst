"""Connection changes must not revive stale status or delete mail history."""
import threading

import httpx
import pytest

from sales_manager.ai import AIService
from sales_manager.storage import Store
from sales_manager.ui import app


class MemorySecrets:
    def __init__(self):
        self.values = {}

    def get(self, key, default=''):
        return self.values.get(key, default)

    def set(self, key, value):
        if value:
            self.values[key] = value
        else:
            self.values.pop(key, None)

    def delete(self, key):
        self.values.pop(key, None)


class Stub:
    def __init__(self, *args):
        pass


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(app, 'ResearchService', Stub)
    monkeypatch.setattr(app, 'MailService', Stub)
    view = app.Window(store=Store(tmp_path / 'data.sqlite3'), secrets=MemorySecrets())
    view.timer.stop()
    view.startup_mail_timer.stop()
    qtbot.addWidget(view)
    view.show()
    yield view
    qtbot.waitUntil(lambda: not view.workers, timeout=5000)
    view.close()


@pytest.mark.parametrize('change', ['key', 'scope', 'key_back'])
@pytest.mark.parametrize('fails', [False, True])
def test_old_gigachat_response_cannot_restore_balance_after_change(window, qtbot, monkeypatch, change, fails):
    entered, release = threading.Event(), threading.Event()
    window.giga_key.setText('old-test-key')
    assert window.save_settings()
    window.store.set_setting('ai.balance', {'balance': [{'value': 123}]})
    window.store.set_setting('ai.checked_at', 'old-date')

    def request(method, path, **kwargs):
        if path == '/models':
            entered.set()
            assert release.wait(5)
            if fails:
                raise RuntimeError('ошибка прежнего подключения')
            return {'data': [{'id': 'old-model'}]}
        return {'balance': [{'value': 777}]}

    monkeypatch.setattr(window.ai, '_request', request)
    window.test_ai()
    assert entered.wait(3)
    if change in ('key', 'key_back'):
        window.giga_key.setText('new-test-key')
    else:
        window.ai_scope.setCurrentText('GIGACHAT_API_B2B')
    assert window.save_settings()
    if change == 'key_back':
        window.giga_key.setText('old-test-key')
        assert window.save_settings()
    assert window.store.get_setting('ai.balance') is None
    assert window.store.get_setting('ai.checked_at') == ''
    assert 'Не подключено' in window.ai_status.text()
    release.set()
    qtbot.waitUntil(lambda: not window.workers, timeout=5000)
    assert window.store.get_setting('ai.balance') is None
    assert window.store.get_setting('ai.checked_at') == ''
    assert window.store.get_setting('ai.models') == []
    assert 'ошибка прежнего подключения' not in window.ai_status.text()


def test_clearing_email_removes_password_and_disables_account_but_preserves_messages(window):
    window.mail_email.setText('owner@gmail.com')
    window.mail_password.setText('test-app-password')
    assert window.save_settings()
    window.store.execute('UPDATE mail_accounts SET connected=1,last_uid=123 WHERE id=1')
    window.store.execute("INSERT INTO mail_messages(message_id,direction,subject,body) VALUES(?,?,?,?)",
                         ('<saved@example.test>', 'in', 'История', 'Сохранить текст'))
    window.mail_email.clear()
    assert window.save_settings()
    assert window.store.one('SELECT email,connected FROM mail_accounts WHERE id=1') == {'email': '', 'connected': 0}
    assert window.secrets.get('mail_password') == ''
    assert window.mail_password.text() == ''
    assert window.store.one('SELECT subject,body FROM mail_messages') == {'subject': 'История', 'body': 'Сохранить текст'}


def test_clear_email_credential_failure_rolls_back_account_and_password(window, monkeypatch):
    window.mail_email.setText('owner@gmail.com')
    window.mail_password.setText('test-app-password')
    assert window.save_settings()
    window.store.execute('UPDATE mail_accounts SET connected=1,last_uid=42 WHERE id=1')
    original = window.secrets.set
    failed = [False]
    def fail_after_removal(key, value):
        original(key, value)
        if key == 'mail_password' and not value and not failed[0]:
            failed[0] = True
            raise RuntimeError('Хранилище недоступно')
    monkeypatch.setattr(window.secrets, 'set', fail_after_removal)
    monkeypatch.setattr(app.QMessageBox, 'information', lambda *args: None)
    window.mail_email.clear()
    assert not window.save_settings()
    assert window.store.one('SELECT email,connected,last_uid FROM mail_accounts') == {
        'email': 'owner@gmail.com', 'connected': 1, 'last_uid': 42}
    assert window.secrets.get('mail_password') == 'test-app-password'


def test_queued_check_with_old_generation_does_not_issue_requests(window, monkeypatch):
    window.giga_key.setText('old-test-key')
    assert window.save_settings()
    old_generation = window.store.get_setting('ai.connection_generation')
    window.giga_key.setText('new-test-key')
    assert window.save_settings()
    def unexpected(*args, **kwargs):
        raise AssertionError('Запрос прежнего подключения запущен после его замены')
    monkeypatch.setattr(window.ai, '_request', unexpected)
    assert 'прежнего подключения' in window.ai.test(expected_generation=old_generation)


@pytest.mark.parametrize(('status', 'message'), [(401, 'ключ или тип доступа'), (429, 'лимит запросов'), (500, 'ошибка сервера')])
def test_oauth_error_classification(tmp_path, status, message):
    secrets = MemorySecrets()
    secrets.set('gigachat_key', 'test-key')
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status, json={})))
    service = AIService(Store(tmp_path / 'data.sqlite3'), secrets, client)
    with pytest.raises(RuntimeError, match=message):
        service._authorize()
