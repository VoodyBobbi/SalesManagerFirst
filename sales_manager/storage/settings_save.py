"""Validated settings save with SQLite rollback and best-effort WinVault recovery.

SQLite and Windows Credential Manager have no shared transaction manager. All
fields are validated before writing either store. On any failure, the SQLite
transaction rolls back and written secrets are restored to their old values.
"""
import json
from urllib.parse import urlsplit

from sales_manager.storage.store import now


def validate_extra_urls(entries):
    result = {}
    for setting, values in entries.items():
        if not isinstance(values, list):
            raise ValueError('Укажите URL дополнительных источников через пробел.')
        for url in values:
            try:
                parts = urlsplit(url)
                valid = (parts.scheme in ('http', 'https') and bool(parts.hostname)
                         and not parts.username and not parts.password and not parts.fragment
                         and not any(char.isspace() for char in url))
                # Validate port syntax too, before anything is written.
                _ = parts.port
            except ValueError:
                valid = False
            if not valid:
                raise ValueError('Для дополнительных источников укажите полные http/https URL через пробел.')
        result['search.' + setting] = list(values)
    return result


def apply_settings(store, secrets, settings, credentials, mail_values=None, mail_password=None, *, sent_folder=None):
    """Commit all settings and optional mail account, or retain previous state.

    `mail_values` is the six-field tuple email/provider/hosts/ports. A supplied
    `sent_folder` records a verified connection, even if no folder was found.
    """
    old_secrets = {key: secrets.get(key) for key in credentials}
    if mail_values is not None:
        old_secrets['mail_password'] = secrets.get('mail_password')
        credentials = {**credentials, 'mail_password': mail_password}
    changed = {key: value for key, value in credentials.items() if old_secrets[key] != value}
    touched = []
    try:
        # Do not invoke Store.execute/set_setting inside the transaction:
        # those methods independently commit each statement.
        with store.db as connection:
            for key, value in settings.items():
                connection.execute(
                    'INSERT INTO settings(key,value) VALUES(?,?) '
                    'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                    (key, json.dumps(value, ensure_ascii=False)))
            if mail_values is not None:
                connection.execute('''
                    INSERT INTO mail_accounts(id,email,provider,imap_host,imap_port,smtp_host,smtp_port,connected,sent_folder,updated_at)
                    VALUES(1,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET email=excluded.email,provider=excluded.provider,
                    imap_host=excluded.imap_host,imap_port=excluded.imap_port,
                    smtp_host=excluded.smtp_host,smtp_port=excluded.smtp_port,
                    connected=excluded.connected,sent_folder=excluded.sent_folder,updated_at=excluded.updated_at,
                    last_uid=CASE WHEN excluded.connected=1 AND mail_accounts.email=excluded.email
                        AND mail_accounts.imap_host=excluded.imap_host AND mail_accounts.imap_port=excluded.imap_port
                        THEN mail_accounts.last_uid ELSE 0 END,
                    uidvalidity=CASE WHEN excluded.connected=1 AND mail_accounts.email=excluded.email
                        AND mail_accounts.imap_host=excluded.imap_host AND mail_accounts.imap_port=excluded.imap_port
                        THEN mail_accounts.uidvalidity ELSE '' END
                ''', tuple(mail_values) + (int(sent_folder is not None), sent_folder or '', now()))
            for key, value in changed.items():
                # Mark BEFORE set: a backend is allowed to modify the entry
                # and then raise, in which case it must also be rolled back.
                touched.append(key)
                secrets.set(key, value)
    except Exception:
        failures = []
        for key in reversed(touched):
            try:
                secrets.set(key, old_secrets[key])
            except Exception:
                failures.append(key)
        if failures:
            raise RuntimeError('Не удалось полностью восстановить секреты после ошибки сохранения. '
                               'Проверьте Windows Credential Manager.') from None
        raise
