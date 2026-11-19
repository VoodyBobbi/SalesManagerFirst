"""Gmail protocol integration, with no real account and no outbound network."""
import imaplib
import smtplib
from email.message import EmailMessage
from unittest.mock import Mock

import pytest

from sales_manager.mail import MAIL_PROVIDERS, MailService, provider_help, provider_settings
from sales_manager.mail.service import MailAuthenticationError
from sales_manager.storage import Store, now


class Secrets:
    def __init__(self, password='abcd efgh ijkl mnop'):
        self.password = password

    def get(self, key, default=''):
        return self.password if key == 'mail_password' else default


def confirm_company_fit(store, product, company):
    source = store.put_source('https://client.test/about', 'Деятельность', 'Компания продаёт ноутбуки и офисную электронику.')
    store.execute('INSERT INTO company_fit(product_id,company_id,status,reason,source_ids,checked_at) VALUES(?,?,?,?,?,?)',
                  (product, company, 'FIT', 'Товар относится к подтверждённому ассортименту.', '[' + str(source) + ']', now()))


@pytest.fixture
def gmail(tmp_path):
    store = Store(tmp_path / 'gmail.sqlite3')
    config = provider_settings('gmail')
    store.execute('INSERT INTO mail_accounts(id,email,provider,imap_host,imap_port,smtp_host,smtp_port,connected) '
                  'VALUES(1,?,?,?,?,?,?,0)',
                  ('owner@gmail.com', 'gmail', config['imap_host'], config['imap_port'],
                   config['smtp_host'], config['smtp_port']))
    service = MailService(store, Secrets())
    yield store, service
    store.close()


def test_gmail_defaults_are_first_and_do_not_mutate_existing_account(gmail):
    store, _ = gmail
    assert MAIL_PROVIDERS[0] == ('gmail', 'Gmail (Google)')
    assert provider_settings('gmail') == {
        'imap_host': 'imap.gmail.com', 'imap_port': 993,
        'smtp_host': 'smtp.gmail.com', 'smtp_port': 465,
    }
    store.execute("UPDATE mail_accounts SET provider='yandex',imap_host='imap.yandex.ru',smtp_host='smtp.yandex.ru',connected=1")
    before = store.one('SELECT * FROM mail_accounts')
    provider_settings('gmail')
    MailService(store, Secrets())
    assert store.one('SELECT * FROM mail_accounts') == before


def test_gmail_help_explains_unavailable_app_password_without_claiming_oauth():
    help_text = provider_help('gmail')
    assert 'двухэтапную' in help_text
    assert 'администратором' in help_text and 'Дополнительной защитой' in help_text
    assert 'не OAuth' in help_text
    assert provider_settings('yandex')['smtp_host'] == 'smtp.yandex.ru'
    assert provider_settings('manual')['imap_host'] == ''


def test_gmail_uses_tls_ports_full_address_and_pasted_app_password(gmail, monkeypatch):
    store, service = gmail
    account, password = service._account()
    assert password == 'abcdefghijklmnop'
    tls = object()
    service._tls = lambda: tls
    imap, smtp = Mock(), Mock()
    imap_class, smtp_class = Mock(return_value=imap), Mock(return_value=smtp)
    monkeypatch.setattr(imaplib, 'IMAP4_SSL', imap_class)
    monkeypatch.setattr(smtplib, 'SMTP_SSL', smtp_class)
    service._imap(account, password)
    service._smtp(account, password)
    imap_class.assert_called_once_with('imap.gmail.com', 993, ssl_context=tls, timeout=30)
    smtp_class.assert_called_once_with('smtp.gmail.com', 465, timeout=30, context=tls)
    imap.login.assert_called_once_with('owner@gmail.com', 'abcdefghijklmnop')
    smtp.login.assert_called_once_with('owner@gmail.com', 'abcdefghijklmnop')


