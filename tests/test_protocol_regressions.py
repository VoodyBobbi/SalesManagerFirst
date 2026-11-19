"""Protocol parsing uses MIME and IMAP syntax independently of case and locale."""
from datetime import datetime

import pytest

from sales_manager.mail.service import MailService
from sales_manager.research.safe_http import FetchError, SafeFetcher
from sales_manager.storage import Store


@pytest.mark.parametrize('content_type', [
    'TEXT/HTML', 'Text/Plain; Charset=UTF-8', 'Application/XHTML+XML',
])
def test_page_accepts_case_insensitive_media_type(monkeypatch, content_type):
    fetcher = SafeFetcher()
    monkeypatch.setattr(fetcher, '_check_robots', lambda url: None)
    monkeypatch.setattr(fetcher, '_request', lambda *args, **kwargs:
                        (200, {'content-type': content_type}, b'<html>page</html>'))
    assert fetcher.fetch('https://site.example/page').text == '<html>page</html>'


@pytest.mark.parametrize('content_type', [
    'Application/RSS+XML', 'APPLICATION/ATOM+XML; CHARSET="UTF-8"',
    'Text/XML', 'APPLICATION/XML', 'Text/Plain', 'TEXT/HTML',
])
def test_document_accepts_case_insensitive_media_type(monkeypatch, content_type):
    fetcher = SafeFetcher()
    monkeypatch.setattr(fetcher, '_check_robots', lambda url: None)
    monkeypatch.setattr(fetcher, '_request', lambda *args, **kwargs:
                        (200, {'content-type': content_type}, b'<rss/>'))
    assert fetcher.fetch_document('https://site.example/feed').text == '<rss/>'


@pytest.mark.parametrize('method', ['fetch', 'fetch_document'])
@pytest.mark.parametrize('content_type', [
    'Application/Octet-Stream', 'image/png; note="text/html xml"',
])
def test_mime_support_is_checked_on_base_type_not_parameters(monkeypatch, method, content_type):
    fetcher = SafeFetcher()
    monkeypatch.setattr(fetcher, '_check_robots', lambda url: None)
    monkeypatch.setattr(fetcher, '_request', lambda *args, **kwargs:
                        (200, {'content-type': content_type}, b'payload'))
    with pytest.raises(FetchError):
        getattr(fetcher, method)('https://site.example/file')


@pytest.mark.parametrize('charset', ['CHARSET = "windows-1251"', 'charset=windows-1251'])
def test_charset_parameter_accepts_case_quotes_and_spacing(monkeypatch, charset):
    fetcher = SafeFetcher()
    monkeypatch.setattr(fetcher, '_check_robots', lambda url: None)
    text = '<html>Контакты компании</html>'
    monkeypatch.setattr(fetcher, '_request', lambda *args, **kwargs:
                        (200, {'content-type': 'Text/HTML; ' + charset}, text.encode('cp1251')))
    assert fetcher.fetch('https://site.example/contacts').text == text


@pytest.mark.parametrize('month,name', list(enumerate(
    ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'], 1)))
def test_first_imap_sync_uses_english_month_without_locale_formatting(monkeypatch, tmp_path, month, name):
    import sales_manager.mail.service as module

    class NoLocaleDateTime(datetime):
        def strftime(self, pattern):
            raise AssertionError('IMAP protocol dates must not use locale-dependent strftime')

    class Inbox:
        queries = None
        def select(self, folder, readonly=True):
            return 'OK', [b'0']
        def response(self, name):
            return name, [b'123']
        def uid(self, operation, *args):
            assert operation == 'search'
            self.queries = args
            return 'OK', [b'']
        def logout(self):
            pass

    monkeypatch.setattr(module, 'datetime', NoLocaleDateTime)
    store = Store(tmp_path / 'mail.sqlite3')
    try:
        store.execute('INSERT INTO mail_messages(message_id,direction,sender,sent_at) VALUES(?,?,?,?)',
                      ('<first@business.example>', 'outgoing', 'sender@business.example',
                       f'2026-{month:02d}-14T12:00:00+00:00'))
        mail = MailService(store, object())
        account = {'email': 'sender@business.example', 'last_uid': 0, 'uidvalidity': ''}
        monkeypatch.setattr(mail, '_account', lambda **kwargs: (account, 'synthetic'))
        monkeypatch.setattr(mail, 'reconcile', lambda: None)
        inbox = Inbox()
        monkeypatch.setattr(mail, '_imap', lambda *args: inbox)
        assert mail.sync() == 'Почта обновлена'
        assert inbox.queries == (None, 'SINCE', f'13-{name}-2026')
    finally:
        store.close()
