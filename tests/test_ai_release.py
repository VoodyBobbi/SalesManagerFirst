"""End-to-end saved-fact checks for inflections and semantic buyer decisions."""
import json

import pytest

from sales_manager.ai.service import AIService
from sales_manager.storage import Store, now


def numeric_offer(source, body):
    card = {'product': 'Товар', 'company': 'Покупатель', 'sender': 'Аргон', 'signature': '',
            'sources': [{'id': 1, 'url': 'https://buyer.test/report', 'text': source}]}
    draft = {'subject': 'Сотрудничество', 'body': body,
             'reason': 'Гипотеза: сотрудничество.', 'source_ids': [1]}
    return AIService.validate_offer(draft, card)


@pytest.mark.parametrize('source,body', [
    ('Отчёт 2026 года.', 'Отчёт за 2026 год.'),
    ('В ноябре 2026 года опубликован отчёт.', 'За 2026 год опубликован отчёт.'),
    ('По итогам октября 2026 года опубликован отчёт.', 'В 2026 году опубликован отчёт.'),
    ('Компания работает в 5 магазинах.', 'У компании 5 магазинов.'),
    ('Компания владеет 10 принтерами.', 'У компании 10 принтеров.'),
    ('Срок составляет 6 месяцев.', 'Срок равен 6 месяцам.'),
    ('Компания располагает 8 складами.', 'У компании 8 складов.'),
    ('Компания работает с 9 предприятиями.', 'У компании 9 предприятий.'),
    ('Компания работает в 3 филиалах.', 'У компании 3 филиала.'),
    ('Компания торгует 10 стульями.', 'Компания продала 10 стульев.'),
    ('Компания владеет 12 станками.', 'У компании 12 станков.'),
])
def test_real_inflections_do_not_block_drafts(source, body):
    assert numeric_offer(source, body)['body'] == body


@pytest.mark.parametrize('source,body', [
    ('Компания владеет 10 принтерами.', 'Компания владеет 10 принтерами и 10 сканерами.'),
    ('У компании 5 магазинов.', 'У компании 5 складов.'),
    ('Объём составляет 6 литров.', 'Объём составляет 6 месяцев.'),
    ('Срок службы составляет 2026 года.', 'Срок службы с 2026 года.'),
    ('Отчёт 2026 года.', 'Отчёт за 2027 год.'),
    ('Компания владеет 10 станками.', 'Компания владеет 10 станциями.'),
    ('Компания владеет 10 принтерами.', 'Компания владеет 100 принтерами.'),
])
def test_inflections_do_not_relax_quantity_grounding(source, body):
    with pytest.raises(ValueError, match='неподтвержд'):
        numeric_offer(source, body)


@pytest.mark.parametrize('source,body', [
    ('Продажи копиров выросли на 20%.', 'Продажи копиров упали на 20%.'),
    ('Продажи копиров упали на 20%.', 'Продажи копиров на 20% выше.'),
    ('Доля продаж выросла на 20 процентов.', 'Доля продаж выросла на 20 процентных пунктов.'),
    ('Доля продаж выросла на 20 п. п.', 'Доля продаж выросла на 20%.'),
    ('Доля продаж не выросла на 20%.', 'Доля продаж выросла на 20%.'),
    ('Доля продаж составила 20%.', 'Доля продаж выросла на 20%.'),
])
def test_percentage_claim_rejects_a_changed_direction_or_unit(source, body):
    with pytest.raises(ValueError, match='неподтвержд'):
        numeric_offer(source, body)


@pytest.mark.parametrize('source,body', [
    ('Продажи копиров выросли на 20 процентов.', 'Продажи копиров увеличились на 20%.'),
    ('Продажи копиров упали на 20%.', 'Продажи копиров на 20% ниже.'),
    ('Доля продаж выросла на 20 п. п.', 'Рост доли продаж составил 20 процентных пунктов.'),
])
def test_percentage_claim_accepts_equivalent_direction_and_unit(source, body):
    assert numeric_offer(source, body)['body'] == body


@pytest.mark.parametrize('source,body', [
    ('Компания владеет не более 20 магазинами.', 'Компания владеет 20 магазинами.'),
    ('Компания владеет примерно 20 магазинами.', 'Компания владеет ровно 20 магазинами.'),
    ('Доля продаж составляет от 10 до 20%.', 'Доля продаж составляет 20%.'),
    ('Доля продаж составляет 10–20%.', 'Доля продаж составляет 15–20%.'),
    ('Компания владеет менее 20 магазинами.', 'Компания владеет не более чем 20 магазинами.'),
])
def test_offer_rejects_unsupported_exactness_or_changed_range(source, body):
    with pytest.raises(ValueError, match='неподтвержд'):
        numeric_offer(source, body)


@pytest.mark.parametrize('source,body', [
    ('Компания владеет примерно 20 магазинами.', 'Компания владеет около 20 магазинами.'),
    ('Компания владеет не более чем 20 магазинами.', 'Компания владеет до 20 магазинами.'),
    ('Доля продаж составляет от 10 до 20%.', 'Доля продаж составляет 10–20%.'),
    ('Продажи копиров выросли на 10–20%.', 'Продажи копиров увеличились на 10–20%.'),
])
def test_offer_accepts_equivalent_bounds_and_ranges(source, body):
    assert numeric_offer(source, body)['body'] == body


