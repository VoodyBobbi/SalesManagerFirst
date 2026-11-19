"""A late model response must not replace a draft changed by another UI action."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from sales_manager.ai import AIService
from sales_manager.storage import Store, now


@pytest.fixture
def offer_case(tmp_path):
    store = Store(tmp_path / 'offers.sqlite3')
    store.db.execute('PRAGMA busy_timeout=1000')
    product_id = store.ensure_product('Смартфон')['id']
    company_id = store.execute('INSERT INTO companies(name,created_at) VALUES(?,?)',
                               ('Покупатель', now()))
    source_id = store.put_source('https://buyer.ru/about', 'Деятельность',
                                 'Компания продаёт электронику организациям.')
    store.execute('INSERT INTO company_fit(product_id,company_id,status,reason,source_ids) '
                  'VALUES(?,?,?,?,?)', (product_id, company_id, 'FIT',
                  'Гипотеза: расширение ассортимента электроники.', json.dumps([source_id])))
    offer_id = store.execute('INSERT INTO offers(product_id,company_id,subject,body,created_at,updated_at) '
                             'VALUES(?,?,?,?,?,?)', (product_id, company_id, 'Исходная тема',
                             'Исходный черновик', now(), now()))
    data = {'subject': 'Обсудим сотрудничество',
            'body': 'Предлагаем обсудить смартфоны для вашего ассортимента.',
            'reason': 'Гипотеза: расширение ассортимента электроники.',
            'source_ids': [source_id]}
    response = {'choices': [{'message': {'content': json.dumps(data)}, 'finish_reason': 'stop'}]}
    ai = AIService(store, object())
    yield ai, product_id, company_id, offer_id, response
    store.close()


def generate_during_change(case, change, force=True):
    ai, product_id, company_id, _, response = case
    entered, release = threading.Event(), threading.Event()

    def complete(*args):
        entered.set()
        assert release.wait(5), 'The test did not release the simulated response'
        return response

    ai._completion = complete

    def generate():
        try:
            return ai.generate_offer(product_id, company_id, force=force)
        finally:
            ai.store.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(generate)
        try:
            assert entered.wait(5), 'Generation did not reach the simulated API'
            change()
        finally:
            release.set()
        return pending.result(timeout=5)


@pytest.mark.parametrize('force', [False, True])
@pytest.mark.parametrize(('assignment', 'column', 'value'), [
    ("body='Правка пользователя',edited=1", 'body', 'Правка пользователя'),
    ('approved=1', 'approved', 1),
    ("recipient='new@buyer.ru'", 'recipient', 'new@buyer.ru'),
])
def test_late_generation_preserves_user_changes(offer_case, force, assignment, column, value):
    ai, _, _, offer_id, _ = offer_case
    with pytest.raises(ValueError, match='изменил|изменён|изменен'):
        generate_during_change(offer_case,
            lambda: ai.store.execute(f'UPDATE offers SET {assignment} WHERE id=?', (offer_id,)), force)
    assert ai.store.one('SELECT * FROM offers WHERE id=?', (offer_id,))[column] == value


@pytest.mark.parametrize('status', ['sending', 'sent', 'uncertain', 'failed'])
def test_attempt_started_during_generation_freezes_original_draft(offer_case, status):
    ai, _, _, offer_id, _ = offer_case
    before = ai.store.one('SELECT * FROM offers WHERE id=?', (offer_id,))

    def attempt():
        ai.store.execute('INSERT INTO outbound_attempts(offer_id,idempotency_key,message_id,recipient,'
                         'status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                         (offer_id, 'concurrent', '<out@buyer.ru>', 'sales@buyer.ru', status, now(), now()))

    with pytest.raises(ValueError, match='отправк'):
        generate_during_change(offer_case, attempt)
    assert ai.store.one('SELECT * FROM offers WHERE id=?', (offer_id,)) == before
    assert ai.store.one('SELECT status FROM outbound_attempts')['status'] == status


def test_deleted_draft_is_not_resurrected_by_late_response(offer_case):
    ai, _, _, offer_id, _ = offer_case
    with pytest.raises(ValueError, match='изменил|изменён|изменен'):
        generate_during_change(offer_case,
            lambda: ai.store.execute('DELETE FROM offers WHERE id=?', (offer_id,)))
    assert ai.store.one('SELECT id FROM offers') is None


def test_late_response_preserves_draft_created_by_another_generator(offer_case):
    ai, pid, cid, offer_id, _ = offer_case
    ai.store.execute('DELETE FROM offers WHERE id=?', (offer_id,))

    def create():
        ai.store.execute('INSERT INTO offers(product_id,company_id,subject,body,edited,created_at,updated_at) '
                         'VALUES(?,?,?,?,?,?,?)', (pid, cid, 'Новый', 'Создан другим действием', 1, now(), now()))

    with pytest.raises(ValueError, match='изменил|изменён|изменен'):
        generate_during_change(offer_case, create)
    assert ai.store.one('SELECT body,edited FROM offers') == {'body': 'Создан другим действием', 'edited': 1}


def test_late_response_is_discarded_after_connection_changes(offer_case):
    ai, _, _, offer_id, _ = offer_case
    before = ai.store.one('SELECT * FROM offers WHERE id=?', (offer_id,))
    with pytest.raises(ValueError, match='(?i)подключени'):
        generate_during_change(offer_case,
            lambda: ai.store.set_setting('ai.connection_generation', 'replacement-account'))
    assert ai.store.one('SELECT * FROM offers WHERE id=?', (offer_id,)) == before


def test_explicit_regeneration_can_replace_the_requested_edited_snapshot(offer_case):
    ai, pid, cid, offer_id, response = offer_case
    ai.store.execute('UPDATE offers SET edited=1 WHERE id=?', (offer_id,))
    ai._completion = lambda *args: response
    result = ai.generate_offer(pid, cid, force=True)
    assert result['body'] == 'Предлагаем обсудить смартфоны для вашего ассортимента.'
    assert result['edited'] == 0


def test_regeneration_after_definitive_refusal_preserves_attempt_and_mail_snapshot(offer_case):
    ai, pid, cid, offer_id, response = offer_case
    timestamp = now()
    ai.store.execute('INSERT INTO outbound_attempts(offer_id,idempotency_key,message_id,recipient,'
                     'status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                     (offer_id, 'refused', '<old@buyer.ru>', 'sales@buyer.ru', 'failed', timestamp, timestamp))
    ai.store.execute('INSERT INTO mail_messages(product_id,company_id,message_id,direction,subject,body) '
                     'VALUES(?,?,?,?,?,?)', (pid, cid, '<old@buyer.ru>', 'outgoing', 'Старая тема', 'Старый текст'))
    attempts = ai.store.query('SELECT * FROM outbound_attempts')
    messages = ai.store.query('SELECT * FROM mail_messages')
    ai._completion = lambda *args: response

    regenerated = ai.generate_offer(pid, cid, force=True)

    assert regenerated['id'] == offer_id
    assert regenerated['body'] == 'Предлагаем обсудить смартфоны для вашего ассортимента.'
    assert ai.store.query('SELECT * FROM outbound_attempts') == attempts
    assert ai.store.query('SELECT * FROM mail_messages') == messages
