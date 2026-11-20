from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import time
import pytest
from sales_manager.storage import Store, now
from sales_manager.storage.migrations import MIGRATIONS


def test_restart_preserves_products_and_recovers_without_resending(tmp_path):
    path = tmp_path / 'db.sqlite3'
    s = Store(path)
    product = s.ensure_product('Товар')
    s.set_setting('sender.name', 'Компания')
    company = s.execute('INSERT INTO companies(name,created_at) VALUES(?,?)', ('Покупатель', now()))
    offer = s.execute('INSERT INTO offers(product_id,company_id,subject,body,created_at,updated_at) VALUES(?,?,?,?,?,?)', (product['id'], company, 'Тема', 'Письмо', now(), now()))
    s.execute('INSERT INTO outbound_attempts(offer_id,idempotency_key,message_id,recipient,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)', (offer, 'unique', '<1@test>', 'a@test.ru', 'sending', now(), now()))
    run = s.execute('INSERT INTO job_runs(kind,status,created_at,updated_at) VALUES(?,?,?,?)', ('companies', 'running', now(), now()))
    s.execute('INSERT INTO job_queue(run_id,task_key,payload,status) VALUES(?,?,?,?)', (run, 'task', '{}', 'running'))
    s.close()
    resumed = Store(path)
    assert resumed.ensure_product('ТОВАР')['id'] == product['id']
    assert resumed.get_setting('sender.name') == 'Компания'
    assert resumed.one('SELECT status FROM outbound_attempts')['status'] == 'uncertain'
    assert resumed.one('SELECT status FROM job_runs')['status'] == 'interrupted'
    assert resumed.one('SELECT status FROM job_queue')['status'] == 'pending'


def test_constraints_and_evidence_versioning(tmp_path):
    s = Store(tmp_path / 'db.sqlite3')
    a = s.put_source('https://example.org', text='first')
    assert a == s.put_source('https://example.org', text='first')
    assert a != s.put_source('https://example.org', text='changed')
    with pytest.raises(sqlite3.IntegrityError):
        s.execute('INSERT INTO company_contacts(company_id,kind,value) VALUES(?,?,?)', (999, 'email', 'x@test.ru'))
    assert s.one('PRAGMA user_version')['user_version'] == max(MIGRATIONS)


def test_parallel_workers_keep_thread_local_connections(tmp_path):
    s = Store(tmp_path / 'db.sqlite3')
    def worker(i):
        s.ensure_product(f'Товар {i}')
        s.close()
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(worker, range(30)))
    assert len(s.query('SELECT * FROM products')) == 30


def test_schema_three_upgrade_releases_old_reserve_and_preserves_usage_and_drafts(tmp_path):
    from sales_manager.storage.migrations import SCHEMA, MARKET_AND_FIT, MARKET_SOURCES
    path = tmp_path / 'old.sqlite3'
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA + MARKET_AND_FIT + MARKET_SOURCES + '\nPRAGMA user_version=3;')
    connection.execute("INSERT INTO settings(key,value) VALUES('ai.unconfirmed_tokens','99999')")
    connection.execute("INSERT INTO usage_log(provider,operation,input_tokens,output_tokens,created_at) VALUES('gigachat','offer',20,3,?)", (now(),))
    connection.execute("INSERT INTO products(id,name,normalized,created_at) VALUES(1,'Товар','товар',?)", (now(),))
    connection.execute("INSERT INTO companies(id,name,created_at) VALUES(1,'Компания',?)", (now(),))
    connection.execute("INSERT INTO mail_messages(product_id,company_id,message_id,direction,sender,recipient,subject,body,is_reply) VALUES(1,1,'<reply@firm.ru>','incoming','info@firm.ru','owner@test.ru','Ответ','Сохранённая переписка',1)")
    connection.execute("INSERT INTO offers(product_id,company_id,subject,body,created_at,updated_at) VALUES(1,1,'Тема','Сохранённый текст',?,?)", (now(),now()))
    # Historical rows may legitimately retain more than one completed attempt.
    # Migration 4 must not reject that existing history while protecting live sends.
    connection.execute("INSERT INTO outbound_attempts(offer_id,idempotency_key,message_id,recipient,status,created_at,updated_at) VALUES(1,'old-1','<old-1@test.ru>','info@firm.ru','sent',?,?)", (now(),now()))
    connection.execute("INSERT INTO outbound_attempts(offer_id,idempotency_key,message_id,recipient,status,created_at,updated_at) VALUES(1,'old-2','<old-2@test.ru>','sales@firm.ru','sent',?,?)", (now(),now()))
    connection.commit(); connection.close()
    store = Store(path)
    assert store.get_setting('ai.unconfirmed_tokens') == 0
    assert store.one('SELECT SUM(input_tokens+output_tokens) n FROM usage_log')['n'] == 23
    assert store.one('SELECT body,recipient FROM offers') == {'body':'Сохранённый текст','recipient':''}
    assert store.one("SELECT COUNT(*) n FROM outbound_attempts WHERE status='sent'")['n'] == 2
    assert store.one("SELECT body,is_reply FROM mail_messages WHERE message_id='<reply@firm.ru>'") == {'body': 'Сохранённая переписка', 'is_reply': 1}
    assert store.one('PRAGMA auto_vacuum')['auto_vacuum'] == 0
    store.retention()
    assert store.one('PRAGMA auto_vacuum')['auto_vacuum'] == 2
    assert store.token_usage() == 23
    assert store.one('PRAGMA integrity_check')['integrity_check'] == 'ok'
    store.close()


