"""Synthetic SMTP; asserts no send without explicit conditions and no duplicate sends."""
import pytest
pytest.importorskip('truststore')

from sales_manager.mail.service import MailService
from sales_manager.storage.store import now


class FakeSMTP:
    def __init__(self):
        self.sent = []
    def send_message(self, message, from_addr, to_addrs):
        self.sent.append((message, from_addr, list(to_addrs)))
        return {}
    def quit(self):
        pass


@pytest.fixture
def envelope(isolated_store, fake_secrets):
    s = isolated_store
    pid = s.ensure_product('Смартфоны')['id']
    cid = s.execute('INSERT INTO companies(name,created_at) VALUES(?,?)', ('Buyer QA', now()))
    src = s.put_source('https://buyer.ru/about', text='Мы продаём электронику.')
    s.execute('INSERT INTO company_fit(product_id,company_id,status,reason,source_ids,checked_at) VALUES(?,?,?,?,?,?)',
              (pid, cid, 'FIT', 'Подтверждено', '['+str(src)+']', now()))
    s.execute('INSERT INTO company_contacts(company_id,kind,value) VALUES(?,?,?)', (cid, 'email', 'zakupki@buyer.ru'))
    s.execute('INSERT INTO permissions(company_id,email,basis,evidence,created_at) VALUES(?,?,?,?,?)',
              (cid, 'zakupki@buyer.ru', 'Подтверждение', 'Имеется письменное основание', now()))
    oid = s.execute('INSERT INTO offers(product_id,company_id,subject,body,approved,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                    (pid, cid, 'Сотрудничество', 'Письмо QA', 1, now(), now()))
    s.execute('INSERT INTO mail_accounts(id,email,provider,imap_host,smtp_host,connected) VALUES(1,?,?,?,?,1)',
              ('sender@business.ru', 'manual', 'imap.business.ru', 'smtp.business.ru'))
    mail = MailService(s, fake_secrets)
    fake = FakeSMTP()
    mail._smtp = lambda *args: fake
    mail._append_sent = lambda *args: None
    return s, mail, fake, pid, cid, oid


@pytest.mark.stability
def test_deduplicates_confirmed_sends(envelope):
    s, mail, smtp, *_rest, oid = envelope
    assert mail.send_offer(oid) == 'Отправлено'
    assert mail.send_offer(oid) == 'Уже отправлено'
    assert len(smtp.sent) == 1
    assert len(s.query('SELECT * FROM mail_messages')) == 1
    assert s.one('SELECT status FROM outbound_attempts')['status'] == 'sent'


def test_unapproved_offer_is_never_sent(envelope):
    store, mail, smtp, *_rest, oid = envelope
    store.execute('UPDATE offers SET approved=0 WHERE id=?', (oid,))
    with pytest.raises(ValueError):
        mail.send_offer(oid)
    assert smtp.sent == []
    assert store.query('SELECT * FROM outbound_attempts') == []


def test_missing_contact_permission_blocks_send(envelope):
    store, mail, smtp, *_rest, oid = envelope
    store.execute('DELETE FROM permissions')
    with pytest.raises(ValueError, match='основание'):
        mail.send_offer(oid)
    assert smtp.sent == []


def test_opt_out_blocks_send_even_with_approval(envelope):
    store, mail, smtp, *_rest, oid = envelope
    store.execute('INSERT INTO opt_out(email,created_at) VALUES(?,?)', ('zakupki@buyer.ru', now()))
    with pytest.raises(ValueError, match='отказался'):
        mail.send_offer(oid)
    assert smtp.sent == []


def test_changed_subject_requires_new_explicit_confirmation(envelope):
    store, mail, smtp, *_rest, oid = envelope
    expected = dict(subject='Сотрудничество', body='Письмо QA', recipient='zakupki@buyer.ru', sender='sender@business.ru')
    store.execute('UPDATE offers SET subject=? WHERE id=?', ('Новое содержание', oid))
    with pytest.raises(ValueError, match='Текст письма изменился'):
        mail.send_offer(oid, expected=expected)
    assert store.one('SELECT approved FROM offers WHERE id=?', (oid,))['approved'] == 0
    assert smtp.sent == []


def test_unknown_previous_attempt_blocks_retry(envelope):
    store, mail, smtp, *_rest, oid = envelope
    store.execute('INSERT INTO outbound_attempts(offer_id,idempotency_key,message_id,recipient,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                  (oid, 'qa-unknown', '<synthetic@business.ru>', 'zakupki@buyer.ru', 'uncertain', now(), now()))
    with pytest.raises(ValueError, match='проверяется'):
        mail.send_offer(oid)
    assert smtp.sent == []
