"""Deterministic contact validation and suggested recipient purpose."""
import re


def is_contact_email(value):
    value = str(value).strip().casefold()
    if not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,24}", value):
        return False
    local, domain = value.rsplit('@', 1)
    if len(value) > 254 or len(local) > 64 or local.startswith('.') or local.endswith('.') or '..' in local:
        return False
    if any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part) for part in domain.split('.')):
        return False
    # Frequently copied example addresses that pass ordinary syntax validation.
    # Use exact addresses: a legitimate corporate domain must not be rejected
    # solely because its mailbox is called "name" or "email".
    if value in {'name@domain.com', 'email@domain.com', 'your@email.com'}:
        return False
    if any(domain == root or domain.endswith('.' + root) for root in ('example.com', 'example.org', 'example.net')) or domain.endswith(('.example', '.invalid', '.test', '.localhost')):
        return False
    return not re.fullmatch(r'(?:test|example|sample|dummy|placeholder|your[-_.]?email|user(?:name)?)(?:[._+-]?\d*)?(?:[._+-].*)?', local)


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
    values = {row['value'].strip().casefold() for row in rows if is_contact_email(row['value'])}
    return [{'email': value, 'purpose': email_purpose(value)[1]} for value in sorted(values, key=lambda v: (email_purpose(v)[0], v))]