class Keys:
    def get(self, key, default=''):
        return 'test-key' if key == 'gigachat_key' else default


@pytest.fixture
def semantic_case(tmp_path):
    store = Store(tmp_path / 'semantic.sqlite3')
    pid = store.ensure_product('Принтер')['id']
    cid = store.execute('INSERT INTO companies(name,url,created_at) VALUES(?,?,?)',
                        ('Типография', 'https://buyer.test/', now()))
    text = 'Мы печатаем книги и учебники для школ.'
    sid = store.put_source('https://buyer.test/', text=text)
    store.execute('INSERT INTO company_fit(product_id,company_id,status,reason,source_ids) VALUES(?,?,?,?,?)',
                  (pid, cid, 'INSUFFICIENT_EVIDENCE', 'Нужна проверка.', json.dumps([sid])))
    ai = AIService(store, Keys())
    result = {'status': 'FIT', 'reason': 'Гипотеза: принтеры могут использоваться при печати книг.',
              'source_ids': [sid], 'evidence_quote': text}
    calls = []

    def completion(*args):
        calls.append(args)
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(result)}}]}

    ai._completion = completion
    yield store, ai, pid, cid, sid, result, calls
    store.close()


def test_semantic_relation_outside_local_templates_can_fit_and_cache(semantic_case):
    store, ai, pid, cid, sid, result, calls = semantic_case
    assert ai.assess_fit(pid, cid)['status'] == 'FIT'
    assert ai.assess_fit(pid, cid)['status'] == 'FIT'
    assert len(calls) == 1
    assert store.one('SELECT status,manual FROM company_fit') == {'status': 'FIT', 'manual': 0}
    assert store.one('SELECT ai_checked_key FROM company_fit')['ai_checked_key'].startswith('semantic-v6:')


def test_invalid_cached_numeric_claim_cannot_reapply_fit(semantic_case):
    store, ai, pid, cid, _, result, calls = semantic_case
    text = 'Мы печатаем книги и учебники для школ. У компании около 20 принтеров.'
    sid = store.put_source('https://buyer.test/', text=text)
    store.execute('UPDATE company_fit SET source_ids=?', (json.dumps([sid]),))
    result.update(source_ids=[sid], evidence_quote='Мы печатаем книги и учебники для школ.')
    ai.assess_fit(pid, cid)
    previous = store.one('SELECT status,reason FROM company_fit')
    invalid = {**result, 'status': 'NOT_FIT', 'reason': 'У компании ровно 20 принтеров.'}
    store.execute("UPDATE cache SET value=? WHERE key LIKE 'fit:%'", (json.dumps(invalid),))

    with pytest.raises(ValueError, match='нужна проверка'):
        ai.assess_fit(pid, cid)

    assert store.one('SELECT status,reason FROM company_fit') == previous
    assert not store.one("SELECT key FROM cache WHERE key LIKE 'fit:%'")
    assert len(calls) == 1


@pytest.mark.parametrize('text,quote', [
    ('Наш партнёр — типография. Мы печатаем книги и учебники для школ.', 'Наш партнёр — типография.'),
    ('Наш партнёр печатает книги и учебники для школ.', 'печатает книги и учебники для школ.'),
    ('Мы печатаем книги и учебники для школ. Мы не закупаем принтеры.',
     'Мы печатаем книги и учебники для школ.'),
    ('Мы печатаем книги и учебники для школ. Компания ликвидирована.',
     'Мы печатаем книги и учебники для школ.'),
])
def test_model_cannot_ignore_original_context(semantic_case, text, quote):
    store, ai, pid, cid, old_sid, result, calls = semantic_case
    sid = store.put_source('https://buyer.test/', text=text)
    store.execute('UPDATE company_fit SET source_ids=?', (json.dumps([sid]),))
    result.update(source_ids=[sid], evidence_quote=quote)
    assert ai.assess_fit(pid, cid)['status'] == 'INSUFFICIENT_EVIDENCE'


def test_manual_decision_during_ai_request_cannot_be_overwritten(semantic_case):
    store, ai, pid, cid, sid, result, calls = semantic_case
    complete = ai._completion

    def concurrent_decision(*args):
        store.set_company_fit_manual(pid, cid, 'NOT_FIT', 'Проверено пользователем: компания отказалась.')
        return complete(*args)

    ai._completion = concurrent_decision
    with pytest.raises(ValueError, match='уже изменена'):
        ai.assess_fit(pid, cid)
    assert store.one('SELECT status,manual FROM company_fit') == {'status': 'NOT_FIT', 'manual': 1}
    assert store.one('SELECT reason FROM company_fit')['reason'].endswith('компания отказалась.')


def test_semantic_fit_survives_restart_and_legacy_review(semantic_case):
    store, ai, pid, cid, sid, result, calls = semantic_case
    ai.assess_fit(pid, cid)
    store.execute("DELETE FROM settings WHERE key LIKE 'fit.%reviewed%'")
    store.close()
    reopened = Store(store.path)
    try:
        assert reopened.one('SELECT status FROM company_fit')['status'] == 'FIT'
    finally:
        reopened.close()
