"""Deterministic contact validation and suggested recipient purpose."""
import re

import idna

LOCAL_PART = r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+"


def ascii_domain(value):
    try:
        return idna.encode(value, uts46=True).decode('ascii').lower()
    except (idna.IDNAError, UnicodeError):
        return ''


def normalize_email(value):
    local, _, domain = str(value).strip().rpartition('@')
    local = local.lower()
    if not re.fullmatch(LOCAL_PART, local, re.ASCII):
        return ''
    domain = ascii_domain(domain)
    tld = domain.rpartition('.')[2]
    if '.' not in domain or not (tld.startswith('xn--') or re.fullmatch(r'[a-z]{2,63}', tld)):
        return ''
    value = f'{local}@{domain}'
    if len(value) > 254 or len(local) > 64 or local.startswith('.') or local.endswith('.') or '..' in local:
        return ''
    return value


def is_contact_email(value):
    value = normalize_email(value)
    if not value:
        return False
    local, domain = value.rsplit('@', 1)
    # Frequently copied example addresses that pass ordinary syntax validation.
    # Use exact addresses: a legitimate corporate domain must not be rejected
    # solely because its mailbox is called "name" or "email".
    if value in {'name@domain.com', 'email@domain.com', 'your@email.com'}:
        return False
    if any(domain == root or domain.endswith('.' + root) for root in ('example.com', 'example.org', 'example.net')) or domain.endswith(('.example', '.invalid', '.test', '.localhost')):
        return False
    return not re.fullmatch(r'(?:test|example|sample|dummy|placeholder|your[-_.]?email)(?:[._+-]?\d*)?(?:[._+-].*)?', local)


def is_service_mailbox(value):
    local = str(value).partition('@')[0].casefold()
    return bool(re.fullmatch(r'(?:no[._-]?reply|do[._-]?not[._-]?reply|postmaster|mailer[._-]?daemon)(?:\+.+)?', local))


def email_purpose(value):
    local = value.split('@', 1)[0].casefold()
    rules = (
        (0, 'Закупки', r'(?:otdel[._-]?)?zakup(?:ki|ka|ok)?|purchas(?:e|es|ing)|procure(?:ment)?|buying|snab(?:zhenie)?|supply|tenders?'),
        (1, 'Продажи', r'sales?|commercial|kommer|wholesale|opt(?:om)?|b2b|partner'),
        (2, 'Общий', r'info|contact|office|hello|mail|reception|general'),
        (4, 'Поддержка', r'support|help|service'),
        (5, 'Бухгалтерия', r'accounts?|accounting|billing|finance|buh(?:galter(?:ia|iya)?)?|schet'),
        (5, 'Кадры', r'hr|jobs?|career|recruit|kadry|personal'),
        (6, 'Уведомления', r'no.?reply|notification|mailer.daemon'),
    )
    for rank, label, pattern in rules:
        if re.search(r'(?:^|[._+-])(' + pattern + r')(?:\d*|[._+-].*)$', local):
            return rank, label
    return 3, 'Контакт'


def company_emails(store, company_id):
    rows = store.query("SELECT value FROM company_contacts WHERE company_id=? AND kind='email' ORDER BY id", (company_id,))
    values = {normalize_email(row['value']) for row in rows if is_contact_email(row['value'])}
    return [{'email': value, 'purpose': email_purpose(value)[1]} for value in sorted(values, key=lambda v: (email_purpose(v)[0], v))]


def recipient_emails(store, company_id):
    return [row for row in company_emails(store, company_id) if not is_service_mailbox(row['email'])]