def test_branches_share_company_but_not_offer_history(tmp_path):
    s = Store(tmp_path / 'db.sqlite3')
    a, b = s.ensure_product('Бумага'), s.ensure_product('Принтер')
    company = s.execute('INSERT INTO companies(name,created_at) VALUES(?,?)', ('Компания', now()))
    for product in (a, b):
        s.execute('INSERT INTO company_fit(product_id,company_id,status) VALUES(?,?,?)', (product['id'], company, 'FIT'))
    s.execute('INSERT INTO mail_messages(product_id,company_id,message_id,direction,body) VALUES(?,?,?,?,?)', (a['id'], company, '<first@test>', 'outbound', 'Бумага'))
    assert not s.query('SELECT * FROM mail_messages WHERE product_id=?', (b['id'],))


def test_retention_reclaims_space_preserves_evidence_and_archives_usage(tmp_path):
    path = tmp_path / 'retention.sqlite3'
    store = Store(path)
    old, recent = '2020-01-15T12:00:00+00:00', now()
    product = store.ensure_product('Товар')['id']
    company = store.execute('INSERT INTO companies(name,created_at) VALUES(?,?)', ('Компания', recent))
    offer_id, fit_id, market_id, cache_id, queued_id, old_id, *latest = [
        store.put_source('https://example.org', text=str(index) + 'x' * 100000)
        for index in range(8)
    ]
    store.execute('INSERT INTO offers(product_id,company_id,subject,body,source_ids,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                  (product, company, 'Тема', 'Письмо', json.dumps([offer_id]), recent, recent))
    store.execute('INSERT INTO outbound_attempts(offer_id,idempotency_key,message_id,recipient,status,created_at,updated_at) '
                  "SELECT id,'send-key','<message@example.org>','a@example.org','sent',?,? FROM offers",
                  (old, old))
    store.execute('INSERT INTO company_fit(product_id,company_id,status,source_ids) VALUES(?,?,?,?)',
                  (product, company, 'FIT', json.dumps([fit_id])))
    store.execute('INSERT INTO market_observations(product_id,source_id,metric,excerpt,event_key,created_at) VALUES(?,?,?,?,?,?)',
                  (product, market_id, 'demand', 'excerpt', 'evidence', old))
    store.execute('INSERT INTO cache(key,value,expires_at) VALUES(?,?,?),(?,?,?)',
                  ('page:v9:live', json.dumps({'source_id': cache_id}), time.time() + 3600,
                   'provider:v3:expired', 'x' * 200000, time.time() - 60))
    run_id = store.execute("INSERT INTO job_runs(kind,status,created_at,updated_at) VALUES('companies','interrupted',?,?)",
                           (recent, recent))
    store.execute('INSERT INTO job_queue(run_id,task_key,payload) VALUES(?,?,?)',
                  (run_id, 'company', json.dumps({'result': {'metadata': {'discovery_source_id': queued_id}}})))
    store.execute("INSERT INTO audit_events(event,created_at) VALUES('research.failed',?),"
                  "('mail.user_confirmed_not_sent',?),('research.failed',?)", (old, old, recent))
    with store.db as db:
        db.executemany('INSERT INTO usage_log(provider,operation,input_tokens,output_tokens,credits,created_at) VALUES(?,?,?,?,?,?)',
                       [('gigachat', 'offer', 20, 3, 0, old), ('gigachat', 'offer', None, None, 0, old),
                        ('gigachat', 'offer', 6, 1, 0, recent), ('tavily', 'research_request', 0, 0, 0.25, old),
                        ('tavily', 'research_request', 0, 0, 0.5, recent)])
    store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
    size_before = path.stat().st_size
    usage_before, calls_before = store.token_usage(), store.search_usage()

    store.retention()
    store.retention()

    assert store.token_usage() == usage_before == 30
    assert store.search_usage() == calls_before == [{'provider': 'tavily', 'calls': 2}]
    assert store.one("SELECT credits,calls FROM usage_totals WHERE provider='tavily'") == {'credits': 0.25, 'calls': 1}
    assert store.one("SELECT month,tokens,calls FROM usage_totals WHERE provider='gigachat'") == {
        'month': '2020-01', 'tokens': 23, 'calls': 2}
    assert store.one('SELECT COUNT(*) AS count FROM usage_log')['count'] == 2
    assert {row['id'] for row in store.query('SELECT id FROM sources')} == {
        offer_id, fit_id, market_id, cache_id, queued_id, *latest}
    assert store.one('SELECT id FROM sources WHERE id=?', (old_id,)) is None
    assert store.query('SELECT key FROM cache') == [{'key': 'page:v9:live'}]
    assert store.query('SELECT event FROM audit_events') == [
        {'event': 'mail.user_confirmed_not_sent'}, {'event': 'research.failed'}]
    assert store.one('SELECT status FROM outbound_attempts')['status'] == 'sent'
    assert path.stat().st_size < size_before
    store.close()


def test_startup_retention_is_throttled_and_damaged_source_ids_prevent_pruning(tmp_path):
    store = Store(tmp_path / 'retention.sqlite3')
    for index in range(3):
        store.put_source('https://example.org', text=str(index))
    product = store.ensure_product('Товар')['id']
    company = store.execute('INSERT INTO companies(name,created_at) VALUES(?,?)', ('Компания', now()))
    store.execute("INSERT INTO company_fit(product_id,company_id,status,source_ids) VALUES(?,?,'FIT','[1,')",
                  (product, company))
    store.retention(startup=True)
    assert store.one('SELECT COUNT(*) AS count FROM sources')['count'] == 3
    store.execute("UPDATE company_fit SET source_ids='[]'")
    store.retention(startup=True)
    assert store.one('SELECT COUNT(*) AS count FROM sources')['count'] == 3
    store.retention()
    assert store.one('SELECT COUNT(*) AS count FROM sources')['count'] == 2
    store.close()
