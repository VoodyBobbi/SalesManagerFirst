"""QA harness: synthetic data and no access to real network, mail or credentials."""
import socket
import pytest

from sales_manager.storage.store import Store


class FakeSecrets:
    def __init__(self, **initial):
        self.data = dict(initial)
        self.fail_on = None

    def get(self, key, default=''):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        if self.fail_on == key:
            self.fail_on = None
            raise RuntimeError('Synthetic Credential Manager failure')

    def delete(self, key):
        self.data.pop(key, None)


@pytest.fixture(autouse=True)
def network_off(monkeypatch):
    """Fail closed: the tests must never touch the user's accounts or Internet."""
    def denied(*args, **kwargs):
        raise AssertionError('LIVE_NETWORK_FORBIDDEN: test must use a fake transport')
    monkeypatch.setattr(socket, 'create_connection', denied)
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'getaddrinfo', denied)


@pytest.fixture
def isolated_store(tmp_path):
    store = Store(tmp_path / 'qa.sqlite3')
    yield store
    store.close()


@pytest.fixture
def fake_secrets():
    return FakeSecrets(gigachat_key='fake-only', mail_password='fake-password')
