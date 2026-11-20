"""TLS mail providers, durable explicit sends and incremental reply sync."""
import email
import base64
import binascii
import hashlib
import imaplib
import io
import json
import re
import smtplib
import socket
import sqlite3
import ssl
import threading
import time
from contextlib import contextmanager
from datetime import datetime, time as clock_time, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, getaddresses, make_msgid, parseaddr, parsedate_to_datetime

import truststore

from sales_manager.storage import now
from sales_manager.core.contacts import (ascii_domain, company_emails, email_purpose, is_contact_email,
                                         is_service_mailbox, normalize_email, recipient_emails)
from sales_manager.core.offer_fields import MAX_SUBJECT_LENGTH
from urllib.parse import quote, urlsplit

MAX_MESSAGE_BYTES = 2_000_000
DEFAULT_DAILY_LIMIT = 20
_UNSUBSCRIBE_FOOTER = 'Чтобы отказаться от писем, ответьте «Не пишите».'
_OPT_OUT_REQUEST = re.compile(r'(?im)^ *(?:пожалуйста[ ,]+)?(?:не пишите\b|отпишите\b|отпишитесь\b|'
                              r'прошу отписать\b|прошу больше не писать\b|unsubscribe\b|'
                              r'please (?:unsubscribe\b|remove me\b|stop (?:emailing|writing)\b))')
_MAILBOX_WIRE = re.compile(r'"(?:[^"\\\x00-\x1f]|\\["\\])*"|[^\s"{}\\\x00-\x1f]+')
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


class MailCancelled(RuntimeError):
    """The user cancelled an ongoing mail operation."""


class _CancellableMailRead(io.RawIOBase):
    """Poll for cancellation without exposing brief socket timeouts to BufferedReader."""

    def __init__(self, sock, check_cancelled):
        super().__init__()
        self.sock = sock
        self.check_cancelled = check_cancelled

    def readable(self):
        return True

    def readinto(self, buffer):
        # Winsock may leave a timed read blocked after another thread shuts
        # down the local socket. Bound each wait while keeping the full timeout.
        timeout = self.sock.gettimeout()
        deadline = time.monotonic() + timeout if timeout is not None else float('inf')
        try:
            while True:
                self.check_cancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Mail server did not respond')
                self.sock.settimeout(min(0.2, remaining))
                try:
                    return self.sock.recv_into(buffer)
                except TimeoutError:
                    continue
        finally:
            self.sock.settimeout(timeout)


def offer_delivery_body(body):
    return body + ('\n' if body.endswith('\n') else '\n\n') + _UNSUBSCRIBE_FOOTER


def daily_limit(store):
    limit = store.get_setting('mail.daily_limit', DEFAULT_DAILY_LIMIT)
    return limit if type(limit) is int and 1 <= limit <= 1000 else DEFAULT_DAILY_LIMIT


def _unsubscribe_alias(account):
    local, domain = account['email'].rsplit('@', 1)
    return local.split('+', 1)[0] + '+unsubscribe@' + domain


def _unsubscribe_token(message_id):
    return hashlib.sha256(message_id.encode()).hexdigest()[:24]


def _recipient_addresses(message):
    headers = ['To', 'Cc', 'Delivered-To', 'X-Original-To']
    return {normalize_email(item[1]) for item in getaddresses(
        [value for header in headers for value in message.get_all(header, [])])}


def _authentication_message(account):
    if _is_gmail(account):
        return ('Gmail отклонил вход. Проверьте полный адрес и отдельный пароль приложения Google. '
                'Включите двухэтапную проверку; после смены пароля Google создайте новый пароль приложения. '
                'Если пароли приложений недоступны из-за политики аккаунта или Дополнительной защиты, '
                'выберите другой ящик либо обратитесь к администратору. Основной пароль не подходит.')
    return 'Почта отклонила вход. Проверьте полный адрес и отдельный пароль приложения в настройках.'


def address(value):
    parsed = normalize_email(parseaddr(str(value))[1])
    if not parsed or '\r' in str(value) or '\n' in str(value):
        raise ValueError('Укажите корректный email.')
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


def is_automatic(message, *, matched_reply=False):
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
    if sender.startswith(('mailer-daemon@', 'postmaster@')) or (is_service_mailbox(sender) and not matched_reply):
        return True
    subject = str(message.get('Subject', '')).casefold()
    return bool(re.match(r'^(?:автоответ|автоматический ответ|auto(?:matic)?\s*reply|out of office|undeliverable|delivery (?:status|failure))\b', subject))


