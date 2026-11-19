from concurrent.futures import ThreadPoolExecutor
import sqlite3
import pytest
from sales_manager.storage import Store, now


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
    assert s.one('PRAGMA user_version')['user_version'] == 6


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
    store.close()


def test_branches_share_company_but_not_offer_history(tmp_path):
    s = Store(tmp_path / 'db.sqlite3')
    a, b = s.ensure_product('Бумага'), s.ensure_product('Принтер')
    company = s.execute('INSERT INTO companies(name,created_at) VALUES(?,?)', ('Компания', now()))
    for product in (a, b):
        s.execute('INSERT INTO company_fit(product_id,company_id,status) VALUES(?,?,?)', (product['id'], company, 'FIT'))
    s.execute('INSERT INTO mail_messages(product_id,company_id,message_id,direction,body) VALUES(?,?,?,?,?)', (a['id'], company, '<first@test>', 'outbound', 'Бумага'))
    assert not s.query('SELECT * FROM mail_messages WHERE product_id=?', (b['id'],))
