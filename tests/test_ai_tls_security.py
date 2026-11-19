"""Secure native trust, automatic bundled fallback, and isolated secret storage."""
import ssl
import sys
import uuid

import httpx
import pytest

from sales_manager.ai.service import AIService, BASE, OAUTH
from sales_manager.storage import Store


class Keys:
    def __init__(self, **values):
        self.values = values

    def get(self, key, default=''):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value


def test_native_trust_does_not_read_bundled_certificate(monkeypatch):
    import sales_manager.ai.service as module
    loaded = []

    class Context:
        def __init__(self, protocol):
            assert protocol == ssl.PROTOCOL_TLS_CLIENT

        def load_verify_locations(self, **kwargs):
            loaded.append(kwargs)

    monkeypatch.setattr(module.truststore, 'SSLContext', Context)
    AIService._tls()
    assert loaded == []
    AIService._tls(bundled_ca=True)
    assert loaded[0]['cafile'].endswith('russian_trusted_root_ca.pem')


@pytest.mark.parametrize('bundled', [False, True])
def test_all_tls_modes_verify_server_and_hostname(bundled):
    context = AIService._tls(bundled_ca=bundled)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_authorization_models_and_completion_use_automatic_ca_fallback(tmp_path, monkeypatch):
    import sales_manager.ai.service as module
    original_client = httpx.Client
    modes, requests, clients = [], [], []

    def native_error(request):
        raise httpx.ConnectError('handshake failed', request=request) from ssl.SSLCertVerificationError(1, 'untrusted root')

    def reply(request):
        requests.append(request)
        if str(request.url) == OAUTH:
            assert request.headers['Authorization'] == 'Basic controlled-key'
            return httpx.Response(200, json={'access_token': 'controlled-access-token', 'expires_at': 9999999999999})
        assert request.headers['Authorization'] == 'Bearer controlled-access-token'
        if request.url.path.endswith('/models'):
            return httpx.Response(200, json={'data': [{'id': 'GigaChat-2'}]})
        if request.url.path.endswith('/balance'):
            return httpx.Response(200, json={'balance': []})
        return httpx.Response(200, json={'choices': [{'message': {'content': 'OK'}}]})

    def client_factory(**kwargs):
        assert kwargs['verify'].verify_mode == ssl.CERT_REQUIRED
        assert kwargs['verify'].check_hostname
        modes.append(True)
        client = original_client(transport=httpx.MockTransport(native_error if len(modes) == 1 else reply))
        clients.append(client)
        return client

    monkeypatch.setattr(module.httpx, 'Client', client_factory)
    store = Store(tmp_path / 'app.sqlite3')
    ai = AIService(store, Keys(gigachat_key='controlled-key'))
    try:
        assert ai.test() == 'Подключено'
        assert ai._request('POST', '/chat/completions', json={})['choices'][0]['message']['content'] == 'OK'
        assert len(modes) == 2 and ai._bundled_ca
        assert clients[0].is_closed
        assert len(requests) == 4
        assert 'controlled-key' not in '\n'.join(store.db.iterdump())
        assert 'controlled-access-token' not in '\n'.join(store.db.iterdump())
    finally:
        for client in clients:
            client.close()
        store.close()


@pytest.mark.parametrize('failure', [httpx.ConnectError('network'), httpx.ReadTimeout('timed out')])
def test_non_certificate_failures_do_not_retry_or_load_extra_ca(tmp_path, monkeypatch, failure):
    import sales_manager.ai.service as module
    original_client = httpx.Client
    attempts = []

    def handler(request):
        attempts.append(request)
        raise failure

    client = original_client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(module.httpx, 'Client', lambda **kwargs: client)
    store = Store(tmp_path / 'app.sqlite3')
    ai = AIService(store, Keys(gigachat_key='test'))
    try:
        with pytest.raises(RuntimeError, match='подключение'):
            ai.test()
        assert len(attempts) == 1 and not ai._bundled_ca
    finally:
        client.close()
        store.close()


def test_fallback_rejects_invalid_server_without_further_retries(tmp_path, monkeypatch):
    import sales_manager.ai.service as module
    original_client = httpx.Client
    attempts, clients = [], []

    def handler(request):
        attempts.append(request)
        raise httpx.ConnectError('certificate failed', request=request) from ssl.SSLCertVerificationError(1, 'hostname mismatch')

    def factory(**kwargs):
        client = original_client(transport=httpx.MockTransport(handler))
        clients.append(client)
        return client

    monkeypatch.setattr(module.httpx, 'Client', factory)
    store = Store(tmp_path / 'app.sqlite3')
    ai = AIService(store, Keys(gigachat_key='test'))
    try:
        with pytest.raises(RuntimeError, match='подключение'):
            ai.test()
        assert len(attempts) == 2
    finally:
        for client in clients:
            client.close()
        store.close()


