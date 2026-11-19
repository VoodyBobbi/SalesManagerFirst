"""Real SQLite integration; isolated databases, no user's DB."""
import sqlite3
import pytest

from sales_manager.storage.store import Store, now


@pytest.mark.stability
def test_product_identity_and_branch_separation(isolated_store):
    db = isolated_store
    coffee = db.ensure_product('  Кофе  ')
    same = db.ensure_product('КОФЕ')
    chairs = db.ensure_product('Офисные кресла')
    assert coffee['id'] == same['id']
    assert coffee['id'] != chairs['id']
    company_id = db.execute('INSERT INTO companies(name,created_at) VALUES(?,?)', ('Вымышленный покупатель', now()))
    for product_id, body in ((coffee['id'], 'Черновик А'), (chairs['id'], 'Черновик Б')):
        db.execute('INSERT INTO offers(product_id,company_id,subject,body,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                   (product_id, company_id, 'Тема', body, now(), now()))
    db.execute('UPDATE offers SET body=? WHERE product_id=?', ('Изменение A', coffee['id']))
    rows = db.query('SELECT product_id,body FROM offers ORDER BY product_id')
    assert {r['product_id']: r['body'] for r in rows} == {coffee['id']: 'Изменение A', chairs['id']: 'Черновик Б'}
    assert db.one('PRAGMA integrity_check')['integrity_check'] == 'ok'
    assert db.query('PRAGMA foreign_key_check') == []


@pytest.mark.stability
def test_restart_is_durable_and_crashed_send_not_retried(tmp_path):
    path = tmp_path / 'crash.db'
    db = Store(path)
    product_id = db.ensure_product('Товар')['id']
    company_id = db.execute('INSERT INTO companies(name,created_at) VALUES(?,?)', ('Компания', now()))
    offer_id = db.execute('INSERT INTO offers(product_id,company_id,subject,body,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                          (product_id, company_id, 'Тема', 'Текст', now(), now()))
    db.execute('INSERT INTO outbound_attempts(offer_id,idempotency_key,message_id,recipient,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
               (offer_id, 'qa-key', '<qa@test.local>', 'recipient@company.ru', 'sending', now(), now()))
    run_id = db.execute('INSERT INTO job_runs(kind,status,created_at,updated_at) VALUES(?,?,?,?)',
                        ('companies', 'running', now(), now()))
    db.execute('INSERT INTO job_queue(run_id,task_key,payload,status) VALUES(?,?,?,?)',
               (run_id, 'qa-task', '{}', 'running'))
    db.set_setting('qa.keep', {'x': 123})
    db.close()
    db = Store(path)
    try:
        assert db.get_setting('qa.keep') == {'x': 123}
        assert db.one('SELECT status FROM outbound_attempts')['status'] == 'uncertain'
        assert db.one('SELECT status FROM job_runs')['status'] == 'interrupted'
        assert db.one('SELECT status FROM job_queue')['status'] == 'pending'
        assert db.one('PRAGMA integrity_check')['integrity_check'] == 'ok'
    finally:
        db.close()


def test_source_hash_cache_does_not_confuse_versions(isolated_store):
    store = isolated_store
    a = store.put_source('https://example.org/news', text='source version 1')
    b = store.put_source('https://example.org/news', text='source version 1')
    c = store.put_source('https://example.org/news', text='source version 2')
    assert a == b
    assert a != c
    assert store.one('SELECT COUNT(*) AS n FROM sources WHERE url=?', ('https://example.org/news',))['n'] == 2


def test_sqlite_foreign_keys_reject_orphan_offer(isolated_store):
    with pytest.raises(sqlite3.IntegrityError):
        isolated_store.execute('INSERT INTO offers(product_id,company_id,subject,body,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                               (87654321, 87654321, 'T', 'B', now(), now()))


def test_invalid_product_does_not_create_db_row(isolated_store):
    before = isolated_store.one('SELECT COUNT(*) AS n FROM products')['n']
    for name in ('', '    ', 'X' * 161):
        with pytest.raises(ValueError):
            isolated_store.ensure_product(name)
    assert isolated_store.one('SELECT COUNT(*) AS n FROM products')['n'] == before
