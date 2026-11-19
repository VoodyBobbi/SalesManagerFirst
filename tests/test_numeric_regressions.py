"""Russian inflections and trusted sender contacts must not break factual checks."""
import json

import pytest

from sales_manager.ai.service import AIService
from sales_manager.storage import Store, now


def proof(text='', signature=''):
    return {'product': 'Мебель', 'company': 'Покупатель', 'sender': 'Аргон', 'signature': signature,
            'sources': [{'id': 1, 'url': 'https://buyer.test/report', 'text': text}]}


def draft(body, **fields):
    return {'subject': 'Сотрудничество', 'body': body, 'reason': 'Гипотеза: расширение ассортимента.',
            'source_ids': [1], **fields}


@pytest.mark.parametrize(('source', 'body'), [
    ('В 2026 году.', 'За 2026 год.'),
    ('Продажи за 2026 год.', 'Продажи в 2026 году.'),
    ('В 2026 году продано 100 кресел.', 'За 2026 год продано 100 кресел.'),
    ('Компания торгует 100 креслами.', 'Компания продала 100 кресел.'),
    ('Компания продала 100 кресел.', 'Компания торгует 100 креслами.'),
    ('Компания продала 100 офисных кресел.', 'Компания торгует 100 креслами.'),
    ('Компания продала 100 тысяч кресел.', 'Компания торгует 100 тысячами кресел.'),
    ('Поставка составляет 100 смартфонов.', 'Поставка представлена 100 смартфонами.'),
    ('Поставка составляет 100 столов.', 'Поставка представлена 100 столами.'),
    ('Срок составляет 100 дней.', 'Работа со сроком в 100 днях.'),
])
def test_supported_inflections_preserve_same_quantity(source, body):
    assert AIService.validate_offer(draft(body), proof(source))['body'] == body


@pytest.mark.parametrize(('source', 'body'), [
    ('Продажи в 2026 году.', 'Продажи в 2027 году.'),
    ('Компания продала 100 кресел.', 'Компания продала 100 столов.'),
    ('Компания продала 100 кг кресел.', 'Компания продала 100 кресел.'),
    ('Компания продала 100 упаковок кресел.', 'Компания продала 100 кресел.'),
    ('Компания продала 100 тысяч кресел.', 'Компания продала 100 кресел.'),
    ('Работа за 100 дней.', 'Работа за 100 сотрудников.'),
    ('Рост 100 процентов в году.', 'Рост 100 кресел в году.'),
    ('Сотруднику исполнилось 20 лет.', 'В 20 году открыли филиал.'),
    ('Филиал работает 2026 лет.', 'Филиал работает с 2026 года.'),
    ('Объём указан в 2026 году.', 'Объём указан за 2026 лет.'),
])
def test_inflection_support_cannot_change_number_dimension_or_magnitude(source, body):
    with pytest.raises(ValueError, match='неподтвержд'):
        AIService.validate_offer(draft(body), proof(source))


@pytest.mark.parametrize('model_signature', [
    'Иван, офис 12, Контакт: +7 999 123-45-67',
    'Иван\nофис 12\nКонтакт: +7 (999) 123 45 67',
    'Иван  офис 12; Контакт +79991234567',
    'ИВАН, офис 12. Контакт: +7 999 123-45-67.',
])
def test_signature_formatting_is_replaced_with_exact_saved_signature(model_signature):
    signature = 'Иван, офис 12, Контакт: +7 999 123-45-67'
    checked = AIService.validate_offer(draft('Обсудим сотрудничество.\n' + model_signature), proof(signature=signature))
    assert checked['body'] == 'Обсудим сотрудничество.\n\n' + signature


def test_signature_is_added_when_model_does_not_include_it():
    signature = 'Иван, офис 12, Контакт: +7 999 123-45-67'
    checked = AIService.validate_offer(draft('Обсудим сотрудничество.'), proof(signature=signature))
    assert checked['body'] == 'Обсудим сотрудничество.\n\n' + signature


def test_phone_only_signature_leaves_no_stray_formatting_in_prose():
    signature = '+7 999 123-45-67'
    checked = AIService.validate_offer(draft('Обсудим сотрудничество.\n+7 (999) 123 45 67'), proof(signature=signature))
    assert checked['body'] == 'Обсудим сотрудничество.\n\n' + signature


@pytest.mark.parametrize('body', [
    'Компания работает в 12 городах.\nИван, офис 12, Контакт: +7 999 123-45-67',
    'Обсудим сотрудничество.\nИван, офис 13, Контакт: +7 999 123-45-67',
    'Обсудим сотрудничество.\nИван, офис 12, Контакт: +7 999 123-45-68',
])
def test_signature_cannot_hide_new_quantities_or_changed_contacts(body):
    signature = 'Иван, офис 12, Контакт: +7 999 123-45-67'
    with pytest.raises(ValueError, match='неподтвержд'):
        AIService.validate_offer(draft(body), proof(signature=signature))


def test_signature_numbers_in_subject_are_not_exempt_from_fact_check():
    signature = 'Офис 12'
    with pytest.raises(ValueError, match='неподтвержд'):
        AIService.validate_offer(draft('Обсудим сотрудничество.', subject='Офис 12'), proof(signature=signature))


def test_generation_adds_trusted_signature_and_cache_preserves_it(tmp_path):
    store = Store(tmp_path / 'signed.sqlite3')
    try:
        pid = store.ensure_product('Смартфон')['id']
        cid = store.execute('INSERT INTO companies(name,created_at) VALUES(?,?)', ('Клиент', now()))
        sid = store.put_source('https://buyer.test/about', text='Компания продаёт электронику.')
        store.execute('INSERT INTO company_fit(product_id,company_id,status,reason,source_ids) VALUES(?,?,?,?,?)',
                      (pid, cid, 'FIT', 'Гипотеза: расширение ассортимента.', json.dumps([sid])))
        signature = 'Иван, офис 12, Контакт: +7 999 123-45-67'
        store.set_setting('sender.signature', signature)
        calls = []

        def complete(model, system, prompt, schema, operation):
            calls.append(system)
            data = draft('Обсудим сотрудничество.', source_ids=[sid])
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(data)}}]}

        ai = AIService(store, object())
        ai._completion = complete
        first = ai.generate_offer(pid, cid)
        second = ai.generate_offer(pid, cid)
        assert first['body'] == second['body'] == 'Обсудим сотрудничество.\n\n' + signature
        assert len(calls) == 1
        assert 'Не добавляй подпись' in calls[0]
    finally:
        store.close()
