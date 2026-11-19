import sys
import uuid
import pytest


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows Credential Manager')
def test_credential_manager_round_trip():
    from sales_manager.storage import Secrets
    secrets = Secrets(service='Argon.SalesManagerFirst.Tests.' + uuid.uuid4().hex)
    try:
        secrets.set('test', 'controlled-test-value')
        assert secrets.get('test') == 'controlled-test-value'
        # Reconstructing a client models opening the app again, without copying
        # credentials into the project or database.
        restarted = Secrets(service=secrets.service)
        assert restarted.get('test') == 'controlled-test-value'
        secrets.delete('test')
        assert secrets.get('test') == ''
    finally:
        secrets.delete('test')