@pytest.mark.parametrize('status', [400, 401, 403, 429, 500])
def test_remote_auth_error_cannot_echo_credentials_into_ui_or_sqlite(tmp_path, status):
    from sales_manager.ui.workers import Worker
    secret = 'controlled-private-key-' + uuid.uuid4().hex
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, json={'error': 'Rejected key: ' + secret})))
    store = Store(tmp_path / 'app.sqlite3')
    ai = AIService(store, Keys(gigachat_key=secret), client)
    messages = []
    worker = Worker(lambda emit: ai.test())
    worker.signals.failed.connect(messages.append)
    try:
        worker.run()
        assert len(messages) == 1
        assert secret not in messages[0]
        assert secret not in '\n'.join(store.db.iterdump())
    finally:
        client.close()
        store.close()


@pytest.mark.parametrize('status', [401, 403, 429, 500])
def test_search_api_errors_do_not_repeat_server_authentication_payload(status):
    from sales_manager.research.providers import ProviderError, check_response
    secret = 'controlled-search-key-' + uuid.uuid4().hex
    with pytest.raises(ProviderError) as error:
        check_response(status, {'error': 'invalid api key ' + secret})
    assert secret not in str(error.value)


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows Credential Manager')
def test_credentials_are_local_and_sqlite_copy_does_not_transfer_them(tmp_path):
    from win32ctypes.pywin32 import win32cred
    from sales_manager.storage import Secrets
    from sales_manager.storage.settings_save import apply_settings

    namespace = 'Argon.SalesManagerFirst.Tests.' + uuid.uuid4().hex
    secrets = Secrets(service=namespace)
    other_computer = Secrets(service=namespace + '.isolated')
    credentials = {name: uuid.uuid4().hex for name in ('gigachat_key', 'tavily_key', 'exa_key', 'serper_key', 'firecrawl_key', 'jina_key')}
    password = uuid.uuid4().hex
    store = Store(tmp_path / 'app.sqlite3')
    try:
        assert secrets.backend.persist == win32cred.CRED_PERSIST_LOCAL_MACHINE
        apply_settings(store, secrets, {'ai.model': 'GigaChat-2'}, credentials,
                       ('test@example.org', 'custom', 'imap.example.org', 993, 'smtp.example.org', 465), password)
        dump = '\n'.join(store.db.iterdump())
        for name, value in {**credentials, 'mail_password': password}.items():
            assert secrets.get(name) == value
            assert other_computer.get(name) == ''
            assert value not in dump
            saved = secrets.backend._resolve_credential(namespace, name)
            assert saved['Persist'] == win32cred.CRED_PERSIST_LOCAL_MACHINE
        store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        store.close()
        import shutil
        shutil.copy2(store.path, tmp_path / 'copy.sqlite3')
        copied = Store(tmp_path / 'copy.sqlite3')
        try:
            assert copied.get_setting('ai.model') == 'GigaChat-2'
            assert copied.one('PRAGMA integrity_check')['integrity_check'] == 'ok'
            assert all(other_computer.get(name) == '' for name in credentials)
        finally:
            copied.close()
    finally:
        for name in (*credentials, 'mail_password'):
            secrets.delete(name)
        store.close()


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows Credential Manager')
def test_existing_roaming_entries_become_local_without_losing_keys():
    from keyring.backends.Windows import WinVaultKeyring, win32cred
    from sales_manager.storage import Secrets
    namespace = 'Argon.SalesManagerFirst.Tests.' + uuid.uuid4().hex
    legacy = WinVaultKeyring()
    values = {name: 'Проверка-' + uuid.uuid4().hex for name in Secrets.credential_names}
    try:
        for name, value in values.items():
            legacy.set_password(namespace, name, value)
        assert legacy._read_credential(namespace)['Persist'] == win32cred.CRED_PERSIST_ENTERPRISE
        upgraded = Secrets(service=namespace)
        for name, value in values.items():
            assert upgraded.get(name) == value
            assert upgraded.backend._resolve_credential(namespace, name)['Persist'] == win32cred.CRED_PERSIST_LOCAL_MACHINE
        # keyring may retain both its main target and an older compound target.
        targets = [namespace] + [legacy._compound_name(name, namespace) for name in values]
        assert all(record['Persist'] == win32cred.CRED_PERSIST_LOCAL_MACHINE
                   for target in targets if (record := legacy._read_credential(target)))
    finally:
        for name in values:
            try:
                legacy.delete_password(namespace, name)
            except Exception:
                pass


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows Credential Manager')
def test_persistence_update_failure_keeps_previous_credential(monkeypatch):
    from keyring.backends.Windows import WinVaultKeyring, win32cred
    from sales_manager.storage import Secrets
    namespace = 'Argon.SalesManagerFirst.Tests.' + uuid.uuid4().hex
    legacy = WinVaultKeyring()
    value = uuid.uuid4().hex
    legacy.set_password(namespace, 'gigachat_key', value)

    def fail(*args):
        raise OSError('controlled update failure')

    try:
        with monkeypatch.context() as context:
            context.setattr(win32cred, 'CredWrite', fail)
            available = Secrets(service=namespace)
            assert available.get('gigachat_key') == value
            assert len(available.warnings) == 1
            assert 'Ключи сохранены' in available.warnings[0]
            assert value not in available.warnings[0]
        assert legacy.get_password(namespace, 'gigachat_key') == value
        assert legacy._read_credential(namespace)['Persist'] == win32cred.CRED_PERSIST_ENTERPRISE
    finally:
        legacy.delete_password(namespace, 'gigachat_key')