def test_gmail_connection_checks_inbox_and_sender_without_sending_test_mail(gmail):
    store, service = gmail
    imap, smtp = Mock(), Mock()
    imap.select.return_value = ('OK', [b'0'])
    imap.list.return_value = ('OK', [b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"'])
    smtp.mail.return_value = (250, b'OK')
    service._imap = lambda *args: imap
    service._smtp = lambda *args: smtp
    assert service.test() == 'Почта подключена'
    account = store.one('SELECT * FROM mail_accounts')
    assert account['connected'] == 1
    assert account['sent_folder'] == '"[Gmail]/Sent Mail"'
    smtp.mail.assert_called_once_with('owner@gmail.com')
    smtp.rset.assert_called_once()
    smtp.send_message.assert_not_called()
    imap.logout.assert_called_once()
    smtp.quit.assert_called_once()


@pytest.mark.parametrize('protocol', ['imap', 'smtp'])
def test_gmail_auth_refusal_explains_2fa_app_password_and_restrictions(gmail, monkeypatch, protocol):
    store, service = gmail
    store.execute('UPDATE mail_accounts SET connected=1')
    account, password = service._account()
    client = Mock()
    if protocol == 'imap':
        client.login.side_effect = imaplib.IMAP4.error('[AUTHENTICATIONFAILED] secret-server-response')
        monkeypatch.setattr(imaplib, 'IMAP4_SSL', Mock(return_value=client))
        connect = service._imap
    else:
        client.login.side_effect = smtplib.SMTPAuthenticationError(534, b'Application-specific password required secret-server-response')
        monkeypatch.setattr(smtplib, 'SMTP_SSL', Mock(return_value=client))
        connect = service._smtp
    with pytest.raises(MailAuthenticationError) as caught:
        connect(account, password)
    assert 'Gmail отклонил вход' in str(caught.value)
    assert 'двухэтапную' in str(caught.value)
    assert 'администратору' in str(caught.value)
    assert 'secret-server-response' not in str(caught.value)
    assert 'abcdefghijklmnop' not in str(caught.value)
    assert store.one('SELECT connected FROM mail_accounts')['connected'] == 0


def test_gmail_missing_secret_has_actionable_guidance(gmail):
    store, _ = gmail
    with pytest.raises(ValueError, match='пароль приложения Google'):
        MailService(store, Secrets(''))._account()


def test_candidate_authentication_failure_preserves_working_account(gmail, monkeypatch):
    store, service = gmail
    store.execute("UPDATE mail_accounts SET connected=1,last_uid=52,uidvalidity='9'")
    previous = store.one('SELECT * FROM mail_accounts')
    client = Mock()
    client.login.side_effect = imaplib.IMAP4.error('Authentication rejected')
    monkeypatch.setattr(imaplib, 'IMAP4_SSL', Mock(return_value=client))

    values = tuple(previous[key] for key in service.account_fields)
    with pytest.raises(MailAuthenticationError):
        service.verify_connection(values, 'new-invalid-password')

    assert store.one('SELECT * FROM mail_accounts') == previous
    assert service.secrets.get('mail_password') == 'abcd efgh ijkl mnop'
    client.logout.assert_called_once()


def test_gmail_authentication_failure_clears_connected_status(gmail):
    store, service = gmail
    store.execute('UPDATE mail_accounts SET connected=1')
    def refuse(*args):
        raise MailAuthenticationError('Gmail отклонил вход. Добавьте пароль приложения.')
    service._imap = refuse
    with pytest.raises(MailAuthenticationError, match='пароль приложения'):
        service.test()
    assert store.one('SELECT connected FROM mail_accounts')['connected'] == 0


def test_gmail_network_failure_is_not_reported_as_wrong_password(gmail):
    _, service = gmail
    def unavailable(*args):
        raise TimeoutError('network timeout')
    service._imap = unavailable
    with pytest.raises(RuntimeError, match='интернет и адреса IMAP/SMTP'):
        service.test()


def test_gmail_allowed_send_and_reply_remain_in_product_branch(gmail):
    store, service = gmail
    store.execute('UPDATE mail_accounts SET connected=1')
    product = store.ensure_product('Ноутбук')['id']
    company = store.execute('INSERT INTO companies(name,created_at) VALUES(?,?)', ('Клиент', now()))
    confirm_company_fit(store, product, company)
    store.execute("INSERT INTO company_contacts(company_id,kind,value) VALUES(?,'email',?)", (company, 'buyer@client.ru'))
    store.execute('INSERT INTO permissions(company_id,email,basis,evidence,created_at) VALUES(?,?,?,?,?)',
                  (company, 'buyer@client.ru', 'Согласие', 'Письменное согласие', now()))
    offer = store.execute('INSERT INTO offers(product_id,company_id,subject,body,approved,created_at,updated_at) '
                          'VALUES(?,?,?,?,?,?,?)', (product, company, 'Предложение', 'Согласованный текст', 1, now(), now()))
    smtp = Mock()
    smtp.send_message.return_value = {}
    service._smtp = lambda *args: smtp
    service._append_sent = lambda *args: None
    assert service.send_offer(offer) == 'Отправлено'
    assert service.send_offer(offer) == 'Уже отправлено'
    smtp.send_message.assert_called_once()
    outgoing = store.one('SELECT * FROM mail_messages')
    reply = EmailMessage()
    reply['Message-ID'] = '<reply@client.ru>'
    reply['From'] = 'buyer@client.ru'
    reply['To'] = 'owner@gmail.com'
    reply['Subject'] = 'Re: Предложение'
    reply['In-Reply-To'] = outgoing['message_id']
    reply.set_content('Пришлите подробности')
    class Inbox:
        def select(self, *args, **kwargs):
            return 'OK', [b'1']
        def response(self, name):
            return name, [b'123']
        def uid(self, operation, *args):
            if operation == 'search':
                return 'OK', [b'20']
            if args[1] == '(RFC822.SIZE)':
                return 'OK', [f'1 (UID 20 RFC822.SIZE {len(reply.as_bytes())})'.encode()]
            return 'OK', [(b'1 (UID 20)', reply.as_bytes())]
        def logout(self):
            pass
    service._imap = lambda *args: Inbox()
    service.reconcile = lambda: ''
    assert service.sync() == 'Новых ответов: 1'
    saved = store.one("SELECT * FROM mail_messages WHERE direction='incoming'")
    assert saved['product_id'] == product and saved['company_id'] == company
    assert saved['in_reply_to'] == outgoing['message_id']
    assert saved['is_reply'] == 1


def test_gmail_revoked_password_is_definitive_failed_send_not_uncertain(gmail):
    store, service = gmail
    store.execute('UPDATE mail_accounts SET connected=1')
    product = store.ensure_product('Ноутбук')['id']
    company = store.execute('INSERT INTO companies(name,created_at) VALUES(?,?)', ('Клиент', now()))
    confirm_company_fit(store, product, company)
    store.execute("INSERT INTO company_contacts(company_id,kind,value) VALUES(?,'email',?)", (company, 'buyer@client.ru'))
    store.execute('INSERT INTO permissions(company_id,email,basis,evidence,created_at) VALUES(?,?,?,?,?)',
                  (company, 'buyer@client.ru', 'Согласие', 'Письменное согласие', now()))
    offer = store.execute('INSERT INTO offers(product_id,company_id,subject,body,approved,created_at,updated_at) '
                          'VALUES(?,?,?,?,?,?,?)', (product, company, 'Предложение', 'Текст', 1, now(), now()))
    def revoked(*args):
        raise MailAuthenticationError('Gmail отклонил вход. Обновите пароль приложения.')
    service._smtp = revoked
    with pytest.raises(MailAuthenticationError, match='пароль приложения'):
        service.send_offer(offer)
    assert store.one('SELECT status FROM outbound_attempts')['status'] == 'failed'
    assert not store.one("SELECT id FROM outbound_attempts WHERE status='uncertain'")


def test_gmail_smtp_auth_refusal_never_stores_secret_or_retries_on_restart(gmail, monkeypatch):
    store, service = gmail
    store.execute('UPDATE mail_accounts SET connected=1')
    product = store.ensure_product('Ноутбук')['id']
    company = store.execute('INSERT INTO companies(name,created_at) VALUES(?,?)', ('Клиент', now()))
    confirm_company_fit(store, product, company)
    store.execute("INSERT INTO company_contacts(company_id,kind,value) VALUES(?,'email',?)", (company, 'buyer@client.ru'))
    store.execute('INSERT INTO permissions(company_id,email,basis,evidence,created_at) VALUES(?,?,?,?,?)',
                  (company, 'buyer@client.ru', 'Согласие', 'Письменное согласие', now()))
    offer = store.execute('INSERT INTO offers(product_id,company_id,subject,body,approved,created_at,updated_at) '
                          'VALUES(?,?,?,?,?,?,?)', (product, company, 'Предложение', 'Точный текст', 1, now(), now()))
    client = Mock()
    # A misbehaving server might echo a secret. It must not reach the UI or SQLite.
    client.login.side_effect = smtplib.SMTPAuthenticationError(535, b'bad credential abcdefghijklmnop')
    smtp_class = Mock(return_value=client)
    monkeypatch.setattr(smtplib, 'SMTP_SSL', smtp_class)
    with pytest.raises(MailAuthenticationError) as caught:
        service.send_offer(offer)
    assert 'abcdefghijklmnop' not in str(caught.value)
    client.send_message.assert_not_called()
    attempt = store.one('SELECT * FROM outbound_attempts')
    assert attempt['status'] == 'failed'
    assert 'abcdefghijklmnop' not in attempt['error']
    assert 'abcdefghijklmnop' not in '\n'.join(store.db.iterdump())
    assert store.one('SELECT connected FROM mail_accounts')['connected'] == 0
    path = store.path
    store.close()
    restarted = Store(path)
    try:
        restored_mail = MailService(restarted, Secrets())
        assert restarted.one('SELECT status FROM outbound_attempts')['status'] == 'failed'
        assert restarted.one('SELECT COUNT(*) AS count FROM outbound_attempts')['count'] == 1
        assert restored_mail._match_reply(EmailMessage(), restarted.one('SELECT * FROM mail_accounts')) is None
        with pytest.raises(ValueError, match='подключение почты'):
            restored_mail.sync()
        # Only another explicit connection and send action may retry a failed attempt.
        smtp_class.assert_called_once()
    finally:
        restarted.close()

