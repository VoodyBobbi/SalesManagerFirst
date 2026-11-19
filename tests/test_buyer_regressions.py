import json
import threading
from unittest.mock import Mock

import pytest

from sales_manager.research.fit_rules import evaluate_fit
from sales_manager.research.safe_http import RobotsRules, SafeFetcher
from sales_manager.research.service import ResearchService
from sales_manager.storage import Store, now


def test_activity_negation_and_later_own_activity():
    page = {'text': 'Мы не гостиница. Мы агентство.', 'source_id': 1}
    assert evaluate_fit('Кофе', [page], {}, 'Россия')['status'] == 'INSUFFICIENT_EVIDENCE'
    page['text'] = 'В здании нет ресторана.'
    assert evaluate_fit('Кофе', [page], {}, 'Россия')['status'] == 'INSUFFICIENT_EVIDENCE'
    page['text'] = 'Мы разрабатываем сайты для гостиниц. Наша гостиница принимает гостей.'
    assert evaluate_fit('Кофе', [page], {}, 'Россия')['status'] == 'FIT'


def test_negated_assortment_is_not_a_fit():
    page = {'text': 'Мы не продаем смартфоны в нашем магазине.', 'source_id': 1}
    assert evaluate_fit('смартфон', [page], {}, '')['status'] == 'INSUFFICIENT_EVIDENCE'


@pytest.mark.parametrize('url,allowed', [
    ('https://example.org/private/a', False),
    ('https://example.org/private/public/a', True),
    ('https://example.org/shop/a/cart', False),
    ('https://example.org/shop/a/cart/extra', True),
    ('https://example.org/equal', True),
    ('https://example.org/other', True),
    ('https://example.org/~me', False),
])
def test_robots_specificity_wildcards_groups_and_percent(url, allowed):
    robot = RobotsRules('''User-agent: *
Disallow: /
User-agent: SalesManagerFirst
Disallow: /private
Allow: /private/public
Disallow: /shop/*/cart$
Disallow: /equal
Allow: /equal
Disallow: /%7Eme
User-agent: SalesManagerFirst
Disallow: /second
'''.splitlines())
    assert robot.can_fetch('SalesManagerFirst', url) is allowed
    assert not robot.can_fetch('SalesManagerFirst', 'https://example.org/second')


def test_robots_file_is_always_allowed():
    robot = RobotsRules(['User-agent: *', 'Disallow: /'])
    assert robot.can_fetch('SalesManagerFirst', 'https://example.org/robots.txt')


def test_large_robots_reads_first_500_kib(monkeypatch):
    import io
    import sales_manager.research.safe_http as module
    body = b'# filler\n' * 56000 + b'Disallow: /secret\n' + b'# tail\n' * 20000
    class Response:
        status = 200
        def __init__(self):
            self.stream = io.BytesIO(body)
        def getheaders(self):
            return [('Content-Length', str(len(body))), ('Content-Type', 'text/plain')]
        def read(self, size):
            return self.stream.read(size)
    class Connection:
        sock = None
        def __init__(self, *args): pass
        def request(self, *args, **kwargs): pass
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr(module, 'public_addresses', lambda *args: ['1.1.1.1'])
    monkeypatch.setattr(module, '_PinnedHTTPS', Connection)
    fetcher = SafeFetcher()
    result = fetcher._raw('https://example.org/robots.txt', 500 * 1024, truncate=True)
    assert len(result.content) == 500 * 1024
    robot = RobotsRules(['User-agent: *'] + result.text.splitlines())
    assert not robot.can_fetch('SalesManagerFirst', 'https://example.org/secret')


def test_old_automatic_fit_is_reviewed_without_losing_history(tmp_path):
    db = tmp_path / 'legacy.sqlite3'
    store = Store(db)
    product = store.ensure_product('картонная коробка')
    company = store.execute('INSERT INTO companies(name,created_at) VALUES(?,?)', ('Клиент', now()))
    manual = store.execute('INSERT INTO companies(name,created_at) VALUES(?,?)', ('Вручную', now()))
    text = 'Мы производим товары для дома и поставляем продукцию магазинам.'
    source = store.put_source('https://buyer.example/', 'О компании', text)
    old_reason = 'Гипотеза: возможна упаковка. Основание: «' + text + '»'
    for company_id, is_manual in [(company, 0), (manual, 1)]:
        store.execute('INSERT INTO company_fit(product_id,company_id,status,reason,source_ids,manual,ai_checked_key) VALUES(?,?,?,?,?,?,?)',
                      (product['id'], company_id, 'FIT', old_reason, json.dumps([source]), is_manual, 'old-hash'))
    store.execute("INSERT INTO mail_messages(product_id,company_id,message_id,direction,body) VALUES(?,?,?,'in',?)", (product['id'], company, '<old@example>', 'Переписка'))
    store.execute('DELETE FROM settings WHERE key=?', ('fit.legacy_ai_reviewed_v4',))
    store.close()
    store = Store(db)
    try:
        automatic = store.one('SELECT * FROM company_fit WHERE company_id=?', (company,))
        assert automatic['status'] == 'INSUFFICIENT_EVIDENCE'
        assert store.one('SELECT status FROM company_fit WHERE company_id=?', (manual,))['status'] == 'FIT'
        assert store.one('SELECT body FROM mail_messages')['body'] == 'Переписка'
        audit = store.one("SELECT details FROM audit_events WHERE event='legacy_ai_fit_review'")
        assert json.loads(audit['details'])['previous_reason'] == old_reason
    finally:
        store.close()
    store = Store(db)
    try:
        assert store.one("SELECT COUNT(*) AS n FROM audit_events WHERE event='legacy_ai_fit_review'")['n'] == 1
    finally:
        store.close()


def test_manual_resume_uses_persisted_run_after_reopen(tmp_path):
    db = tmp_path / 'manual.sqlite3'
    store = Store(db)
    secrets = Mock(get=Mock(return_value=''))
    product = store.ensure_product('кофе')
    service = ResearchService(store, secrets)
    cancelled = threading.Event()
    cancelled.set()
    first = service.find_companies(product['id'], ['Россия'], cancel=cancelled, manual_url='https://buyer.example/')
    assert first['status'] == 'interrupted'
    store.close()
    store = Store(db)
    try:
        service = ResearchService(store, secrets)
        service._company = Mock()
        result = service.resume_companies(first['run_id'], product['id'], ['Россия'])
        assert result['run_id'] == first['run_id']
        assert result['status'] == 'complete'
        assert service._company.call_count == 1
        assert store.one('SELECT COUNT(*) AS n FROM job_runs')['n'] == 1
        assert json.loads(store.one('SELECT payload FROM job_queue')['payload'])['manual'] is True
    finally:
        store.close()
