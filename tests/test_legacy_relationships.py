"""Release upgrade rechecks only recognisable automatic false buyer matches."""
import json

import pytest

from sales_manager.storage import Store, now


@pytest.mark.parametrize('text,rule,manual,ai,expected', [
    ('Мы сервисный центр по ремонту обуви.', 'activity:electronics', 0, False, 'INSUFFICIENT_EVIDENCE'),
    ('Мы не занимаемся продажей смартфонов.', 'documented_assortment', 0, False, 'INSUFFICIENT_EVIDENCE'),
    ('Мы сервисный центр по ремонту обуви.', 'activity:electronics', 0, True, 'INSUFFICIENT_EVIDENCE'),
    ('Мы сервисный центр по ремонту обуви.', 'activity:electronics', 1, False, 'FIT'),
    ('Мы продаём смартфоны в нашем магазине.', 'documented_assortment', 0, False, 'FIT'),
    ('Наш сервисный центр ремонтирует смартфоны.', 'activity:electronics', 0, False, 'FIT'),
    ('shop: electronics', 'activity:electronics', 0, False, 'FIT'),
])
def test_upgrade_preserves_manual_decisions_and_history(tmp_path, text, rule, manual, ai, expected):
    path = tmp_path / 'legacy.sqlite3'
    store = Store(path)
    pid = store.ensure_product('Смартфон')['id']
    cid = store.execute('INSERT INTO companies(name,created_at) VALUES(?,?)', ('Покупатель', now()))
    sid = store.put_source('https://buyer.example/about', text=text)
    profile = {'rule': rule, 'rule_version': 'activity-fit-v2', 'activities': [
        {'code': 'electronics', 'quote': text, 'source_id': sid}
    ]}
    reason = ('Гипотеза: товар подходит. Основание: «' + text + '»') if ai else ('Факт: «' + text + '». Связь: ассортимент.')
    store.execute('INSERT INTO company_fit(product_id,company_id,status,reason,source_ids,manual,activity_profile,ai_checked_key) VALUES(?,?,?,?,?,?,?,?)',
                  (pid, cid, 'FIT', reason, json.dumps([sid]), manual, json.dumps(profile), 'old-ai' if ai else ''))
    store.execute('INSERT INTO offers(product_id,company_id,subject,body,edited,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                  (pid, cid, 'Тема', 'Правка пользователя', 1, now(), now()))
    store.execute('INSERT INTO mail_messages(product_id,company_id,message_id,direction,body) VALUES(?,?,?,?,?)',
                  (pid, cid, '<legacy@buyer.example>', 'incoming', 'Переписка'))
    store.execute('DELETE FROM settings WHERE key=?', ('fit.relationship_reviewed_v5',))
    store.close()

    for _ in range(2):
        store = Store(path)
        try:
            assert store.one('SELECT status FROM company_fit')['status'] == expected
            assert store.one('SELECT body,edited FROM offers') == {'body': 'Правка пользователя', 'edited': 1}
            assert store.one('SELECT body FROM mail_messages')['body'] == 'Переписка'
            events = store.query("SELECT details FROM audit_events WHERE event='legacy_relationship_review'")
            assert len(events) == int(expected != 'FIT')
            if events:
                assert json.loads(events[0]['details'])['previous_reason'] == reason
            assert store.one('PRAGMA integrity_check')['integrity_check'] == 'ok'
            assert store.query('PRAGMA foreign_key_check') == []
        finally:
            store.close()


def test_upgrade_retains_linked_structured_osm_evidence(tmp_path):
    path = tmp_path / 'osm.sqlite3'
    store = Store(path)
    pid = store.ensure_product('Смартфон')['id']
    cid = store.execute('INSERT INTO companies(name,created_at) VALUES(?,?)', ('Сервисный центр', now()))
    text = 'Наш сервисный центр работает в Москве.'
    site = store.put_source('https://buyer.example/', text=text)
    osm = store.put_source('https://www.openstreetmap.org/node/123', text='name: Центр; shop: electronics')
    profile = {'rule': 'activity:electronics', 'rule_version': 'activity-fit-v2',
               'activities': [{'code': 'electronics', 'quote': text, 'source_id': site}]}
    store.execute('INSERT INTO company_fit(product_id,company_id,status,reason,source_ids,activity_profile) VALUES(?,?,?,?,?,?)',
                  (pid, cid, 'FIT', 'Факт: электроника', json.dumps([site, osm]), json.dumps(profile)))
    store.execute('DELETE FROM settings WHERE key=?', ('fit.relationship_reviewed_v5',))
    store.close()
    store = Store(path)
    try:
        assert store.one('SELECT status FROM company_fit')['status'] == 'FIT'
        assert store.query("SELECT * FROM audit_events WHERE event='legacy_relationship_review'") == []
    finally:
        store.close()
