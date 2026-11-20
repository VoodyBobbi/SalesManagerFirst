import email
import imaplib
import smtplib
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock

import pytest

from sales_manager.mail import MailService
from sales_manager.mail.service import MailCancelled, is_automatic, message_text, offer_delivery_body
from sales_manager.storage import Store, now


class Secrets:
    def get(self, key, default=''):
        return 'test-password' if key == 'mail_password' else default
    def delete(self, key):
        pass


class SMTP:
    def __init__(self):
        self.messages = []
    def send_message(self, message, from_addr, to_addrs):
        self.messages.append((message, from_addr, to_addrs))
        return {}
    def quit(self):
        pass


@pytest.fixture
def mail_case(tmp_path):
    store = Store(tmp_path / 'mail.sqlite3')
    pid = store.ensure_product('Смартфон')['id']
    cid = store.execute('INSERT INTO companies(name,created_at) VALUES(?,?)', ('Клиент', now()))
    source = store.put_source('https://client.test/about', 'Компания', 'Компания продаёт электронику и смартфоны.')
    store.execute('INSERT INTO company_fit(product_id,company_id,status,reason,source_ids,checked_at) VALUES(?,?,?,?,?,?)',
                  (pid, cid, 'FIT', 'Ассортимент электроники соответствует товару.', '[' + str(source) + ']', now()))
    store.execute("INSERT INTO company_contacts(company_id,kind,value) VALUES(?,'email',?)", (cid, 'buyer@client.ru'))
    store.execute('INSERT INTO permissions(company_id,email,basis,evidence,created_at) VALUES(?,?,?,?,?)',
                  (cid, 'buyer@client.ru', 'Согласие', 'Письменное согласие клиента', now()))
    offer = store.execute('INSERT INTO offers(product_id,company_id,subject,body,approved,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                          (pid, cid, 'Предложение', 'Точный текст письма', 1, now(), now()))
    store.execute('INSERT INTO mail_accounts(id,email,provider,imap_host,smtp_host,connected) VALUES(1,?,?,?,?,1)',
                  ('owner@sender.test', 'manual', 'imap.sender.test', 'smtp.sender.test'))
    mail = MailService(store, Secrets())
    smtp = SMTP()
    mail._smtp = lambda a, p: smtp
    mail._append_sent = lambda *args: None
    return store, mail, smtp, offer, pid, cid


def test_send_is_idempotent_and_snapshot_exact(mail_case):
    store, mail, smtp, offer, pid, cid = mail_case
    assert mail.send_offer(offer) == 'Отправлено'
    assert mail.send_offer(offer) == 'Уже отправлено'
    assert len(smtp.messages) == 1
    snapshot = store.one('SELECT * FROM mail_messages')
    assert snapshot['product_id'] == pid and snapshot['company_id'] == cid
    assert snapshot['body'] == 'Точный текст письма'
    assert snapshot['sender'] == 'owner@sender.test'
    assert store.one('SELECT status FROM outbound_attempts')['status'] == 'sent'


def test_default_daily_limit_counts_local_day_for_this_mailbox(mail_case):
    store, mail, smtp, original, _, cid = mail_case
    assert mail.send_offer(original) == 'Отправлено'
    for number in range(2, 22):
        product = store.ensure_product(f'Товар {number}')['id']
        store.execute("INSERT INTO company_fit(product_id,company_id,status) VALUES(?,?,'FIT')", (product, cid))
        candidate = store.execute(
            'INSERT INTO offers(product_id,company_id,subject,body,approved,created_at,updated_at) VALUES(?,?,?,?,1,?,?)',
            (product, cid, f'Предложение {number}', 'Текст', now(), now()))
        if number <= 20:
            assert mail.send_offer(candidate) == 'Отправлено'
    with pytest.raises(ValueError, match='дневной лимит.*20'):
        mail.send_offer(candidate)
    assert mail.send_offer(original) == 'Уже отправлено'
    assert len(smtp.messages) == 20
    assert not store.one('SELECT id FROM outbound_attempts WHERE offer_id=?', (candidate,))

    past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    store.execute('UPDATE mail_messages SET sent_at=? WHERE id=1', (past,))
    assert mail.send_offer(candidate) == 'Отправлено'


def test_shared_address_send_ignores_its_own_reservation_but_blocks_other_offer(mail_case):
    store, mail, smtp, offer, pid, cid = mail_case
    store.execute("INSERT INTO company_contacts(company_id,kind,value,scope) VALUES(?,'email','info@client.ru','hq')", (cid,))
    store.execute('INSERT INTO permissions(company_id,email,basis,evidence,created_at) VALUES(?,?,?,?,?)',
                  (cid, 'info@client.ru', 'Основание', 'Источник подтверждения', now()))
    assert mail.send_offer(offer, recipient='info@client.ru') == 'Отправлено'
    assert mail.send_offer(offer, recipient='info@client.ru') == 'Уже отправлено'
    assert len(smtp.messages) == 1

    second = store.execute('INSERT INTO companies(name,created_at) VALUES(?,?)', ('Другой клиент', now()))
    source = store.put_source('https://other.test/', 'Другая компания', 'Информация')
    store.execute('INSERT INTO company_fit(product_id,company_id,status,reason,source_ids,checked_at) VALUES(?,?,?,?,?,?)',
                  (pid, second, 'FIT', 'Подходит.', f'[{source}]', now()))
    store.execute("INSERT INTO company_contacts(company_id,kind,value,scope) VALUES(?,'email','info@client.ru','hq')", (second,))
    store.execute('INSERT INTO permissions(company_id,email,basis,evidence,created_at) VALUES(?,?,?,?,?)',
                  (second, 'info@client.ru', 'Основание', 'Источник подтверждения', now()))
    second_offer = store.execute('INSERT INTO offers(product_id,company_id,subject,body,approved,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                                 (pid, second, 'Предложение', 'Точный текст письма', 1, now(), now()))
    with pytest.raises(ValueError, match='уже отправлено или отправляется'):
        mail.send_offer(second_offer, recipient='info@client.ru')
    assert len(smtp.messages) == 1


def test_selected_recipient_not_first_inserted_and_has_own_permission(mail_case):
    store, mail, smtp, offer, _, company_id = mail_case
    store.execute("INSERT INTO company_contacts(company_id,kind,value) VALUES(?,'email','zakupki@client.ru')", (company_id,))
    with pytest.raises(ValueError, match='основание'):
        mail.send_offer(offer, recipient='zakupki@client.ru')
    assert not smtp.messages
    store.execute('INSERT INTO permissions(company_id,email,basis,evidence,created_at) VALUES(?,?,?,?,?)',
                  (company_id, 'zakupki@client.ru', 'Согласие', 'Подтверждено', now()))
    mail.send_offer(offer, recipient='zakupki@client.ru')
    assert smtp.messages[0][2] == ['zakupki@client.ru']
    assert store.one('SELECT recipient FROM mail_messages')['recipient'] == 'zakupki@client.ru'


def test_confirmed_snapshot_changed_before_worker_never_connects(mail_case):
    store, mail, smtp, offer, _, _ = mail_case
    expected = dict(subject='Предложение', body='Точный текст письма', recipient='buyer@client.ru', sender='owner@sender.test')
    store.execute("UPDATE offers SET body='Другая версия'")
    with pytest.raises(ValueError, match='Текст письма изменился'):
        mail.send_offer(offer, expected=expected)
    assert not smtp.messages and not store.query('SELECT * FROM outbound_attempts')
    assert store.one('SELECT approved FROM offers')['approved'] == 0


def test_changed_text_can_send_after_new_explicit_approval_preserving_failed_history(mail_case):
    store, mail, smtp, offer, _, _ = mail_case
    def connect(*args):
        store.execute("UPDATE offers SET body='Новая версия'")
        return smtp
    mail._smtp = connect
    with pytest.raises(ValueError, match='Текст письма изменился'):
        mail.send_offer(offer)
    assert store.one('SELECT error FROM outbound_attempts')['error'].startswith('Текст письма изменился')
    store.execute('UPDATE offers SET approved=1')
    mail._smtp = lambda *args: smtp
    assert mail.send_offer(offer) == 'Отправлено'
    assert [r['body'] for r in store.query('SELECT body FROM mail_messages ORDER BY id')] == ['Точный текст письма', 'Новая версия']
    assert [r['status'] for r in store.query('SELECT status FROM outbound_attempts ORDER BY id')] == ['failed','sent']


def test_connection_error_has_connection_message_and_does_not_submit(mail_case):
    store, mail, smtp, offer, _, _ = mail_case
    def offline(*args):
        raise OSError('socket unavailable')
    mail._smtp = offline
    with pytest.raises(RuntimeError, match='подключиться к почтовому серверу'):
        mail.send_offer(offer)
    assert not smtp.messages
    assert store.one('SELECT status FROM outbound_attempts')['status'] == 'failed'


def test_recipient_changed_during_smtp_preparation_is_not_silently_overridden(mail_case):
    store, mail, smtp, offer, _, company_id = mail_case
    store.execute("UPDATE offers SET recipient='buyer@client.ru'")
    store.execute("INSERT INTO company_contacts(company_id,kind,value) VALUES(?,'email','info@client.ru')", (company_id,))
    def connect(*args):
        store.execute("UPDATE offers SET recipient='info@client.ru'")
        return smtp
    mail._smtp = connect
    with pytest.raises(ValueError, match='Email получателя изменился'):
        mail.send_offer(offer, recipient='buyer@client.ru')
    assert not smtp.messages
    assert store.one('SELECT approved FROM offers')['approved'] == 0


def test_test_addresses_cannot_be_sent_even_if_they_have_permission(mail_case):
    store, mail, smtp, offer, _, _ = mail_case
    store.execute("UPDATE company_contacts SET value='sales@example.com'")
    with pytest.raises(ValueError, match='нет email для отправки'):
        mail.send_offer(offer)
    assert not smtp.messages


def test_permission_gate_and_optout_priority(mail_case):
    store, mail, smtp, offer, _, _ = mail_case
    store.execute("INSERT INTO opt_out(email,created_at) VALUES('buyer@client.ru',?)", (now(),))
    with pytest.raises(ValueError, match='отказался'):
        mail.send_offer(offer)
    assert not smtp.messages
    assert not store.query('SELECT * FROM outbound_attempts')


def test_public_email_is_not_consent(mail_case):
    store, mail, smtp, offer, _, _ = mail_case
    store.execute('DELETE FROM permissions')
    with pytest.raises(ValueError, match='основание'):
        mail.send_offer(offer)
    assert not smtp.messages


def test_unapproved_draft_cannot_send(mail_case):
    store, mail, smtp, offer, _, _ = mail_case
    store.execute('UPDATE offers SET approved=0')
    with pytest.raises(ValueError, match='подтвердите'):
        mail.send_offer(offer)


def test_uncertain_submission_never_retried(mail_case):
    store, mail, smtp, offer, _, _ = mail_case
    def lost(*args, **kwargs):
        raise smtplib.SMTPServerDisconnected('connection lost')
    smtp.send_message = lost
    with pytest.raises(RuntimeError, match='неизвестен'):
        mail.send_offer(offer)
    assert store.one('SELECT status FROM outbound_attempts')['status'] == 'uncertain'
    with pytest.raises(ValueError, match='проверяется'):
        mail.send_offer(offer)


def test_definitive_refusal_can_retry_explicitly(mail_case):
    store, mail, smtp, offer, _, _ = mail_case
    def refuse(*args, **kwargs):
        raise smtplib.SMTPRecipientsRefused({'buyer@client.ru': (550, b'no')})
    smtp.send_message = refuse
    with pytest.raises(RuntimeError, match='отклонил'):
        mail.send_offer(offer)
    assert store.one('SELECT status FROM outbound_attempts')['status'] == 'failed'
    smtp.send_message = lambda *args, **kwargs: {}
    assert mail.send_offer(offer) == 'Отправлено'
    assert len(store.query('SELECT * FROM outbound_attempts')) == 1


def make_reply(original, peer='buyer@client.ru', reference=True, auto=False):
    message = EmailMessage()
    message['Message-ID'] = '<reply@client.ru>'
    message['From'] = peer
    message['To'] = 'owner@sender.test'
    message['Subject'] = 'Re: Предложение'
    message['Date'] = 'Thu, 12 Nov 2026 10:00:00 +0500'
    if reference:
        message['In-Reply-To'] = original
        message['References'] = original
    if auto:
        message['Auto-Submitted'] = 'auto-replied'
    message.set_content('Ответ клиента')
    return message


def test_reply_requires_identity_and_ids_not_subject(mail_case):
    store, mail, _, offer, _, _ = mail_case
    mail.send_offer(offer)
    original = store.one('SELECT message_id FROM mail_messages')['message_id']
    account = store.one('SELECT * FROM mail_accounts')
    assert mail._match_reply(make_reply(original), account)
    assert mail._match_reply(make_reply(original, peer='stranger@other.test'), account) is None
    assert mail._match_reply(make_reply(original, reference=False), account) is None
    assert is_automatic(make_reply(original, auto=True))


def test_employee_reply_requires_known_thread_and_company_relation(mail_case):
    store, mail, _, offer, _, _ = mail_case
    mail.send_offer(offer)
    original = store.one('SELECT message_id FROM mail_messages')['message_id']
    account = store.one('SELECT * FROM mail_accounts')
    assert mail._match_reply(make_reply(original, peer='manager@client.ru'), account)
    assert mail._match_reply(make_reply(original, peer='manager@client.ru', reference=False), account) is None
    assert mail._match_reply(make_reply(original, peer='manager@unrelated.ru'), account) is None
    mail._imap = lambda *args: Inbox(make_reply(original, peer='manager@client.ru').as_bytes())
    mail.reconcile = lambda: ''
    assert mail.sync() == 'Новых ответов: 1'
    assert store.one("SELECT sender,is_reply FROM mail_messages WHERE direction='incoming'") == {'sender':'manager@client.ru','is_reply':1}


def test_shared_mail_domain_does_not_make_stranger_a_company_employee(mail_case):
    store, mail, _, offer, _, _ = mail_case
    store.execute("UPDATE company_contacts SET value='client@gmail.com'")
    store.execute("UPDATE permissions SET email='client@gmail.com'")
    mail.send_offer(offer)
    original = store.one('SELECT message_id FROM mail_messages')['message_id']
    assert mail._match_reply(make_reply(original, peer='stranger@gmail.com'), store.one('SELECT * FROM mail_accounts')) is None


class Inbox:
    def __init__(self, raw, validity=b'123'):
        self.raw, self.validity, self.queries = raw, validity, []
    def select(self, folder, readonly=True):
        return 'OK', [b'1']
    def response(self, name):
        return name, [self.validity]
    def uid(self, operation, *args):
        self.queries.append((operation, args))
        if operation == 'search':
            return 'OK', [b'20']
        if operation == 'fetch' and args[1] == '(RFC822.SIZE)':
            return 'OK', [f'1 (UID 20 RFC822.SIZE {len(self.raw)})'.encode()]
        return 'OK', [(b'1 (UID 20)', self.raw), b')']
    def logout(self):
        pass


@pytest.mark.parametrize('provider, alias', [
    ('manual', 'owner@sender.test'), ('gmail', 'owner+unsubscribe@sender.test'),
    ('yandex', 'owner+unsubscribe@sender.test'),
])
def test_mailto_unsubscribe_and_footer_are_delivered_and_processed(mail_case, provider, alias):
    store, mail, smtp, offer, _, _ = mail_case
    store.execute('UPDATE mail_accounts SET provider=?', (provider,))
    mail.send_offer(offer)
    outgoing = email.message_from_bytes(smtp.messages[0][0].as_bytes(), policy=policy.default)
    raw_header = next(value for name, value in outgoing.raw_items() if name == 'List-Unsubscribe')
    assert '=?' not in raw_header
    instructions = urlsplit(str(outgoing['List-Unsubscribe'])[1:-1])
    assert instructions.scheme == 'mailto' and instructions.path == alias
    assert outgoing['List-Unsubscribe-Post'] is None
    wire_body = outgoing.get_content().replace('\r\n', '\n').rstrip('\n')
    assert wire_body == offer_delivery_body('Точный текст письма')
    assert store.one('SELECT delivery_body FROM mail_messages')['delivery_body'] == wire_body
    request = make_reply(outgoing['Message-ID'], reference=False)
    request.replace_header('To', alias)
    request.replace_header('Subject', parse_qs(instructions.query)['subject'][0])
    mail._imap = lambda *args: Inbox(request.as_bytes())
    assert 'Отказов от писем: 1' in mail.sync()
    assert store.one('SELECT email FROM opt_out')['email'] == 'buyer@client.ru'
    assert store.one("SELECT id FROM audit_events WHERE event='mail.opt_out_received'")

    # Old messages can reappear after UIDVALIDITY changes. A verified new
    # consent must survive reprocessing of this already completed request.
    store.execute("DELETE FROM opt_out WHERE email='buyer@client.ru'")
    store.execute("UPDATE mail_accounts SET uidvalidity=''")
    assert 'Отказов от писем' not in mail.sync()
    assert not store.query('SELECT email FROM opt_out')


@pytest.mark.parametrize('auto', [False, True])
def test_direct_unsubscribe_alias_ignores_out_of_office(mail_case, auto):
    store, mail, _, offer, _, _ = mail_case
    mail.send_offer(offer)
    original = store.one('SELECT message_id FROM mail_messages')['message_id']
    request = make_reply(original, reference=False, auto=auto)
    request.replace_header('To', 'owner+unsubscribe@sender.test')
    mail._imap = lambda *args: Inbox(request.as_bytes())
    mail.sync()
    assert bool(store.one('SELECT email FROM opt_out')) is not auto


@pytest.mark.parametrize('confirmed', [False, True])
def test_text_refusal_waits_for_user_confirmation(mail_case, confirmed):
    store, mail, _, offer, _, _ = mail_case
    mail.send_offer(offer)
    original = store.one('SELECT message_id FROM mail_messages')['message_id']
    request = make_reply(original)
    request.set_content('Спасибо за предложение.\nПожалуйста, не пишите больше.')
    mail._imap = lambda *args: Inbox(request.as_bytes())
    assert 'Запросов на отписку для проверки: 1' in mail.sync()
    assert not store.query('SELECT email FROM opt_out')
    pending = mail.pending_opt_outs()
    assert len(pending) == 1 and pending[0]['sender'] == 'buyer@client.ru'
    mail.resolve_opt_out(pending[0]['id'], confirmed)
    assert not mail.pending_opt_outs()
    assert bool(store.one('SELECT email FROM opt_out')) is confirmed


def test_sync_cancels_blocked_socket_and_preserves_checkpoint(mail_case, monkeypatch):
    store, mail, _, _, _, _ = mail_case
    cancel = threading.Event()
    reading = threading.Event()
    inbox = Inbox(b'')
    client, server = socket.socketpair()
    with client, server, ThreadPoolExecutor(max_workers=1) as pool:
        client.settimeout(3)
        inbox.sock = client
        inbox.file = client.makefile('rb')

        def blocked_uid(*args):
            reading.set()
            inbox.file.read(1)
            return 'NO', []

        inbox.uid = blocked_uid
        inbox.login = Mock()
        inbox.shutdown = lambda: inbox.file.close()
        monkeypatch.setattr(imaplib, 'IMAP4_SSL', lambda *args, **kwargs: inbox)
        result = pool.submit(mail.sync, cancel)
        assert reading.wait(1)
        cancel.set()
        mail.cancel_network()
        with pytest.raises(MailCancelled, match='отменена'):
            result.result(timeout=2)
    assert store.one('SELECT last_uid FROM mail_accounts')['last_uid'] == 0


def test_incremental_sync_preserves_branch_and_ignores_duplicates(mail_case):
    store, mail, _, offer, pid, cid = mail_case
    mail.send_offer(offer)
    original = store.one('SELECT message_id FROM mail_messages')['message_id']
    inbox = Inbox(make_reply(original).as_bytes())
    mail._imap = lambda *args: inbox
    mail.reconcile = lambda: ''
    assert mail.sync() == 'Новых ответов: 1'
    reply = store.one("SELECT * FROM mail_messages WHERE direction='incoming'")
    assert (reply['product_id'], reply['company_id'], reply['is_reply']) == (pid, cid, 1)
    assert store.one('SELECT last_uid,uidvalidity FROM mail_accounts') == {'last_uid': 20, 'uidvalidity': '123'}
    assert mail.sync() == 'Почта обновлена'
    assert len(store.query("SELECT * FROM mail_messages WHERE direction='incoming'")) == 1
    assert any('21:*' in args for op, args in inbox.queries if op == 'search')


def test_empty_sync_persists_uid_reset_and_next_reply_is_found(mail_case):
    store, mail, _, offer, _, _ = mail_case
    mail.send_offer(offer)
    original = store.one('SELECT message_id FROM mail_messages')['message_id']
    store.execute("UPDATE mail_accounts SET last_uid=500,uidvalidity='123' WHERE id=1")
    empty = Inbox(b'', validity=b'124')
    empty.uid = Mock(return_value=('OK', [b'']))
    incoming = Inbox(make_reply(original).as_bytes(), validity=b'124')
    mail._imap = Mock(side_effect=[empty, incoming])

    assert mail.sync() == 'Почта обновлена'
    assert store.one('SELECT last_uid,uidvalidity FROM mail_accounts') == {'last_uid': 0, 'uidvalidity': '124'}
    assert mail.sync() == 'Новых ответов: 1'
    assert store.one("SELECT is_reply FROM mail_messages WHERE direction='incoming'") == {'is_reply': 1}


@pytest.mark.parametrize('next_uid, recovers', [(b'21', True), (None, False), (b'unknown', False), (b'501', False)])
def test_sync_recovers_old_cursor_only_with_valid_uidnext_evidence(mail_case, next_uid, recovers):
    store, mail, _, offer, _, _ = mail_case
    mail.send_offer(offer)
    original = store.one('SELECT message_id FROM mail_messages')['message_id']
    store.execute("UPDATE mail_accounts SET last_uid=500,uidvalidity='123' WHERE id=1")
    inbox = Inbox(make_reply(original).as_bytes())
    inbox.response = Mock(side_effect=lambda name: (name, [inbox.validity if name == 'UIDVALIDITY' else next_uid]))
    mail._imap = Mock(return_value=inbox)

    report = mail.sync()

    assert report == ('Новых ответов: 1' if recovers else 'Почта обновлена')
    assert store.one('SELECT last_uid FROM mail_accounts')['last_uid'] == (20 if recovers else 500)
    assert bool(store.one("SELECT id FROM mail_messages WHERE direction='incoming'")) == recovers


def test_unavailable_sent_folder_does_not_block_incoming_reply(mail_case):
    store, mail, smtp, offer, _, _ = mail_case
    smtp.send_message = Mock(side_effect=smtplib.SMTPServerDisconnected('lost response'))
    with pytest.raises(RuntimeError, match='неизвестен'):
        mail.send_offer(offer)
    original = store.one('SELECT message_id FROM mail_messages')['message_id']
    store.execute("UPDATE mail_accounts SET sent_folder='Sent' WHERE id=1")
    sent = Mock()
    sent.select.return_value = ('NO', [b'Folder renamed'])
    incoming = Inbox(make_reply(original).as_bytes())
    mail._imap = Mock(side_effect=[sent, incoming])

    report = mail.sync()

    assert report.startswith('Новых ответов: 1.')
    assert 'Неясные отправки сохранены' in report
    assert store.one("SELECT is_reply FROM mail_messages WHERE direction='incoming'") == {'is_reply': 1}
    assert store.one('SELECT status FROM outbound_attempts') == {'status': 'uncertain'}
    with pytest.raises(ValueError, match='проверяется'):
        mail.send_offer(offer)
    smtp.send_message.assert_called_once()


def test_auto_reply_saved_but_no_reply_check(mail_case):
    store, mail, _, offer, _, _ = mail_case
    mail.send_offer(offer)
    original = store.one('SELECT message_id FROM mail_messages')['message_id']
    mail._imap = lambda *args: Inbox(make_reply(original, auto=True).as_bytes())
    mail.reconcile = lambda: ''
    assert mail.sync() == 'Почта обновлена'
    assert store.one("SELECT is_reply FROM mail_messages WHERE direction='incoming'")['is_reply'] == 0


def test_edit_while_connecting_prevents_submission(mail_case):
    store, mail, smtp, offer, _, _ = mail_case
    def connect(*args):
        store.execute("UPDATE offers SET body='Изменено во время отправки'")
        return smtp
    mail._smtp = connect
    with pytest.raises(ValueError, match='Текст письма изменился'):
        mail.send_offer(offer)
    assert not smtp.messages
    assert store.one('SELECT status FROM outbound_attempts')['status'] == 'failed'


def test_html_email_never_loads_remote_images():
    msg = EmailMessage()
    msg.set_content('<html><script>bad()</script><img src="https://track.test/pixel">Ответ клиента</html>', subtype='html')
    assert message_text(msg) == 'Ответ клиента'


def test_forwarded_message_attachment_cannot_replace_reply_body():
    reply = EmailMessage()
    reply.set_content('<p>Условия не подходят.</p>', subtype='html')
    forwarded = EmailMessage()
    forwarded.set_content('Старое письмо: предложение принято.')
    reply.add_attachment(forwarded, filename='previous.eml')

    parsed = email.message_from_bytes(reply.as_bytes(), policy=policy.default)

    assert message_text(parsed) == 'Условия не подходят.'


def test_delivery_status_is_not_reply():
    msg = EmailMessage()
    msg['From'] = 'postmaster@sender.test'
    msg['Subject'] = 'Delivery Status Notification'
    msg.set_content('Отказ доставки')
    assert is_automatic(msg)


def test_no_reply_is_reference_only_but_identified_reply_is_recorded(mail_case):
    store, mail, smtp, offer, _, cid = mail_case
    from sales_manager.core.contacts import company_emails, recipient_emails
    store.execute("INSERT INTO company_contacts(company_id,kind,value) VALUES(?,'email','no-reply@client.ru')", (cid,))
    assert len(company_emails(store, cid)) == 2
    assert [item['email'] for item in recipient_emails(store, cid)] == ['buyer@client.ru']
    with pytest.raises(ValueError, match='действующий email'):
        mail.send_offer(offer, recipient='no-reply@client.ru')
    assert not smtp.messages
    mail.send_offer(offer)
    original = store.one('SELECT message_id FROM mail_messages')['message_id']
    reply = make_reply(original, peer='no-reply@client.ru')
    assert is_automatic(reply)
    assert is_automatic(make_reply(original, peer='no-reply@client.ru', auto=True), matched_reply=True)
    mail._imap = lambda *args: Inbox(reply.as_bytes())
    assert mail.sync() == 'Новых ответов: 1'


def test_idna_recipient_uses_ascii_envelope(mail_case):
    store, mail, smtp, offer, _, _ = mail_case
    store.execute("UPDATE company_contacts SET value='sales@фирма.рф'")
    store.execute("UPDATE permissions SET email='sales@xn--80apmps.xn--p1ai'")
    assert mail.send_offer(offer, recipient='sales@фирма.рф') == 'Отправлено'
    assert smtp.messages[0][2] == ['sales@xn--80apmps.xn--p1ai']


def test_reconcile_uses_localized_sent_folder_and_explicit_exit(mail_case):
    store, mail, smtp, offer, _, _ = mail_case
    smtp.send_message = Mock(side_effect=smtplib.SMTPServerDisconnected())
    with pytest.raises(RuntimeError, match='неизвестен'):
        mail.send_offer(offer)
    attempt = store.one('SELECT * FROM outbound_attempts')
    sent = Mock()
    russian = '&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-'
    sent.list.return_value = ('OK', [rb'(\HasNoChildren) "/" "' + russian.encode('ascii') + b'"'])
    assert mail._sent_folder(sent) == '"' + russian + '"'
    mail._imap = Mock(return_value=sent)
    store.execute("UPDATE mail_accounts SET sent_folder='Sent' WHERE id=1")
    mail._find_message = Mock(side_effect=[False, True])
    assert mail.reconcile() == 'Подтверждено отправок: 1'
    assert mail._find_message.call_args.args[1] == '"' + russian + '"'
    with pytest.raises(ValueError, match='изменился'):
        mail.confirm_not_sent(offer, attempt['id'], attempt['updated_at'])

    store.execute("UPDATE outbound_attempts SET status='uncertain' WHERE id=?", (attempt['id'],))
    current = store.one('SELECT * FROM outbound_attempts')
    mail.confirm_not_sent(offer, current['id'], current['updated_at'])
    assert store.one('SELECT status FROM outbound_attempts')['status'] == 'failed'
    assert store.one('SELECT approved FROM offers')['approved'] == 0
    assert store.one("SELECT details FROM audit_events WHERE event='mail.user_confirmed_not_sent'") == {'details': str(attempt['id'])}
    assert smtp.send_message.call_count == 1


def test_reconcile_checks_selected_folder_then_archive(mail_case):
    store, mail, smtp, offer, _, _ = mail_case
    smtp.send_message = Mock(side_effect=smtplib.SMTPServerDisconnected())
    with pytest.raises(RuntimeError, match='неизвестен'):
        mail.send_offer(offer)
    original = store.one('SELECT message_id FROM mail_messages')['message_id']
    server = Mock()
    server.list.return_value = ('OK', [
        rb'(\Noselect) "/" "Not a folder"',
        (rb'(\HasNoChildren) "/" {15}', b'Copy of my mail'),
        rb'(\HasNoChildren \All) "/" "[Gmail]/All Mail"',
    ])
    selected = []

    def select(folder, readonly):
        selected.append(folder)
        return 'OK', []

    def uid(operation, *args):
        if operation == 'search':
            return 'OK', [b'17' if selected[-1] == '"[Gmail]/All Mail"' else b'']
        return 'OK', [(b'header', ('Message-ID: ' + original + '\r\n\r\n').encode())]

    server.select.side_effect = select
    server.uid.side_effect = uid
    mail._imap = lambda *args: server
    assert mail.list_folders() == [
        ('Автоматически', ''), ('Copy of my mail', '"Copy of my mail"'),
        ('[Gmail]/All Mail', '"[Gmail]/All Mail"'),
    ]
    mail.set_sent_folder('"Copy of my mail"')
    assert mail.reconcile() == 'Подтверждено отправок: 1'
    assert selected == ['"Copy of my mail"', '"[Gmail]/All Mail"']


def test_failed_permission_test_clears_previous_connection(mail_case):
    store, mail, smtp, _, _, _ = mail_case
    class NoInbox:
        def select(self, *args, **kwargs):
            return 'NO', [b'denied']
        def logout(self):
            pass
    mail._imap = lambda *args: NoInbox()
    with pytest.raises(RuntimeError, match='Нет права'):
        mail.test()
    assert store.one('SELECT connected FROM mail_accounts')['connected'] == 0


def test_sent_search_requires_exact_identifier():
    class Sent:
        def select(self, *args, **kwargs):
            return 'OK', []
        def uid(self, op, *args):
            if op == 'search':
                return 'OK', [b'55']
            return 'OK', [(b'', b'Message-ID: <prefix-original@client.ru>\r\n\r\n')]
    assert not MailService._find_message(Sent(), 'Sent', '<original@client.ru>')
    assert MailService._find_message(Sent(), 'Sent', '<prefix-original@client.ru>')


@pytest.mark.parametrize('failure', ['select', 'search', 'fetch', 'empty_fetch', 'missing_header'])
def test_sent_copy_not_appended_if_lookup_fails(mail_case, failure):
    store, mail, _, _, _, _ = mail_case
    class Sent:
        appended = False
        def select(self, *args, **kwargs):
            return ('NO', []) if failure == 'select' else ('OK', [])
        def uid(self, op, *args):
            if op == 'search':
                return ('NO', []) if failure == 'search' else ('OK', [b'55'])
            if failure == 'missing_header':
                return 'OK', [(b'header', b'From: test@example.test\r\n\r\n')]
            return ('NO', []) if failure == 'fetch' else ('OK', [])
        def append(self, *args):
            self.appended = True
        def logout(self):
            pass
    sent = Sent()
    mail._imap = lambda *args: sent
    message = EmailMessage()
    message['Message-ID'] = '<original@client.ru>'
    MailService._append_sent(mail, {'sent_folder': 'Sent'}, 'password', message)
    assert not sent.appended
    assert store.one("SELECT id FROM audit_events WHERE event='mail.sent_copy_pending'")


@pytest.mark.parametrize('header_failure', [False, True])
def test_large_reply_headers_are_saved_or_checkpoint_retained(mail_case, header_failure):
    store, mail, _, offer, pid, cid = mail_case
    mail.send_offer(offer)
    original = store.one('SELECT message_id FROM mail_messages')['message_id']
    reply = make_reply(original).as_bytes(policy=policy.SMTP)
    headers = reply.split(b'\r\n\r\n', 1)[0] + b'\r\n\r\n'
    class LargeInbox(Inbox):
        def uid(self, operation, *args):
            if operation == 'fetch' and args[1] == '(RFC822.SIZE)':
                return 'OK', [b'1 (UID 20 RFC822.SIZE 3000000)']
            if operation == 'fetch' and 'HEADER' in args[1]:
                return ('NO', []) if header_failure else ('OK', [(b'header', headers)])
            return super().uid(operation, *args)
    mail._imap = lambda *args: LargeInbox(headers)
    mail.reconcile = lambda: ''
    if header_failure:
        with pytest.raises(RuntimeError, match='прочитать'):
            mail.sync()
        assert store.one('SELECT last_uid FROM mail_accounts')['last_uid'] == 0
        assert not store.one("SELECT id FROM mail_messages WHERE direction='incoming'")
    else:
        assert mail.sync() == 'Новых ответов: 1'
        row = store.one("SELECT * FROM mail_messages WHERE direction='incoming'")
        assert (row['product_id'], row['company_id'], row['is_reply']) == (pid, cid, 1)
        assert 'Текст не загружен' in row['body']
        assert store.one('SELECT last_uid FROM mail_accounts')['last_uid'] == 20


@pytest.mark.parametrize('other_recipient', [None, 'sales@client.ru'])
def test_concurrent_instances_send_once(mail_case, other_recipient):
    store, mail, smtp, offer, _, _ = mail_case
    if other_recipient:
        company_id = store.one('SELECT company_id FROM offers WHERE id=?', (offer,))['company_id']
        store.execute("INSERT INTO company_contacts(company_id,kind,value) VALUES(?,'email',?)", (company_id,other_recipient))
        store.execute('INSERT INTO permissions(company_id,email,basis,evidence,created_at) VALUES(?,?,?,?,?)',
                      (company_id,other_recipient,'Согласие','Документ',now()))
    entered, release = threading.Event(), threading.Event()
    errors = []
    original = smtp.send_message
    def slow_send(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    smtp.send_message = slow_send
    def worker():
        try:
            mail.send_offer(offer, recipient='buyer@client.ru')
        except Exception as exc:
            errors.append(exc)
        finally:
            store.close()
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(5)
        other = MailService(store, Secrets())
        other._smtp = lambda *args: smtp
        with pytest.raises(ValueError, match='проверяется'):
            other.send_offer(offer, recipient=other_recipient)
    finally:
        release.set()
        thread.join(timeout=5)
    assert not errors
    assert len(smtp.messages) == 1


@pytest.mark.parametrize('status', ['INSUFFICIENT_EVIDENCE', 'NOT_FIT', None])
def test_contact_and_permission_do_not_allow_send_without_confirmed_fit(mail_case, status):
    store, mail, smtp, offer, product, company = mail_case
    if status is None:
        store.execute('DELETE FROM company_fit WHERE product_id=? AND company_id=?', (product, company))
    else:
        store.execute('UPDATE company_fit SET status=? WHERE product_id=? AND company_id=?', (status, product, company))
    with pytest.raises(ValueError, match='применимость'):
        mail.send_offer(offer)
    assert not smtp.messages
    assert not store.query('SELECT * FROM outbound_attempts')


def test_immediate_inbound_parent_wins_over_other_product_references(mail_case):
    store, mail, _, offer_a, product_a, company = mail_case
    mail.send_offer(offer_a)
    outgoing_a = store.one("SELECT message_id FROM mail_messages WHERE product_id=?", (product_a,))['message_id']
    incoming_a = '<first-reply-product-a@client.ru>'
    store.execute('INSERT INTO mail_messages(product_id,company_id,message_id,in_reply_to,direction,sender,recipient,subject,body,sent_at,is_reply) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                  (product_a, company, incoming_a, outgoing_a, 'incoming', 'buyer@client.ru', 'owner@sender.test', 'Ответ A', 'Текст A', now(), 1))
    product_b = store.ensure_product('Ноутбук')['id']
    store.execute('INSERT INTO company_fit(product_id,company_id,status,reason,source_ids,checked_at) SELECT ?,company_id,status,reason,source_ids,checked_at FROM company_fit WHERE product_id=? AND company_id=?', (product_b, product_a, company))
    offer_b = store.execute('INSERT INTO offers(product_id,company_id,subject,body,approved,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                            (product_b, company, 'Предложение B', 'Текст B', 1, now(), now()))
    mail.send_offer(offer_b)
    outgoing_b = store.one("SELECT message_id FROM mail_messages WHERE product_id=?", (product_b,))['message_id']
    response = make_reply(incoming_a)
    response.replace_header('References', outgoing_b)
    match = mail._match_reply(response, store.one('SELECT * FROM mail_accounts'))
    assert match['product_id'] == product_a
    assert match['message_id'] == incoming_a


def test_immediate_outbound_parent_wins_over_other_product_references(mail_case):
    store, mail, _, offer_a, product_a, company = mail_case
    mail.send_offer(offer_a)
    outgoing_a = store.one("SELECT message_id FROM mail_messages WHERE product_id=?", (product_a,))['message_id']
    product_b = store.ensure_product('Ноутбук')['id']
    store.execute('INSERT INTO company_fit(product_id,company_id,status,reason,source_ids,checked_at) SELECT ?,company_id,status,reason,source_ids,checked_at FROM company_fit WHERE product_id=? AND company_id=?', (product_b, product_a, company))
    offer_b = store.execute('INSERT INTO offers(product_id,company_id,subject,body,approved,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                            (product_b, company, 'Предложение B', 'Текст B', 1, now(), now()))
    mail.send_offer(offer_b)
    outgoing_b = store.one("SELECT message_id FROM mail_messages WHERE product_id=?", (product_b,))['message_id']
    response = make_reply(outgoing_a)
    response.replace_header('References', outgoing_b)
    assert mail._match_reply(response, store.one('SELECT * FROM mail_accounts'))['product_id'] == product_a


def test_completed_send_after_initial_read_cannot_be_sent_again(mail_case, monkeypatch):
    store, mail, smtp, offer, _, company_id = mail_case
    # Reproduce another service completing a send after this worker's initial
    # read but before it reserves the write transaction (including a new key).
    store.execute("INSERT INTO company_contacts(company_id,kind,value) VALUES(?,'email','sales@client.ru')", (company_id,))
    store.execute('INSERT INTO permissions(company_id,email,basis,evidence,created_at) VALUES(?,?,?,?,?)',
                  (company_id, 'sales@client.ru', 'Согласие', 'Документ', now()))
    real_query = store.query
    captured = False
    def racing_query(sql, params=()):
        nonlocal captured
        rows = real_query(sql, params)
        if not captured and sql.startswith('SELECT * FROM outbound_attempts WHERE offer_id='):
            captured = True
            other = MailService(store, Secrets())
            other._smtp = lambda *args: smtp
            other._append_sent = lambda *args: None
            assert other.send_offer(offer, recipient='buyer@client.ru') == 'Отправлено'
        return rows
    monkeypatch.setattr(store, 'query', racing_query)
    assert mail.send_offer(offer, recipient='sales@client.ru') == 'Уже отправлено'
    assert len(smtp.messages) == 1
    assert len(real_query('SELECT * FROM outbound_attempts')) == 1


@pytest.mark.parametrize('failure', ['size', 'body', 'missing_body', 'truncated'])
def test_failed_imap_fetch_keeps_checkpoint_for_retry(mail_case, failure):
    store, mail, _, offer, _, _ = mail_case
    mail.send_offer(offer)
    original = store.one('SELECT message_id FROM mail_messages')['message_id']
    original_inbox = Inbox(make_reply(original).as_bytes())
    real_uid = original_inbox.uid
    def fail_fetch(operation, *args):
        if operation == 'fetch':
            is_size = args[1] == '(RFC822.SIZE)'
            if (failure == 'size' and is_size) or (failure == 'body' and not is_size):
                return 'NO', [b'temporary failure']
            if failure == 'missing_body' and not is_size:
                return 'OK', []
            if failure == 'truncated' and not is_size:
                return 'OK', [(b'partial', original_inbox.raw[:40])]
        return real_uid(operation, *args)
    original_inbox.uid = fail_fetch
    mail._imap = lambda *args: original_inbox
    mail.reconcile = lambda: ''
    with pytest.raises(RuntimeError, match='письм'):
        mail.sync()
    assert store.one('SELECT last_uid FROM mail_accounts')['last_uid'] == 0
    assert not store.query("SELECT * FROM mail_messages WHERE direction='incoming'")
    original_inbox.uid = real_uid
    assert mail.sync() == 'Новых ответов: 1'
    assert store.one('SELECT last_uid FROM mail_accounts')['last_uid'] == 20

