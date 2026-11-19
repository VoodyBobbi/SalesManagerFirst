"""TLS mail providers, durable explicit sends and incremental reply sync."""
import email
import hashlib
import imaplib
import json
import re
import smtplib
import sqlite3
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, getaddresses, make_msgid, parseaddr, parsedate_to_datetime

import truststore

from sales_manager.storage import now
from sales_manager.core.contacts import company_emails, email_purpose, is_contact_email
from sales_manager.core.offer_fields import MAX_SUBJECT_LENGTH
from urllib.parse import urlsplit

MAX_MESSAGE_BYTES = 2_000_000
_IMAP_MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
GMAIL_APP_PASSWORD_URL = 'https://myaccount.google.com/apppasswords'
MAIL_PROVIDERS = (
    ('gmail', 'Gmail (Google)'),
    ('yandex', 'Яндекс.Почта'),
    ('manual', 'Вручную (IMAP/SMTP)'),
)


def save_offer(store, offer_id, subject, body, recipient, approved=False):
    """Update a draft only while no delivery has started or completed."""
    if len(subject) > MAX_SUBJECT_LENGTH:
        raise ValueError(f'Тема письма — не больше {MAX_SUBJECT_LENGTH} символов.')
    if approved and (not subject.strip() or not body.strip()):
        raise ValueError('Введите тему и текст письма перед подтверждением отправки.')
    with store.db as connection:
        result = connection.execute('''UPDATE offers SET subject=?,body=?,recipient=?,
            approved=?,edited=1,updated_at=? WHERE id=? AND NOT EXISTS
            (SELECT 1 FROM outbound_attempts WHERE offer_id=offers.id
             AND status IN ('sending','uncertain','sent'))''',
            (subject, body, recipient, int(approved), now(), offer_id))
        if result.rowcount != 1:
            raise ValueError('Письмо уже отправлено или его отправка проверяется. Текст сохранён в истории.')


def provider_settings(provider):
    """Defaults for an explicit provider selection; never rewrites a saved account."""
    hosts = {
        'gmail': ('imap.gmail.com', 'smtp.gmail.com'),
        'yandex': ('imap.yandex.ru', 'smtp.yandex.ru'),
        'manual': ('', ''),
    }
    if provider not in hosts:
        raise ValueError('Выберите Gmail, Яндекс.Почту или ручное подключение.')
    imap_host, smtp_host = hosts[provider]
    return {'imap_host': imap_host, 'imap_port': 993, 'smtp_host': smtp_host, 'smtp_port': 465}


def provider_help(provider):
    if provider == 'gmail':
        return ('Полный адрес Gmail и отдельный пароль приложения Google. '
                'Включите двухэтапную проверку и создайте пароль приложения в аккаунте Google. '
                'Если пункт недоступен, способ может быть запрещён администратором, '
                'Дополнительной защитой или настройкой только с электронными ключами. '
                'В таком аккаунте этот способ входа не работает; выберите другой ящик. '
                'Основной пароль Google не подходит. Это подключение IMAP/SMTP, не OAuth.')
    if provider == 'yandex':
        return 'Включите IMAP в Яндекс.Почте и создайте отдельный пароль приложения для почты.'
    return 'Укажите IMAP 993 и SMTP 465 (SSL/TLS) или 587 (STARTTLS), адрес ящика и пароль приложения.'


def _is_gmail(account):
    return account.get('provider') == 'gmail' or (
        str(account.get('imap_host', '')).casefold() == 'imap.gmail.com' and
        str(account.get('smtp_host', '')).casefold() == 'smtp.gmail.com')


class MailAuthenticationError(RuntimeError):
    """An authentication refusal, with guidance safe to show without raw credentials."""


class SentReconciliationError(RuntimeError):
    """The Sent folder could not confirm an uncertain submission."""


def _authentication_message(account):
    if _is_gmail(account):
        return ('Gmail отклонил вход. Проверьте полный адрес и отдельный пароль приложения Google. '
                'Включите двухэтапную проверку; после смены пароля Google создайте новый пароль приложения. '
                'Если пароли приложений недоступны из-за политики аккаунта или Дополнительной защиты, '
                'выберите другой ящик либо обратитесь к администратору. Основной пароль не подходит.')
    return 'Почта отклонила вход. Проверьте полный адрес и отдельный пароль приложения в настройках.'


