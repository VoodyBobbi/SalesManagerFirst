"""Windows Credential Manager only, with no plaintext fallback."""
import sys

class Secrets:
    service = 'Argon.SalesManagerFirst'
    credential_names = ('gigachat_key', 'tavily_key', 'exa_key', 'serper_key',
                        'firecrawl_key', 'jina_key', 'mail_password')

    def __init__(self, service=None):
        if service:
            self.service = service
        if sys.platform != 'win32':
            raise RuntimeError('Защищённое хранилище доступно только в Windows.')
        from keyring.backends.Windows import WinVaultKeyring
        self.backend = WinVaultKeyring()
        # keyring defaults to enterprise persistence, which may roam with a
        # domain profile. Keep new/updated entries on this computer only.
        self.backend.persist = 'local machine'
        self.warnings = []
        self._restrict_existing_entries()

    def _restrict_existing_entries(self):
        """Restrict older app entries without deleting or relocating credentials.

        Read only our exact namespace and known compound targets used by keyring.
        CredWrite atomically updates the same record. Values remain in WinVault;
        no plaintext export, database migration, or new encryption is involved.
        """
        from keyring.backends.Windows import win32cred
        targets = [self.service] + [self.backend._compound_name(name, self.service)
                                   for name in self.credential_names]
        for target in targets:
            record = self.backend._read_credential(target)
            if (not record or record['UserName'] not in self.credential_names
                    or record['Persist'] != win32cred.CRED_PERSIST_ENTERPRISE):
                continue
            updated = dict(record)
            # pywin32-ctypes CredWrite accepts Unicode, whereas CredRead returns
            # the blob bytes; use keyring's existing UTF-16/UTF-8 decoder.
            updated['CredentialBlob'] = record.value
            updated['Persist'] = win32cred.CRED_PERSIST_LOCAL_MACHINE
            try:
                win32cred.CredWrite(updated, 0)
            except Exception:
                # One legacy entry must not prevent access to all local work.
                # Keep the original entry and report this without exposing it.
                message = ('Не удалось ограничить хранение некоторых прежних ключей этим компьютером. '
                           'Ключи сохранены в Windows Credential Manager; новые ключи сохраняются локально.')
                if message not in self.warnings:
                    self.warnings.append(message)

    def get(self, key, default=''):
        return self.backend.get_password(self.service, key) or default

    def set(self, key, value):
        if value:
            self.backend.set_password(self.service, key, value)
        else:
            self.delete(key)

    def delete(self, key):
        import keyring.errors
        try:
            self.backend.delete_password(self.service, key)
        except keyring.errors.PasswordDeleteError:
            pass