def _mailbox_name(folder):
    """Decode for comparison, keeping the original IMAP argument for commands."""
    def decode(match):
        encoded = match[1].replace(',', '/')
        if not encoded:
            return '&'
        try:
            return base64.b64decode(encoded + '=' * (-len(encoded) % 4), validate=True).decode('utf-16-be')
        except (binascii.Error, UnicodeError):
            return match[0]
    if folder.startswith('"') and folder.endswith('"'):
        folder = re.sub(r'\\(["\\])', r'\1', folder[1:-1])
    return re.sub(r'&([A-Za-z0-9+,]*)-', decode, folder)


def _mailbox_argument(folder):
    return folder if folder.isascii() else folder.encode('utf-8')


class MailService:
    account_fields = ('email', 'provider', 'imap_host', 'imap_port', 'smtp_host', 'smtp_port')

    def __init__(self, store, secrets):
        self.store, self.secrets = store, secrets
        self._lock = threading.RLock()
        self._network_lock = threading.Lock()
        self._active_connections = set()
        self._cancel = None

    @contextmanager
    def _cancellable(self, cancel):
        previous = self._cancel
        if cancel is not None:
            self._cancel = cancel
        try:
            self._check_cancelled()
            yield
        finally:
            self._cancel = previous

    def _check_cancelled(self):
        if self._cancel is not None and self._cancel.is_set():
            raise MailCancelled('Работа с почтой отменена.')

    def _activate(self, connection):
        if self._cancel is not None:
            with self._network_lock:
                self._active_connections.add(connection)
            self._check_cancelled()
            sock = getattr(connection, 'sock', None)
            if isinstance(sock, socket.socket):
                if connection.file is not None:
                    connection.file.close()
                connection.file = io.BufferedReader(_CancellableMailRead(sock, self._check_cancelled))

    def cancel_network(self):
        """Wake blocked reads after the calling window has set its cancellation event."""
        with self._network_lock:
            connections = tuple(self._active_connections)
        for connection in connections:
            sock = getattr(connection, 'sock', None)
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

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
        self._check_cancelled()
        connection = imaplib.IMAP4_SSL(account['imap_host'], int(account['imap_port']),
                                      ssl_context=self._tls(), timeout=30)
        try:
            self._activate(connection)
            connection.login(account['email'], password)
            self._check_cancelled()
            return connection
        except imaplib.IMAP4.abort:
            self._close_imap(connection)
            raise
        except imaplib.IMAP4.error:
            self._close_imap(connection)
            self._check_cancelled()
            raise self._authentication_refused(account) from None
        except Exception:
            self._close_imap(connection)
            raise

    def _smtp(self, account, password):
        connection = None
        try:
            self._check_cancelled()
            if int(account['smtp_port']) == 465:
                connection = smtplib.SMTP_SSL(account['smtp_host'], 465, timeout=30, context=self._tls())
                self._activate(connection)
            else:
                connection = smtplib.SMTP(account['smtp_host'], 587, timeout=30)
                self._activate(connection)
                connection.ehlo()
                self._check_cancelled()
                if not connection.has_extn('starttls'):
                    raise RuntimeError('SMTP-сервер не поддерживает защищённое соединение.')
                connection.starttls(context=self._tls())
                self._activate(connection)
            self._check_cancelled()
            connection.ehlo()
            self._check_cancelled()
            connection.login(account['email'], password)
            self._check_cancelled()
            return connection
        except smtplib.SMTPAuthenticationError:
            self._close_smtp(connection)
            self._check_cancelled()
            raise self._authentication_refused(account) from None
        except Exception:
            self._close_smtp(connection)
            raise

    def _close_imap(self, connection):
        if connection:
            try:
                self._check_cancelled()
                connection.logout()
            except Exception:
                try:
                    connection.shutdown()
                except Exception:
                    pass
            finally:
                with self._network_lock:
                    self._active_connections.discard(connection)

    def _close_smtp(self, connection):
        if connection:
            try:
                self._check_cancelled()
                connection.quit()
            except Exception:
                try:
                    connection.close()
                except Exception:
                    pass
            finally:
                with self._network_lock:
                    self._active_connections.discard(connection)

    @staticmethod
    def _sent_folder(connection):
        return next(iter(MailService._sent_folders(connection)), '')

    @staticmethod
    def _folders(connection):
        listing = connection.list()
        if not isinstance(listing, tuple) or len(listing) != 2:
            return []
        status, rows = listing
        if status != 'OK':
            return []
        folders = []
        for row in rows or []:
            literal = None
            if isinstance(row, tuple) and len(row) == 2:
                row, literal = row
            if not isinstance(row, (bytes, str)) or (literal is not None and not isinstance(literal, bytes)):
                continue
            decoded = row.decode('utf-8', errors='replace') if isinstance(row, bytes) else row
            match = re.fullmatch(r'\(([^)]*)\)\s+(?:"([^"]*)"|NIL)\s+(.+)', decoded)
            if not match:
                continue
            flags = match[1].casefold().split()
            folder = match[3].strip()
            if literal is not None and re.fullmatch(r'\{\d+\}', folder):
                folder = '"' + literal.decode('utf-8', errors='replace').replace('\\', '\\\\').replace('"', '\\"') + '"'
            if '\\noselect' in flags or '\\nonexistent' in flags or not _MAILBOX_WIRE.fullmatch(folder):
                continue
            label = _mailbox_name(folder)
            leaf = label.rsplit(match[2], 1)[-1] if match[2] else label
            folders.append((folder, label, leaf.casefold(), flags))
        return folders

    @staticmethod
    def _sent_folders(connection, include_archive=False):
        sent, named, archive = [], [], []
        for folder, _, name, flags in MailService._folders(connection):
            if '\\sent' in flags:
                sent.append(folder)
            elif name in ('sent', 'sent mail', 'sent items', 'sent messages', 'отправленные',
                          'отправленные письма', 'gesendet', 'envoyés', 'éléments envoyés',
                          'enviados', 'inviata', 'posta inviata'):
                named.append(folder)
            elif include_archive and ('\\all' in flags or '\\archive' in flags or
                    name in ('all mail', 'all messages', 'archive', 'archives', 'архив', 'вся почта',
                             'tous les messages', 'alle nachrichten', 'archiv')):
                archive.append(folder)
        return list(dict.fromkeys(sent + named + archive))

    def list_folders(self, cancel=None):
        with self._lock, self._cancellable(cancel):
            account, password = self._account(require_connected=True)
            connection = None
            try:
                connection = self._imap(account, password)
                folders = self._folders(connection)
                self._check_cancelled()
                if not folders:
                    raise RuntimeError('Почтовый сервер не вернул список доступных папок.')
                return [('Автоматически', '')] + [(label, folder) for folder, label, _, _ in folders]
            except (imaplib.IMAP4.error, OSError) as exc:
                self._check_cancelled()
                raise RuntimeError('Не удалось получить список папок. Проверьте соединение с почтой.') from exc
            finally:
                self._close_imap(connection)

    def set_sent_folder(self, folder):
        # Values come from LIST and are persisted as IMAP command arguments.
        if folder and not _MAILBOX_WIRE.fullmatch(folder):
            raise ValueError('Выберите папку из списка почтового сервера.')
        self.store.execute('UPDATE mail_accounts SET sent_folder=?,updated_at=? WHERE id=1', (folder, now()))

    def test(self, cancel=None):
        with self._lock, self._cancellable(cancel):
            self.store.execute('UPDATE mail_accounts SET connected=0,updated_at=? WHERE id=1', (now(),))
            account, password = self._account()
            sent = self._probe_connection(account, password)
            self._check_cancelled()
            self.store.execute('UPDATE mail_accounts SET connected=1,sent_folder=?,updated_at=? WHERE id=1',
                               (sent, now()))
            return 'Почта подключена'

    def verify_connection(self, values, password, cancel=None):
        with self._lock, self._cancellable(cancel):
            candidate = dict(zip(self.account_fields, values))
            account, password = self._validate_account(candidate, password)
            previous = self.store.one('SELECT * FROM mail_accounts WHERE id=1')
            if previous and all(previous[field] == account[field] for field in self.account_fields):
                account['sent_folder'] = previous['sent_folder']
            sent = self._probe_connection(account, password)
            self._check_cancelled()
            return sent

    def _probe_connection(self, account, password):
        imap = smtp = None
        try:
            imap = self._imap(account, password)
            status, _ = imap.select('INBOX', readonly=True)
            self._check_cancelled()
            if status != 'OK':
                raise RuntimeError('Нет права читать входящие письма.')
            sent = account.get('sent_folder') or self._sent_folder(imap)
            self._check_cancelled()
            smtp = self._smtp(account, password)
            code, _ = smtp.mail(account['email'])
            self._check_cancelled()
            smtp.rset()
            if code != 250:
                raise RuntimeError('Почта не разрешает отправку от этого адреса.')
            return sent
        except (imaplib.IMAP4.error, smtplib.SMTPException, OSError) as exc:
            self._check_cancelled()
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
        contacts = recipient_emails(self.store, offer['company_id'])
        if not contacts:
            raise ValueError('У компании нет email для отправки.')
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

    def _check_daily_limit(self, db, sender):
        limit = daily_limit(self.store)
        day = datetime.now().date()
        start = datetime.combine(day, clock_time.min).astimezone(timezone.utc).isoformat()
        end = datetime.combine(day + timedelta(days=1), clock_time.min).astimezone(timezone.utc).isoformat()
        count = db.execute("""
            SELECT COUNT(*) FROM mail_messages m JOIN outbound_attempts a ON a.message_id=m.message_id
            WHERE m.direction='outgoing' AND lower(m.sender)=?
              AND m.sent_at>=? AND m.sent_at<? AND a.status IN ('sending','uncertain','sent')
        """, (sender.casefold(), start, end)).fetchone()[0]
        if count >= limit:
            raise ValueError(f'Достигнут дневной лимит почтового ящика ({limit}). Повторите отправку завтра.')

    def send_offer(self, offer_id, recipient=None, expected=None, cancel=None):
        """An explicit UI confirmation is required before this method is called."""
        with self._lock, self._cancellable(cancel):
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
            # Mailto works without a web endpoint. Only use plus addressing for
            # providers known to route the alias to this inbox.
            unsubscribe_email = (_unsubscribe_alias(account)
                                 if _is_gmail(account) or account.get('provider') == 'yandex'
                                 else account['email'])
            unsubscribe_subject = quote('unsubscribe ' + _unsubscribe_token(message_id))
            msg['List-Unsubscribe'] = f'<mailto:{quote(unsubscribe_email, safe="@+")}?subject={unsubscribe_subject}>'
            delivery_body = offer_delivery_body(offer['body'])
            msg.set_content(delivery_body)
            # Keep the mailto URL a literal ASCII header even for a long address.
            # The already encoded MIME body keeps its normal short line width.
            msg.policy = policy.SMTP.clone(max_line_length=998)
            timestamp = now()
            # One atomic transaction reserves the send and freezes exactly what will be sent.
            self._check_cancelled()
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
                self._check_daily_limit(self.store.db, account['email'])
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
                self.store.db.execute('INSERT OR IGNORE INTO mail_messages(product_id,company_id,message_id,direction,sender,recipient,subject,body,sent_at,delivery_body) VALUES(?,?,?,?,?,?,?,?,?,?)',
                                      (offer['product_id'], offer['company_id'], message_id, 'outgoing',
                                       account['email'], recipient, offer['subject'], offer['body'], timestamp, delivery_body))
                if old:
                    self.store.db.execute('UPDATE mail_messages SET sent_at=?,delivery_body=? WHERE message_id=?',
                                          (timestamp, delivery_body, message_id))
            smtp = None
            submitted = False
            try:
                smtp = self._smtp(account, password)
                # Check permission again immediately before DATA.
                current_account, _, current_offer, current_recipient = self._send_gate(offer_id, recipient, expected)
                if current_account['email'] != account['email'] or current_recipient != recipient or current_offer['subject'] != offer['subject'] or current_offer['body'] != offer['body']:
                    self.store.execute('UPDATE offers SET approved=0 WHERE id=?', (offer_id,))
                    raise ValueError('Текст письма изменился. Проверьте его и повторно подтвердите отправку.')
                self._check_cancelled()
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
                         str(exc) if isinstance(exc, MailCancelled) else
                         'Не удалось подключиться к почтовому серверу. Проверьте интернет и настройки почты.'
                         if isinstance(exc, (OSError, smtplib.SMTPException)) else
                         'Ошибка подготовки письма. Черновик сохранён; проверьте его перед повторной отправкой.')
                self.store.execute('UPDATE outbound_attempts SET status=?,error=?,updated_at=? WHERE message_id=?',
                                   (status, error, now(), message_id))
                if not submitted:
                    self._check_cancelled()
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
                if not self._find_message(connection, folder, message['Message-ID'], self._check_cancelled):
                    self._check_cancelled()
                    connection.append(_mailbox_argument(folder), '\\Seen', imaplib.Time2Internaldate(time.time()), message.as_bytes())
        except MailCancelled:
            return
        except Exception:
            if self._cancel is None or not self._cancel.is_set():
                self.store.audit('mail.sent_copy_pending')
        finally:
            self._close_imap(connection)

    @staticmethod
    def _find_message(connection, folder, message_id, check_cancelled=None):
        if check_cancelled:
            check_cancelled()
        try:
            status, _ = connection.select(_mailbox_argument(folder), readonly=True)
        except imaplib.IMAP4.abort:
            raise
        except imaplib.IMAP4.error as exc:
            raise SentReconciliationError('Не удалось открыть папку отправленных для проверки копии.') from exc
        if check_cancelled:
            check_cancelled()
        if status != 'OK':
            raise SentReconciliationError('Не удалось открыть папку отправленных для проверки копии.')
        status, result = connection.uid('search', None, 'HEADER', 'Message-ID', '"' + message_id.replace('"', '') + '"')
        if check_cancelled:
            check_cancelled()
        if status != 'OK' or not result or not isinstance(result[0], bytes):
            raise SentReconciliationError('Не удалось проверить копию в отправленных.')
        # Confirm the full Message-ID header, not just a substring search.
        for uid in result[0].split():
            state, data = connection.uid('fetch', uid, '(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])')
            if check_cancelled:
                check_cancelled()
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

    def reconcile(self, cancel=None):
        with self._lock, self._cancellable(cancel):
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
                preferred = account.get('sent_folder')
                fallback = None
                checked = False
                for attempt in attempts:
                    if preferred:
                        try:
                            confirmed = self._find_message(connection, preferred, attempt['message_id'], self._check_cancelled)
                            checked = True
                            if confirmed:
                                self._check_cancelled()
                                found += self._confirm_attempt(attempt['id'])
                                continue
                        except SentReconciliationError:
                            pass
                    if fallback is None:
                        self._check_cancelled()
                        fallback = [folder for folder in self._sent_folders(connection, include_archive=True) if folder != preferred]
                        self._check_cancelled()
                    for folder in fallback:
                        try:
                            confirmed = self._find_message(connection, folder, attempt['message_id'], self._check_cancelled)
                            checked = True
                        except SentReconciliationError:
                            continue
                        if confirmed:
                            self._check_cancelled()
                            found += self._confirm_attempt(attempt['id'])
                            break
                if not checked:
                    raise SentReconciliationError('Не удалось проверить папки отправленных. Проверьте выбранную папку в настройках почты.')
                return 'Подтверждено отправок: ' + str(found)
            except (imaplib.IMAP4.error, OSError) as exc:
                self._check_cancelled()
                raise SentReconciliationError('Не удалось проверить отправленные письма.') from exc
            finally:
                self._close_imap(connection)

    def _confirm_attempt(self, attempt_id):
        with self.store.db as db:
            return db.execute("UPDATE outbound_attempts SET status='sent',error='',updated_at=? "
                              "WHERE id=? AND status='uncertain'", (now(), attempt_id)).rowcount

    def confirm_not_sent(self, offer_id, attempt_id, updated_at):
        """Record explicit user verification and unlock the draft for a new confirmation."""
        timestamp = now()
        with self._lock, self.store.db as db:
            result = db.execute("UPDATE outbound_attempts SET status='failed',error=?,updated_at=? "
                                "WHERE id=? AND offer_id=? AND status='uncertain' AND updated_at=?",
                                ('Пользователь подтвердил отсутствие отправки.', timestamp, attempt_id, offer_id, updated_at))
            if result.rowcount != 1:
                raise ValueError('Статус отправки изменился. Обновите письмо.')
            db.execute('UPDATE offers SET approved=0,updated_at=? WHERE id=?', (timestamp, offer_id))
            db.execute('INSERT INTO audit_events(event,details,created_at) VALUES(?,?,?)',
                       ('mail.user_confirmed_not_sent', str(attempt_id), timestamp))

    def _match_reply(self, message, account):
        peer = normalize_email(parseaddr(str(message.get('From', '')))[1])
        if not peer or peer == account['email']:
            return None
        if account['email'] not in _recipient_addresses(message):
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
        if peer == normalize_email(original):
            return True
        contacts = {row['email'] for row in company_emails(self.store, parent['company_id'])}
        if peer in contacts:
            return True
        company = self.store.one('SELECT domain,url FROM companies WHERE id=?', (parent['company_id'],)) or {}
        domains = {ascii_domain(host).removeprefix('www.') for host in (
            original.rsplit('@', 1)[-1], company.get('domain') or '',
            urlsplit(company.get('url') or '').hostname or '')}
        shared = {'gmail.com', 'googlemail.com', 'yandex.ru', 'yandex.com', 'ya.ru', 'mail.ru', 'bk.ru',
                  'list.ru', 'inbox.ru', 'outlook.com', 'hotmail.com', 'yahoo.com', 'rambler.ru', 'icloud.com', 'proton.me', 'protonmail.com'}
        peer_domain = peer.rsplit('@', 1)[-1]
        return any(domain and not any(domain == host or domain.endswith('.' + host) for host in shared)
                   and (peer_domain == domain or peer_domain.endswith('.' + domain)) for domain in domains)

    def _automatic_opt_out(self, message, account, message_id):
        peer = normalize_email(parseaddr(str(message.get('From', '')))[1])
        if not peer or is_automatic(message):
            return False
        recipients = _recipient_addresses(message)
        direct = _unsubscribe_alias(account) in recipients
        encoded = re.fullmatch(r'unsubscribe ([0-9a-f]{24})',
                               str(message.get('Subject', '')).strip().casefold())
        if not direct and not (encoded and account['email'] in recipients):
            return False
        outbound = self.store.query("""
            SELECT m.message_id FROM mail_messages m JOIN outbound_attempts a ON a.message_id=m.message_id
            WHERE m.direction='outgoing' AND lower(m.sender)=? AND lower(m.recipient)=?
              AND a.status IN ('sent','uncertain')
        """, (account['email'], peer))
        if not outbound or (encoded and not any(_unsubscribe_token(row['message_id']) == encoded[1]
                                                for row in outbound)):
            return False
        self._check_cancelled()
        with self.store.db as db:
            logged = db.execute('INSERT OR IGNORE INTO mail_opt_out_requests(message_id) VALUES(?)', (message_id,))
            if logged.rowcount != 1:
                return False
            return self._record_opt_out(db, peer, 'Запрос на отписку по почте', 'mail.opt_out_received')

    @staticmethod
    def _record_opt_out(db, peer, reason, event):
        timestamp = now()
        inserted = db.execute('INSERT OR IGNORE INTO opt_out(email,reason,created_at) VALUES(?,?,?)',
                              (peer, reason, timestamp))
        if inserted.rowcount:
            db.execute('INSERT INTO audit_events(event,details,created_at) VALUES(?,?,?)',
                       (event, peer, timestamp))
        return bool(inserted.rowcount)

    def pending_opt_outs(self):
        return self.store.query("SELECT id,sender,subject,body FROM mail_messages "
                                "WHERE direction='incoming' AND opt_out_review=1 ORDER BY id")

    def resolve_opt_out(self, message_id, confirmed):
        with self.store.db as db:
            result = db.execute("UPDATE mail_messages SET opt_out_review=2 "
                                "WHERE direction='incoming' AND opt_out_review=1 AND id=? RETURNING sender",
                                (message_id,)).fetchone()
            if result is None:
                raise ValueError('Этот запрос на отписку уже проверен.')
            if confirmed:
                self._record_opt_out(db, result[0], 'Отказ по письму подтверждён пользователем',
                                     'mail.opt_out_confirmed')

    @staticmethod
    def _possible_opt_out(message, body):
        subject = re.sub(r'^(?:(?:re|fw|fwd|ответ):\s*)+', '', str(message.get('Subject', '')).strip(), flags=re.I)
        # Stop at usual quote boundaries so our own footer cannot flag a reply.
        reply = re.split(r'(?im)^\s*(?:>|--\s*$|on .{1,250} wrote:\s*$|.{1,250}(?:написал[аи]?|пишет):\s*$)',
                         body[:4000], maxsplit=1)[0]
        return bool(_OPT_OUT_REQUEST.search(subject) or _OPT_OUT_REQUEST.search(reply))

    def sync(self, cancel=None):
        with self._lock, self._cancellable(cancel):
            account, password = self._account(require_connected=True)
            sent_warning = ''
            try:
                self.reconcile()
            except SentReconciliationError as exc:
                sent_warning = f'{exc} Неясные отправки сохранены.'
            connection = None
            matched = unsubscribed = possible = 0
            try:
                connection = self._imap(account, password)
                status, _ = connection.select('INBOX', readonly=True)
                self._check_cancelled()
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
                self._check_cancelled()
                if state != 'OK' or not result or (result[0] is not None and not isinstance(result[0], bytes)):
                    raise RuntimeError('Не удалось получить новые письма.')
                uids = sorted({int(x) for x in (result[0] or b'').split() if x.isdigit() and int(x) > last_uid})[:100]
                for uid in uids:
                    # Fetch size first, then a bounded body. Never set Seen.
                    self._check_cancelled()
                    state, size_data = connection.uid('fetch', str(uid), '(RFC822.SIZE)')
                    self._check_cancelled()
                    size_blob = b' '.join(item for item in size_data or [] if isinstance(item, bytes))
                    sizes = re.findall(rb'RFC822.SIZE (\d+)', size_blob)
                    if state != 'OK' or not sizes:
                        raise RuntimeError('Не удалось прочитать письмо. Проверка продолжится с него при следующем обновлении.')
                    large = int(sizes[0]) > MAX_MESSAGE_BYTES
                    if large:
                        state, data = connection.uid('fetch', str(uid), '(BODY.PEEK[HEADER]<0.262144>)')
                    else:
                        state, data = connection.uid('fetch', str(uid), '(BODY.PEEK[]<0.' + str(MAX_MESSAGE_BYTES) + '>)')
                    self._check_cancelled()
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
                        if message_id and len(message_id) < 500:
                            unsubscribed += self._automatic_opt_out(message, account, message_id)
                        if source and message_id and len(message_id) < 500:
                            auto = is_automatic(message, matched_reply=True)
                            try:
                                sent_at = parsedate_to_datetime(str(message.get('Date', ''))).astimezone(timezone.utc).isoformat()
                            except (TypeError, ValueError, OverflowError):
                                sent_at = now()
                            peer = normalize_email(parseaddr(str(message.get('From', '')))[1])
                            body = 'Текст не загружен: письмо больше 2 МБ.' if large else message_text(message)
                            review = (not auto and self._possible_opt_out(message, body)
                                      and not self.store.one('SELECT email FROM opt_out WHERE lower(email)=?', (peer,)))
                            self._check_cancelled()
                            with self.store.db as db:
                                inserted = db.execute('INSERT OR IGNORE INTO mail_messages(product_id,company_id,message_id,in_reply_to,refs,direction,sender,recipient,subject,body,sent_at,is_reply,opt_out_review) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                    (source['product_id'], source['company_id'], message_id, str(message.get('In-Reply-To', '')),
                                     str(message.get('References', '')), 'incoming', peer, account['email'],
                                     str(message.get('Subject', ''))[:500], body, sent_at, int(not auto), int(review)))
                            if inserted.rowcount:
                                matched += not auto
                                possible += review
                    # Commit checkpoint only after successful processing.
                    self._check_cancelled()
                    self.store.execute('UPDATE mail_accounts SET last_uid=?,uidvalidity=?,updated_at=? WHERE id=1',
                                       (uid, validity, now()))
                if not uids:
                    self._check_cancelled()
                    self.store.execute('UPDATE mail_accounts SET last_uid=?,uidvalidity=?,updated_at=? WHERE id=1',
                                       (last_uid, validity, now()))
                result = f'Новых ответов: {matched}' if matched else 'Почта обновлена'
                if unsubscribed:
                    result += f'. Отказов от писем: {unsubscribed}'
                if possible:
                    result += f'. Запросов на отписку для проверки: {possible}'
                return f'{result}. {sent_warning}' if sent_warning else result
            except (imaplib.IMAP4.error, OSError) as exc:
                self._check_cancelled()
                raise RuntimeError('Не удалось обновить почту. Проверьте соединение.') from exc
            finally:
                self._close_imap(connection)

    def disconnect(self):
        with self._lock:
            self.store.execute('UPDATE mail_accounts SET connected=0,updated_at=? WHERE id=1', (now(),))
            self.secrets.delete('mail_password')
            return 'Почта отключена'