def address(value):
    parsed = parseaddr(str(value))[1].strip().casefold()
    if not re.fullmatch(r"[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+", parsed) or '\r' in str(value) or '\n' in str(value):
        raise ValueError('Укажите корректный email.')
    try:
        parsed.encode('ascii')
    except UnicodeEncodeError as exc:
        raise ValueError('Используйте email латиницей.') from exc
    return parsed


def message_text(message):
    body = message.get_body(preferencelist=('plain', 'html'))
    if body is None:
        return ''
    if body.get_content_type() == 'text/html':
        return _html_text(body)
    try:
        return body.get_content()[:50000]
    except (LookupError, UnicodeError):
        html = message.get_body(preferencelist=('html',))
        return _html_text(html) if html is not None else ''


def _html_text(part):
    from bs4 import BeautifulSoup
    try:
        soup = BeautifulSoup(part.get_content(), 'html.parser')
        for tag in soup(['script', 'style', 'img', 'iframe', 'object']):
            tag.decompose()
        return soup.get_text(' ', strip=True)[:50000]
    except (LookupError, UnicodeError):
        return ''


def is_automatic(message):
    auto = str(message.get('Auto-Submitted', '')).casefold().strip()
    if auto and auto != 'no':
        return True
    if str(message.get('Precedence', '')).casefold() in ('bulk', 'list', 'junk', 'auto_reply'):
        return True
    if message.get('X-Autoreply') or message.get('X-Autorespond'):
        return True
    if message.get_content_type() in ('multipart/report', 'message/delivery-status'):
        return True
    if message.get_param('report-type') in ('delivery-status', 'disposition-notification'):
        return True
    sender = parseaddr(str(message.get('From', '')))[1].casefold()
    if sender.startswith(('mailer-daemon@', 'postmaster@', 'no-reply@', 'noreply@')):
        return True
    subject = str(message.get('Subject', '')).casefold()
    return bool(re.match(r'^(?:автоответ|автоматический ответ|auto(?:matic)?\s*reply|out of office|undeliverable|delivery (?:status|failure))\b', subject))


