"""Fail-injection around settings saves; never touches Credential Manager."""
import pytest

from sales_manager.storage.settings_save import apply_settings, validate_extra_urls


@pytest.mark.stability
def test_failed_secret_write_restores_all_previous_settings(isolated_store, fake_secrets):
    store = isolated_store
    secrets = fake_secrets
    store.set_setting('sender.name', 'ORIGINAL')
    secrets.set('tavily_key', 'old-value')
    secrets.fail_on = 'tavily_key'
    with pytest.raises(RuntimeError, match='Synthetic Credential Manager failure'):
        apply_settings(store, secrets,
                       {'sender.name': 'CHANGED'},
                       {'gigachat_key': 'new-value', 'tavily_key': 'new-value'},
                       ('owner@gmail.com', 'gmail', 'imap.gmail.com', 993, 'smtp.gmail.com', 465), 'new-password')
    assert store.get_setting('sender.name') == 'ORIGINAL'
    assert store.one('SELECT id FROM mail_accounts') is None
    assert secrets.get('gigachat_key') == 'fake-only'
    assert secrets.get('tavily_key') == 'old-value'
    assert secrets.get('mail_password') == 'fake-password'


def test_credential_validation_precedes_any_write(isolated_store, fake_secrets):
    store = isolated_store
    before = store.get_setting('sender.name')
    with pytest.raises(ValueError):
        validate_extra_urls({'rss_urls': ['javascript:alert(1)']})
    assert store.get_setting('sender.name') == before
    assert fake_secrets.get('gigachat_key') == 'fake-only'


def test_valid_extra_source_urls_are_normalized_to_expected_key():
    result = validate_extra_urls({'rss_urls': ['https://company.ru/rss', 'http://site.ru/feed']})
    assert result == {'search.rss_urls': ['https://company.ru/rss', 'http://site.ru/feed']}
