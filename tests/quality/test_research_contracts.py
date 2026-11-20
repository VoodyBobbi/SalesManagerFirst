"""Evidence and public-web validation: deterministic, offline and adversarial."""
import socket
from unittest.mock import patch

import pytest

from sales_manager.core.contacts import is_contact_email, email_purpose, company_emails
from sales_manager.research.fit_rules import evaluate_fit
from sales_manager.research.safe_http import FetchError, validate_url, public_addresses, RobotsRules, SafeFetcher
from sales_manager.research.service import current_demand_excerpt
from sales_manager.storage.store import now


@pytest.mark.parametrize('value', [
    'name@domain.com', 'test123@organisation.ru', 'info@example.com',
    'owner@localhost', 'x..x@company.ru',
    '.sales@company.ru', 'office@company..ru', 'a@-company.ru',
])
def test_contact_rejects_placeholders_and_invalid_emails(value):
    assert not is_contact_email(value)


@pytest.mark.parametrize('value', [
    'zakupki@organisation.ru', 'sales@organisation.ru', 'info@organisation.ru',
    'firstname.lastname@organisation.ru', 'user@company.ru',
])
def test_contact_accepts_valid_business_emails(value):
    assert is_contact_email(value)


@pytest.mark.stability
def test_email_priority_is_stable_and_no_placeholder_recipients(isolated_store):
    company_id = isolated_store.execute('INSERT INTO companies(name,created_at) VALUES(?,?)', ('Клиент', now()))
    for email in ('info@buyer.ru', 'name@domain.com', 'sales@buyer.ru', 'zakupki@buyer.ru', 'zakupki@buyer.ru'):
        isolated_store.execute('INSERT OR IGNORE INTO company_contacts(company_id,kind,value) VALUES(?,?,?)',
                               (company_id, 'email', email))
    recipients = [row['email'] for row in company_emails(isolated_store, company_id)]
    assert recipients == ['zakupki@buyer.ru', 'sales@buyer.ru', 'info@buyer.ru']


@pytest.mark.parametrize('url', [
    'file:///etc/passwd', 'http://localhost', 'http://127.0.0.1:1234',
    'http://internal.local', 'https://foo.internal/path',
    'http://user:password@company.ru', 'https://company.ru:444/',
    'https://company.ru/hello\nHost:attack',
])
def test_reject_nonpublic_or_unsafe_urls(url):
    with pytest.raises(FetchError):
        validate_url(url)


def test_dns_rebinding_private_address_is_blocked(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))])
    with pytest.raises(FetchError):
        public_addresses('company.ru', 443)


def test_mixed_dns_with_one_private_address_is_blocked(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *_args, **_kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('1.1.1.1', 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.168.1.20', 443)),
    ])
    with pytest.raises(FetchError):
        public_addresses('company.ru', 443)


def test_redirect_to_internal_target_denied_before_network(monkeypatch):
    """Second-hop URL may be attempted but must be checked before any socket call."""
    fetcher = SafeFetcher()
    from sales_manager.research import safe_http
    original_request = fetcher._request
    calls = []

    def fake_dns(host, port, *args, **kwargs):
        assert host == '127.0.0.1'
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', port))]

    def fake_request(url, *args, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            return 302, {'location': 'http://127.0.0.1/private'}, b''
        return original_request(url, *args, **kwargs)

    monkeypatch.setattr(socket, 'getaddrinfo', fake_dns)
    monkeypatch.setattr(fetcher, '_request', fake_request)
    with pytest.raises(FetchError):
        fetcher._raw('https://company.ru/public', 100)
    assert calls == ['https://company.ru/public', 'http://127.0.0.1/private']


def test_robots_specific_allow_overrides_broad_denial():
    rules = RobotsRules('User-agent: *\nDisallow: /\nAllow: /public/\n'.splitlines())
    assert not rules.can_fetch('SalesManagerFirst', 'https://company.ru/admin')
    assert rules.can_fetch('SalesManagerFirst', 'https://company.ru/public/faq')


@pytest.mark.stability
def test_negative_or_speculative_market_news_not_promoted_to_demand():
    for source in (
        'Спрос на офисные кресла снизился в октябре.',
        'В следующем году ожидается рост спроса на офисные кресла.',
        'Продажи офисных кресел упали по сравнению с прошлым годом.',
    ):
        assert current_demand_excerpt('офисные кресла', source) == ''


def test_fit_does_not_promote_customer_example_to_company_activity():
    pages = [{'text': 'Мы разрабатываем сайты для гостиниц.', 'url': 'https://example.org', 'source_id': 1}]
    assert evaluate_fit('Кофе', pages, {}, '')['status'] != 'FIT'


def test_fit_uses_own_activity_when_present():
    pages = [{'text': 'Наша гостиница принимает гостей.', 'url': 'https://buyer.ru', 'source_id': 1}]
    assert evaluate_fit('Кофе', pages, {}, '')['status'] == 'FIT'