class MailService:
    account_fields = ('email', 'provider', 'imap_host', 'imap_port', 'smtp_host', 'smtp_port')

    def __init__(self, store, secrets):
        self.store, self.secrets = store, secrets
        self._lock = threading.RLock()

    def _authentication_refused(self, account):
        # A previously verified password can be revoked. Require an explicit
        # connection check again; do not keep advertising a working account.
        if account.get('connected'):
            self.store.execute('UPDATE mail_accounts SET connected=0,updated_at=? '
                               'WHERE id=1 AND lower(email)=? AND imap_host=? AND smtp_host=?',
                               (now(), account['email'].casefold(), account['imap_host'], account['smtp_host']))
        return MailAuthenticationError(_authentication_message(account))

    def _account(self, require_connected=False):
        account = self.store.one('SELECT * FROM mail_accounts WHERE id=1')
        if not account:
            raise ValueError('Подключите почту в настройках.')
        return self._validate_account(account, self.secrets.get('mail_password'), require_connected)

    @staticmethod
    def _validate_account(account, password, require_connected=False):
        account['email'] = address(account['email'])
        if not password:
            if _is_gmail(account):
                raise ValueError('Добавьте отдельный пароль приложения Google в настройках почты. ' + provider_help('gmail'))
            raise ValueError('Добавьте пароль приложения для почты в настройках.')
        if _is_gmail(account):
            # Google displays app passwords in groups; pasted spaces are not part of the secret.
            password = ''.join(password.split())
            if not password:
                raise ValueError('Добавьте отдельный пароль приложения Google в настройках почты.')
        if require_connected and not account['connected']:
            raise ValueError('Проверьте подключение почты в настройках.')
        for key in ('imap_host', 'smtp_host'):
            if not re.fullmatch(r'[A-Za-z0-9.-]+', account.get(key) or ''):
                raise ValueError('Проверьте адреса почтовых серверов.')
        if int(account['imap_port']) != 993 or int(account['smtp_port']) not in (465, 587):
            raise ValueError('Поддерживаются IMAP 993 и SMTP 465/587 с TLS.')
        return account, password

    @staticmethod
    def _tls():
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    def _imap(self, account, password):
        connection = imaplib.IMAP4_SSL(account['imap_host'], int(account['imap_port']),
                                      ssl_context=self._tls(), timeout=30)
        try:
            connection.login(account['email'], password)
            return connection
        except imaplib.IMAP4.abort:
            self._close_imap(connection)
            raise
        except imaplib.IMAP4.error:
            self._close_imap(connection)
            raise self._authentication_refused(account) from None
        except Exception:
            self._close_imap(connection)
            raise

    def _smtp(self, account, password):
        connection = None
        try:
            if int(account['smtp_port']) == 465:
                connection = smtplib.SMTP_SSL(account['smtp_host'], 465, timeout=30, context=self._tls())
            else:
                connection = smtplib.SMTP(account['smtp_host'], 587, timeout=30)
                connection.ehlo()
                if not connection.has_extn('starttls'):
                    raise RuntimeError('SMTP-сервер не поддерживает защищённое соединение.')
                connection.starttls(context=self._tls())
            connection.ehlo()
            connection.login(account['email'], password)
            return connection
        except smtplib.SMTPAuthenticationError:
            self._close_smtp(connection)
            raise self._authentication_refused(account) from None
        except Exception:
            self._close_smtp(connection)
            raise

    @staticmethod
    def _close_imap(connection):
        if connection:
            try:
                connection.logout()
            except Exception:
                pass

    @staticmethod
    def _close_smtp(connection):
        if connection:
            try:
                connection.quit()
            except Exception:
                try:
                    connection.close()
                except Exception:
                    pass

    @staticmethod
    def _sent_folder(connection):
        status, rows = connection.list()
        if status != 'OK':
            return ''
        candidates = []
        for row in rows or []:
            if not row:
                continue
            decoded = row.decode('utf-8', errors='replace') if isinstance(row, bytes) else str(row)
            match = re.match(r'\([^)]*\)\s+(?:"[^"]*"|NIL)\s+(.+)$', decoded)
            if not match:
                continue
            folder = match.group(1).strip()
            if '\\Sent' in decoded or '\\sent' in decoded:
                return folder
            if folder.strip('"').casefold() in ('sent', 'sent items', 'sent messages', '&BB4EQgQ/BEAEMAQyBDsENQQ9BD0ESwQ1-'):
                candidates.append(folder)
        return candidates[0] if candidates else ''

    def test(self):
        with self._lock:
            self.store.execute('UPDATE mail_accounts SET connected=0,updated_at=? WHERE id=1', (now(),))
            account, password = self._account()
            sent = self._probe_connection(account, password)
            self.store.execute('UPDATE mail_accounts SET connected=1,sent_folder=?,updated_at=? WHERE id=1',
                               (sent, now()))
            return 'Почта подключена'

    def verify_connection(self, values, password):
        with self._lock:
            candidate = dict(zip(self.account_fields, values))
            account, password = self._validate_account(candidate, password)
            return self._probe_connection(account, password)

    def _probe_connection(self, account, password):
        imap = smtp = None
        try:
            imap = self._imap(account, password)
            status, _ = imap.select('INBOX', readonly=True)
            if status != 'OK':
                raise RuntimeError('Нет права читать входящие письма.')
            sent = self._sent_folder(imap)
            smtp = self._smtp(account, password)
            code, _ = smtp.mail(account['email'])
            smtp.rset()
            if code != 250:
                raise RuntimeError('Почта не разрешает отправку от этого адреса.')
            return sent
        except (imaplib.IMAP4.error, smtplib.SMTPException, OSError) as exc:
            if isinstance(exc, smtplib.SMTPAuthenticationError):
                raise MailAuthenticationError(_authentication_message(account)) from None
            raise RuntimeError('Почта недоступна. Проверьте интернет и адреса IMAP/SMTP; повторите подключение в настройках.') from exc
        finally:
            self._close_imap(imap)
            self._close_smtp(smtp)

    def _send_gate(self, offer_id, recipient=None, expected=None):
        account, password = self._account(require_connected=True)
        offer = self.store.one('SELECT * FROM offers WHERE id=?', (offer_id,))
        if offer and expected is not None and (offer['subject'] != expected.get('subject') or offer['body'] != expected.get('body')):
            self.store.execute('UPDATE offers SET approved=0 WHERE id=?', (offer_id,))
            raise ValueError('Текст письма изменился. Проверьте его и повторно подтвердите отправку.')
        if not offer or not offer['approved']:
            raise ValueError('Сначала проверьте и подтвердите текст письма.')
        fit = self.store.one('SELECT status FROM company_fit WHERE product_id=? AND company_id=?',
                             (offer['product_id'], offer['company_id']))
        if not fit or fit['status'] != 'FIT':
            raise ValueError('Сначала подтвердите применимость товара в карточке компании.')
        if (not offer['subject'].strip() or not offer['body'].strip()
                or len(offer['subject']) > MAX_SUBJECT_LENGTH):
            raise ValueError('Проверьте тему и текст письма.')
        if '\r' in offer['subject'] or '\n' in offer['subject']:
            raise ValueError('Тема письма должна занимать одну строку.')
        contacts = company_emails(self.store, offer['company_id'])
        if not contacts:
            raise ValueError('Email компании не найден.')
        recipient = address(recipient if recipient is not None else (offer['recipient'] or contacts[0]['email']))
        if offer['recipient'] and address(offer['recipient']) != recipient:
            self.store.execute('UPDATE offers SET approved=0 WHERE id=?', (offer_id,))
            raise ValueError('Email получателя изменился. Проверьте его и повторно подтвердите отправку.')
        if recipient not in {contact['email'] for contact in contacts}:
            raise ValueError('Выберите действующий email из контактов компании.')
        if expected is not None and any(expected.get(key) != value for key, value in
                (('subject', offer['subject']), ('body', offer['body']), ('recipient', recipient), ('sender', account['email']))):
            self.store.execute('UPDATE offers SET approved=0 WHERE id=?', (offer_id,))
            raise ValueError('Текст письма или получатель изменился. Проверьте письмо и повторно подтвердите отправку.')
        if self.store.one('SELECT email FROM opt_out WHERE lower(email)=?', (recipient,)):
            raise ValueError('Отправка запрещена: получатель отказался от писем.')
        permission = self.store.one('SELECT * FROM permissions WHERE company_id=? AND lower(email)=? AND trim(basis)<>\'\' AND trim(evidence)<>\'\' ORDER BY id DESC LIMIT 1',
                                    (offer['company_id'], recipient))
        if not permission:
            raise ValueError('Укажите подтверждённое основание для рекламного контакта в карточке компании.')
        contact = self.store.one(
            "SELECT scope FROM company_contacts WHERE company_id=? AND kind='email' AND lower(value)=?",
            (offer['company_id'], recipient))
        shared = (contact and contact['scope'] == 'hq') or email_purpose(recipient)[1] == 'Общий'
        if shared:
            duplicate = self.store.one(
                "SELECT a.id FROM outbound_attempts a JOIN mail_messages m ON m.message_id=a.message_id "
                "WHERE lower(m.sender)=? AND lower(m.recipient)=? AND m.subject=? AND m.body=? "
                "AND a.status IN ('sent','sending','uncertain') AND a.offer_id<>? LIMIT 1",
                (account['email'].casefold(), recipient, offer['subject'], offer['body'], offer['id']))
            if duplicate:
                raise ValueError('Идентичное письмо на общий адрес уже отправлено или отправляется с этого ящика.')
        return account, password, offer, recipient

    def send_offer(self, offer_id, recipient=None, expected=None):
        """An explicit UI confirmation is required before this method is called."""
        with self._lock:
            account, password, offer, recipient = self._send_gate(offer_id, recipient, expected)
            previous = self.store.query('SELECT * FROM outbound_attempts WHERE offer_id=? ORDER BY id DESC', (offer_id,))
            if any(row['status'] == 'sent' for row in previous):
                return 'Уже отправлено'
            if any(row['status'] in ('sending', 'uncertain') for row in previous):
                raise ValueError('Исход отправки проверяется. Обновите почту, не отправляйте повторно.')
            version = [offer_id, account['email'], recipient, offer['subject'], offer['body']]
            key = hashlib.sha256(json.dumps(version, ensure_ascii=False).encode()).hexdigest()
            old = self.store.one('SELECT * FROM outbound_attempts WHERE idempotency_key=?', (key,))
            if not old:
                for attempt in previous:
                    prior = self.store.one('SELECT * FROM mail_messages WHERE message_id=?', (attempt['message_id'],))
                    if prior and (prior['body'], prior['subject'], prior['sender'], prior['recipient']) == tuple(version[i] for i in (4, 3, 1, 2)):
                        old = attempt
                        break
            if old:
                if old['status'] == 'sent':
                    return 'Уже отправлено'
                if old['status'] in ('sending', 'uncertain'):
                    raise ValueError('Исход отправки проверяется. Обновите почту, не отправляйте повторно.')
                # A definitive SMTP refusal can be retried only by another explicit click.
                prior = self.store.one('SELECT * FROM mail_messages WHERE message_id=?', (old['message_id'],))
                if prior and (prior['body'] != offer['body'] or prior['subject'] != offer['subject'] or prior['sender'] != account['email'] or prior['recipient'] != recipient):
                    raise ValueError('Сохранённая попытка относится к другой версии письма.')
                message_id = old['message_id']
            else:
                message_id = make_msgid(domain=account['email'].split('@')[1])
            msg = EmailMessage(policy=policy.SMTP)
            display_name = str(self.store.get_setting('sender.name', 'Аргон')).replace('\r', '').replace('\n', '')
            msg['From'] = formataddr((display_name, account['email']))
            msg['To'] = recipient
            msg['Subject'] = offer['subject']
            msg['Date'] = format_datetime(datetime.now(timezone.utc))
            msg['Message-ID'] = message_id
            msg.set_content(offer['body'])
            timestamp = now()
            # One atomic transaction reserves the send and freezes exactly what will be sent.
            with self.store.db:
                self.store.db.execute('BEGIN IMMEDIATE')
                # Another service/process may have completed a send since the
                # initial check. Recheck while holding SQLite's write lock;
                # the active-only index cannot guard a now-completed attempt.
                current = self.store.db.execute('SELECT status FROM outbound_attempts WHERE offer_id=?', (offer_id,)).fetchall()
                if any(row['status'] == 'sent' for row in current):
                    return 'Уже отправлено'
                if any(row['status'] in ('sending', 'uncertain') for row in current):
                    raise ValueError('Исход отправки проверяется. Обновите почту, не отправляйте повторно.')
                if old:
                    try:
                        cur = self.store.db.execute("UPDATE outbound_attempts SET status='sending',error='',updated_at=? WHERE id=? AND status='failed'", (timestamp, old['id']))
                    except sqlite3.IntegrityError as exc:
                        raise ValueError('Эта отправка уже обрабатывается.') from exc
                    if cur.rowcount != 1:
                        raise ValueError('Эта отправка уже обрабатывается.')
                else:
                    try:
                        self.store.db.execute('INSERT INTO outbound_attempts(offer_id,idempotency_key,message_id,recipient,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                                              (offer_id, key, message_id, recipient, 'sending', timestamp, timestamp))
                    except sqlite3.IntegrityError as exc:
                        raise ValueError('Эта отправка уже обрабатывается.') from exc
                self.store.db.execute('INSERT OR IGNORE INTO mail_messages(product_id,company_id,message_id,direction,sender,recipient,subject,body,sent_at) VALUES(?,?,?,?,?,?,?,?,?)',
                                      (offer['product_id'], offer['company_id'], message_id, 'outgoing',
                                       account['email'], recipient, offer['subject'], offer['body'], timestamp))
                if old:
                    self.store.db.execute('UPDATE mail_messages SET sent_at=? WHERE message_id=?',
                                          (timestamp, message_id))
            smtp = None
            submitted = False
            try:
                smtp = self._smtp(account, password)
                # Check permission again immediately before DATA.
                current_account, _, current_offer, current_recipient = self._send_gate(offer_id, recipient, expected)
                if current_account['email'] != account['email'] or current_recipient != recipient or current_offer['subject'] != offer['subject'] or current_offer['body'] != offer['body']:
                    self.store.execute('UPDATE offers SET approved=0 WHERE id=?', (offer_id,))
                    raise ValueError('Текст письма изменился. Проверьте его и повторно подтвердите отправку.')
                submitted = True
                refused = smtp.send_message(msg, from_addr=account['email'], to_addrs=[recipient])
                if refused:
                    raise smtplib.SMTPRecipientsRefused(refused)
                self.store.execute("UPDATE outbound_attempts SET status='sent',updated_at=? WHERE message_id=?", (now(), message_id))
                self.store.audit('mail.accepted')
            except MailAuthenticationError as exc:
                self.store.execute("UPDATE outbound_attempts SET status='failed',error=?,updated_at=? WHERE message_id=?",
                                   (str(exc), now(), message_id))
                raise
            except ValueError as exc:
                if submitted:
                    error = 'Исход отправки неизвестен. Обновите почту для проверки.'
                    self.store.execute("UPDATE outbound_attempts SET status='uncertain',error=?,updated_at=? WHERE message_id=?", (error, now(), message_id))
                    raise RuntimeError(error) from exc
                self.store.execute("UPDATE outbound_attempts SET status='failed',error=?,updated_at=? WHERE message_id=?",
                                   (str(exc), now(), message_id))
                raise
            except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError, smtplib.SMTPAuthenticationError, smtplib.SMTPNotSupportedError) as exc:
                error = ('Почтовый сервер отклонил email получателя. Проверьте адрес.' if isinstance(exc, smtplib.SMTPRecipientsRefused)
                         else 'Почтовый сервер отклонил адрес отправителя.' if isinstance(exc, smtplib.SMTPSenderRefused)
                         else 'Почтовый сервер отклонил письмо. Проверьте текст и ограничения почты.')
                self.store.execute("UPDATE outbound_attempts SET status='failed',error=?,updated_at=? WHERE message_id=?",
                                   (error, now(), message_id))
                raise RuntimeError(error) from exc
            except Exception as exc:
                status = 'uncertain' if submitted else 'failed'
                error = ('Исход отправки неизвестен. Обновите почту для проверки.' if submitted else
                         'Не удалось подключиться к почтовому серверу. Проверьте интернет и настройки почты.'
                         if isinstance(exc, (OSError, smtplib.SMTPException)) else
                         'Ошибка подготовки письма. Черновик сохранён; проверьте его перед повторной отправкой.')
                self.store.execute('UPDATE outbound_attempts SET status=?,error=?,updated_at=? WHERE message_id=?',
                                   (status, error, now(), message_id))
                raise RuntimeError(error) from exc
            finally:
                self._close_smtp(smtp)
            # SMTP success remains success even if saving the sent copy fails.
            self._append_sent(account, password, msg)
            return 'Отправлено'

    def _append_sent(self, account, password, message):
        connection = None
        try:
            connection = self._imap(account, password)
            folder = account.get('sent_folder') or self._sent_folder(connection)
            if folder:
                if not self._find_message(connection, folder, message['Message-ID']):
                    connection.append(folder, '\\Seen', imaplib.Time2Internaldate(time.time()), message.as_bytes(policy=policy.SMTP))
        except Exception:
            self.store.audit('mail.sent_copy_pending')
        finally:
            self._close_imap(connection)

    @staticmethod
    def _find_message(connection, folder, message_id):
        status, _ = connection.select(folder, readonly=True)
        if status != 'OK':
            raise SentReconciliationError('Не удалось открыть папку отправленных для проверки копии.')
        status, result = connection.uid('search', None, 'HEADER', 'Message-ID', '"' + message_id.replace('"', '') + '"')
        if status != 'OK' or not result or not isinstance(result[0], bytes):
            raise SentReconciliationError('Не удалось проверить копию в отправленных.')
        # Confirm the full Message-ID header, not just a substring search.
        for uid in result[0].split():
            state, data = connection.uid('fetch', uid, '(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])')
            if state != 'OK':
                raise SentReconciliationError('Не удалось прочитать заголовок отправленного письма.')
            raw = next((item[1] for item in data or [] if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes)), None)
            if not raw:
                raise SentReconciliationError('Сервер не вернул заголовок отправленного письма.')
            parsed = email.message_from_bytes(raw, policy=policy.default)
            found_id = str(parsed.get('Message-ID', '')).strip()
            if not found_id:
                raise SentReconciliationError('Сервер не вернул идентификатор отправленного письма.')
            if found_id == message_id:
                return True
        return False

    def reconcile(self):
        with self._lock:
            account, password = self._account(require_connected=True)
            attempts = self.store.query(
                "SELECT a.* FROM outbound_attempts a JOIN mail_messages m ON m.message_id=a.message_id WHERE a.status='uncertain' AND m.sender=?",
                (account['email'],))
            if not attempts:
                return 'Отправки проверены'
            connection = None
            found = 0
            try:
                connection = self._imap(account, password)
                folder = account.get('sent_folder') or self._sent_folder(connection)
                if not folder:
                    raise SentReconciliationError('Папка отправленных не найдена.')
                for attempt in attempts:
                    if self._find_message(connection, folder, attempt['message_id']):
                        self.store.execute("UPDATE outbound_attempts SET status='sent',error='',updated_at=? WHERE id=?", (now(), attempt['id']))
                        found += 1
                return 'Подтверждено отправок: ' + str(found)
            except (imaplib.IMAP4.error, OSError) as exc:
                raise SentReconciliationError('Не удалось проверить отправленные письма.') from exc
            finally:
                self._close_imap(connection)

    def _match_reply(self, message, account):
        peer = parseaddr(str(message.get('From', '')))[1].strip().casefold()
        if not peer or peer == account['email']:
            return None
        recipients = {item[1].casefold() for item in getaddresses(message.get_all('To', []) + message.get_all('Cc', []))}
        # Delivered-To helps mailboxes receiving BCC replies.
        delivered = message.get_all('Delivered-To', []) + message.get_all('X-Original-To', [])
        recipients.update(item[1].strip().casefold() for item in getaddresses(delivered))
        if account['email'] not in recipients:
            return None
        # The immediate parent wins over older References, including parents that
        # are already matched inbound messages in the same product workspace.
        parents = re.findall(r'<[^<>\s]+>', str(message.get('In-Reply-To', '')))
        references = re.findall(r'<[^<>\s]+>', str(message.get('References', '')))
        def known(identifier):
            return self.store.one("""SELECT m.* FROM mail_messages m
                LEFT JOIN outbound_attempts a ON a.message_id=m.message_id
                WHERE m.message_id=? AND ((m.direction='outgoing' AND lower(m.sender)=? AND a.status IN ('sent','uncertain'))
                OR (m.direction='incoming' AND lower(m.recipient)=? AND m.is_reply=1))""",
                (identifier, account['email'].casefold(), account['email'].casefold()))
        direct = [row for identifier in dict.fromkeys(reversed(parents)) if (row := known(identifier))]
        candidates = direct or [row for identifier in dict.fromkeys(reversed(references)) if (row := known(identifier))]
        if not candidates or len({(row['product_id'], row['company_id']) for row in candidates}) != 1:
            return None
        row = candidates[0]
        return row if self._company_peer(peer, row) else None

    def _company_peer(self, peer, parent):
        if not is_contact_email(peer):
            return False
        original = parent['recipient'] if parent['direction'] == 'outgoing' else parent['sender']
        if peer == original.casefold():
            return True
        contacts = {row['email'] for row in company_emails(self.store, parent['company_id'])}
        if peer in contacts:
            return True
        company = self.store.one('SELECT domain,url FROM companies WHERE id=?', (parent['company_id'],)) or {}
        domains = {original.rsplit('@', 1)[-1].casefold(),
                   str(company.get('domain') or '').casefold().removeprefix('www.'),
                   (urlsplit(company.get('url') or '').hostname or '').casefold().removeprefix('www.')}
        shared = {'gmail.com', 'googlemail.com', 'yandex.ru', 'yandex.com', 'ya.ru', 'mail.ru', 'bk.ru',
                  'list.ru', 'inbox.ru', 'outlook.com', 'hotmail.com', 'yahoo.com', 'rambler.ru', 'icloud.com', 'proton.me', 'protonmail.com'}
        peer_domain = peer.rsplit('@', 1)[-1]
        return any(domain and not any(domain == host or domain.endswith('.' + host) for host in shared)
                   and (peer_domain == domain or peer_domain.endswith('.' + domain)) for domain in domains)

    def sync(self):
        with self._lock:
            account, password = self._account(require_connected=True)
            sent_warning = ''
            try:
                self.reconcile()
            except SentReconciliationError as exc:
                sent_warning = f'{exc} Неясные отправки сохранены.'
            connection = None
            matched = 0
            try:
                connection = self._imap(account, password)
                status, _ = connection.select('INBOX', readonly=True)
                if status != 'OK':
                    raise RuntimeError('Входящие письма недоступны.')
                validity_data = connection.response('UIDVALIDITY')[1] or [b'']
                validity = validity_data[0].decode('ascii', errors='ignore') if isinstance(validity_data[0], bytes) else str(validity_data[0])
                last_uid = int(account['last_uid'] or 0)
                if validity != account['uidvalidity']:
                    last_uid = 0
                elif last_uid and validity.isdecimal():
                    # A cursor at or beyond the next UID cannot belong to this
                    # mailbox state. Older builds could preserve an old cursor.
                    next_data = connection.response('UIDNEXT')[1]
                    next_value = next_data[0] if next_data else None
                    if (isinstance(next_value, bytes) and re.fullmatch(rb'[1-9][0-9]{0,9}', next_value)
                            and int(next_value) <= min(last_uid, 2**32 - 1)):
                        last_uid = 0
                if last_uid:
                    criteria = ('UID', str(last_uid + 1) + ':*')
                else:
                    earliest = self.store.one("SELECT MIN(sent_at) AS first FROM mail_messages WHERE direction='outgoing' AND sender=?", (account['email'],))['first']
                    try:
                        since = datetime.fromisoformat(earliest) - timedelta(days=1) if earliest else datetime.now(timezone.utc) - timedelta(days=7)
                    except ValueError:
                        since = datetime.now(timezone.utc) - timedelta(days=30)
                    # IMAP requires English month names regardless of LC_TIME.
                    criteria = ('SINCE', f'{since.day:02d}-{_IMAP_MONTHS[since.month - 1]}-{since.year:04d}')
                state, result = connection.uid('search', None, *criteria)
                if state != 'OK' or not result or (result[0] is not None and not isinstance(result[0], bytes)):
                    raise RuntimeError('Не удалось получить новые письма.')
                uids = sorted({int(x) for x in (result[0] or b'').split() if x.isdigit() and int(x) > last_uid})[:100]
                for uid in uids:
                    # Fetch size first, then a bounded body. Never set Seen.
                    state, size_data = connection.uid('fetch', str(uid), '(RFC822.SIZE)')
                    size_blob = b' '.join(item for item in size_data or [] if isinstance(item, bytes))
                    sizes = re.findall(rb'RFC822.SIZE (\d+)', size_blob)
                    if state != 'OK' or not sizes:
                        raise RuntimeError('Не удалось прочитать письмо. Проверка продолжится с него при следующем обновлении.')
                    large = int(sizes[0]) > MAX_MESSAGE_BYTES
                    if large:
                        state, data = connection.uid('fetch', str(uid), '(BODY.PEEK[HEADER]<0.262144>)')
                    else:
                        state, data = connection.uid('fetch', str(uid), '(BODY.PEEK[]<0.' + str(MAX_MESSAGE_BYTES) + '>)')
                    if state != 'OK':
                        raise RuntimeError('Не удалось прочитать письмо. Проверка продолжится с него при следующем обновлении.')
                    raw = next((item[1] for item in data or [] if isinstance(item, tuple)), None)
                    if not isinstance(raw, bytes) or not raw:
                        raise RuntimeError('Сервер не вернул содержимое письма. Повторите обновление почты.')
                    if not large and len(raw) < int(sizes[0]):
                        raise RuntimeError('Сервер вернул неполное письмо. Повторите обновление почты.')
                    if large and b'\r\n\r\n' not in raw and b'\n\n' not in raw:
                        raise RuntimeError('Не удалось прочитать полные заголовки большого письма. Повторите обновление почты.')
                    if raw:
                        message = email.message_from_bytes(raw, policy=policy.default)
                        source = self._match_reply(message, account)
                        message_id = str(message.get('Message-ID', '')).strip()
                        if source and message_id and len(message_id) < 500:
                            auto = is_automatic(message)
                            try:
                                sent_at = parsedate_to_datetime(str(message.get('Date', ''))).astimezone(timezone.utc).isoformat()
                            except (TypeError, ValueError, OverflowError):
                                sent_at = now()
                            exists = self.store.one('SELECT id FROM mail_messages WHERE message_id=?', (message_id,))
                            self.store.execute('INSERT OR IGNORE INTO mail_messages(product_id,company_id,message_id,in_reply_to,refs,direction,sender,recipient,subject,body,sent_at,is_reply) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                                               (source['product_id'], source['company_id'], message_id, str(message.get('In-Reply-To', '')),
                                                str(message.get('References', '')), 'incoming', parseaddr(str(message.get('From', '')))[1].casefold(),
                                                account['email'], str(message.get('Subject', ''))[:500],
                                                'Текст не загружен: письмо больше 2 МБ.' if large else message_text(message), sent_at, int(not auto)))
                            if not auto and not exists:
                                matched += 1
                    # Commit checkpoint only after successful processing.
                    self.store.execute('UPDATE mail_accounts SET last_uid=?,uidvalidity=?,updated_at=? WHERE id=1',
                                       (uid, validity, now()))
                if not uids:
                    self.store.execute('UPDATE mail_accounts SET last_uid=?,uidvalidity=?,updated_at=? WHERE id=1',
                                       (last_uid, validity, now()))
                result = f'Новых ответов: {matched}' if matched else 'Почта обновлена'
                return f'{result}. {sent_warning}' if sent_warning else result
            except (imaplib.IMAP4.error, OSError) as exc:
                raise RuntimeError('Не удалось обновить почту. Проверьте соединение.') from exc
            finally:
                self._close_imap(connection)

    def disconnect(self):
        with self._lock:
            self.store.execute('UPDATE mail_accounts SET connected=0,updated_at=? WHERE id=1', (now(),))
            self.secrets.delete('mail_password')
            return 'Почта отключена'
