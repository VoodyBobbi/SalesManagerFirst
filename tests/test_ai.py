import json
import time
import hashlib
import ssl
from pathlib import Path
import httpx
import pytest

from sales_manager.ai import AIService
from sales_manager.storage import Store, now


class MemorySecrets:
    def __init__(self, **values):
        self.values = values
    def get(self, key, default=''):
        return self.values.get(key, default)
    def delete(self, key):
        self.values.pop(key, None)


@pytest.fixture
def ai_card(tmp_path):
    store = Store(tmp_path / 'app.sqlite3')
    p = store.ensure_product('Смартфон')
    cid = store.execute('INSERT INTO companies(name,domain,url,created_at) VALUES(?,?,?,?)',
                        ('Покупатель', 'client.test', 'https://client.test', now()))
    sid = store.put_source('https://client.test/about', 'О компании', 'Компания продаёт электронику организациям и частным покупателям.')
    store.execute('INSERT INTO company_fit(product_id,company_id,status,reason,source_ids) VALUES(?,?,?,?,?)',
                  (p['id'], cid, 'FIT', 'Гипотеза: расширение ассортимента электроники.', json.dumps([sid])))
    return store, p['id'], cid, sid


def service_with_response(store, sid, response=None, usage=True):
    calls = []
    def handle(request):
        calls.append(request)
        if request.url.path.endswith('/oauth'):
            return httpx.Response(200, json={'access_token': 'test-token', 'expires_at': int(time.time()) + 1800})
        if request.url.path.endswith('/models'):
            return httpx.Response(200, json={'data': [{'id': 'GigaChat-2'}, {'id': 'GigaChat-2-Max'}]})
        if request.url.path.endswith('/balance'):
            return httpx.Response(403, json={})
        content = response or {'subject': 'Возможность сотрудничества', 'body': 'Предлагаем обсудить смартфоны для вашего ассортимента.', 'reason': 'Гипотеза: расширение ассортимента электроники.', 'source_ids': [sid]}
        data = {'choices': [{'message': {'content': json.dumps(content, ensure_ascii=False)}, 'finish_reason': 'stop'}]}
        if usage:
            data['usage'] = {'prompt_tokens': 231, 'completion_tokens': 91}
        return httpx.Response(200, json=data)
    client = httpx.Client(transport=httpx.MockTransport(handle))
    return AIService(store, MemorySecrets(gigachat_key='test-credential'), client), calls


def test_generate_caches_and_logs_only_actual_usage(ai_card):
    store, pid, cid, sid = ai_card
    ai, calls = service_with_response(store, sid)
    first = ai.generate_offer(pid, cid)
    second = ai.generate_offer(pid, cid)
    assert first['id'] == second['id']
    assert len([c for c in calls if c.url.path.endswith('completions')]) == 1
    assert store.one('SELECT input_tokens,output_tokens FROM usage_log') == {'input_tokens': 231, 'output_tokens': 91}
    assert store.get_setting('ai.unconfirmed_tokens') == 0


def test_user_edits_survive_model_source_changes(ai_card):
    store, pid, cid, sid = ai_card
    ai, calls = service_with_response(store, sid)
    offer = ai.generate_offer(pid, cid)
    store.execute("UPDATE offers SET body='Правка пользователя',edited=1 WHERE id=?", (offer['id'],))
    store.set_setting('ai.model', 'GigaChat-2-Max')
    result = ai.generate_offer(pid, cid)
    assert result['body'] == 'Правка пользователя'
    assert len([c for c in calls if c.url.path.endswith('completions')]) == 1


def test_unedited_cache_invalidated_by_sender_change(ai_card):
    store, pid, cid, sid = ai_card
    ai, calls = service_with_response(store, sid)
    first = ai.generate_offer(pid, cid)
    store.set_setting('sender.name', 'Другой отправитель')
    second = ai.generate_offer(pid, cid)
    assert first['cache_key'] != second['cache_key']
    assert len([c for c in calls if c.url.path.endswith('completions')]) == 2


def test_rejects_invented_claims_but_accounts_usage(ai_card):
    store, pid, cid, sid = ai_card
    bad = {'subject': 'Предложение', 'body': 'Доставим за 7 дней со скидкой.', 'reason': 'В наличии.', 'source_ids': [sid]}
    ai, _ = service_with_response(store, sid, bad)
    with pytest.raises(ValueError, match='неподтвержд'):
        ai.generate_offer(pid, cid)
    assert store.one('SELECT id FROM offers') is None
    assert store.one('SELECT input_tokens FROM usage_log')['input_tokens'] == 231


def test_unknown_source_rejected(ai_card):
    store, pid, cid, sid = ai_card
    bad = {'subject': 'Предложение', 'body': 'Предлагаем обсудить сотрудничество.', 'reason': 'Гипотеза: ассортимент.', 'source_ids': [999]}
    ai, _ = service_with_response(store, sid, bad)
    with pytest.raises(ValueError, match='источник'):
        ai.generate_offer(pid, cid)


def test_cap_blocks_before_generation(ai_card):
    store, pid, cid, sid = ai_card
    store.set_setting('ai.token_limit', 1)
    ai, calls = service_with_response(store, sid)
    with pytest.raises(ValueError, match='Лимит'):
        ai.generate_offer(pid, cid)
    assert not any(c.url.path.endswith('completions') for c in calls)


def test_test_fetches_real_models_and_marks_unavailable_balance(ai_card):
    store, _, _, sid = ai_card
    ai, _ = service_with_response(store, sid)
    assert 'недоступен' in ai.test()
    assert store.get_setting('ai.models') == ['GigaChat-2', 'GigaChat-2-Max']
    assert store.get_setting('ai.balance') == {'status': 'unavailable'}
    assert store.get_setting('ai.checked_at')


def test_missing_usage_is_unknown_but_provisionally_counts_against_budget(ai_card):
    store, pid, cid, sid = ai_card
    ai, _ = service_with_response(store, sid, usage=False)
    ai.generate_offer(pid, cid)
    assert store.get_setting('ai.usage_incomplete') is True
    assert store.get_setting('ai.unconfirmed_tokens') > 0
    assert ai._reserved_tokens == 0
    assert store.one('SELECT input_tokens,output_tokens FROM usage_log') == {'input_tokens': None, 'output_tokens': None}


def test_null_usage_is_treated_as_unknown_not_an_attribute_error(ai_card, monkeypatch):
    store, _, _, sid = ai_card
    ai, _ = service_with_response(store, sid)
    monkeypatch.setattr(ai, '_request', lambda *a, **k: {'usage': None, 'choices': []})
    ai._completion('GigaChat-2', 'text', 'text', {}, 'test')
    assert store.get_setting('ai.usage_incomplete') is True
    assert store.get_setting('ai.unconfirmed_tokens') > 0
    assert ai._reserved_tokens == 0


def test_missing_usage_estimate_survives_restart_and_prevents_unlimited_calls(ai_card):
    store, pid, cid, sid = ai_card
    ai, _ = service_with_response(store, sid, usage=False)
    store.set_setting('ai.token_limit', 2200)
    ai._completion('GigaChat-2', 'короткий текст', 'текст', {}, 'test')
    estimated = store.get_setting('ai.unconfirmed_tokens')
    assert 1000 < estimated < 2200
    ai2, calls = service_with_response(store, sid, usage=False)
    with pytest.raises(ValueError, match='Лимит токенов'):
        ai2._completion('GigaChat-2', 'короткий текст', 'текст', {}, 'test')
    assert not any(c.url.path.endswith('completions') for c in calls)
    assert store.get_setting('ai.unconfirmed_tokens') == estimated


def test_lost_request_reserve_survives_restart_and_can_be_reconciled(ai_card, monkeypatch):
    store, _, _, sid = ai_card
    ai, _ = service_with_response(store, sid)
    store.set_setting('ai.token_limit', 2200)
    def lost(*args, **kwargs):
        assert store.get_setting('ai.unconfirmed_tokens') > 0
        raise httpx.ReadTimeout('lost response')
    monkeypatch.setattr(ai, '_request', lost)
    with pytest.raises(httpx.ReadTimeout):
        ai._completion('GigaChat-2', 'text', 'text', {}, 'test')
    estimate = store.get_setting('ai.unconfirmed_tokens')
    restarted, calls = service_with_response(store, sid)
    with pytest.raises(ValueError, match='Сверьте'):
        restarted._completion('GigaChat-2', 'text', 'text', {}, 'test')
    assert not any(c.url.path.endswith('completions') for c in calls)
    with pytest.raises(ValueError, match='изменился'):
        restarted.reconcile_uncertain_usage(300, expected_estimate=estimate + 1)
    assert store.get_setting('ai.unconfirmed_tokens') == estimate
    restarted.reconcile_uncertain_usage(300)
    assert store.get_setting('ai.unconfirmed_tokens') == 0
    assert store.get_setting('ai.usage_incomplete') is False
    assert store.one("SELECT input_tokens FROM usage_log WHERE operation='manual_reconciliation'")['input_tokens'] == 300
    assert str(estimate) in store.one("SELECT details FROM audit_events WHERE event='ai_usage_reconciled'")['details']
    restarted._completion('GigaChat-2', 'text', 'text', {}, 'test')
    assert len([c for c in calls if c.url.path.endswith('completions')]) == 1


def test_interrupted_process_leaves_saved_possible_cost(ai_card, monkeypatch):
    store, _, _, sid = ai_card
    ai, _ = service_with_response(store, sid)
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt()
    monkeypatch.setattr(ai, '_request', interrupted)
    with pytest.raises(KeyboardInterrupt):
        ai._completion('GigaChat-2', 's', 'p', {}, 'test')
    assert store.get_setting('ai.unconfirmed_tokens') > 0
    assert ai._reserved_tokens == 0


def test_failed_calls_do_not_reserve_undocumented_usage(ai_card, monkeypatch):
    store, _, _, sid = ai_card
    ai, _ = service_with_response(store, sid)
    monkeypatch.setattr(ai, '_request', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('429')))
    for _ in range(3):
        with pytest.raises(RuntimeError, match='429'):
            ai._completion('GigaChat-2', 'текст', 'текст', {}, 'test')
    assert ai._reserved_tokens == 0
    assert store.get_setting('ai.unconfirmed_tokens') == 0


@pytest.mark.parametrize('error', [RuntimeError('GigaChat: ошибка сервиса'), ValueError('Некорректный JSON'), httpx.ConnectError('unreachable')])
def test_failed_completion_releases_reserve_and_next_request_succeeds(ai_card, monkeypatch, error):
    store, _, _, sid = ai_card
    ai, _ = service_with_response(store, sid)
    store.set_setting('ai.token_limit', 2300)
    def failed(*args, **kwargs):
        assert ai._reserved_tokens > 0
        raise error
    monkeypatch.setattr(ai, '_request', failed)
    for _ in range(4):
        with pytest.raises(type(error)):
            ai._completion('GigaChat-2', 's', 'p', {}, 'test')
        assert ai._reserved_tokens == 0
        assert not store.query('SELECT * FROM usage_log')
    monkeypatch.setattr(ai, '_request', lambda *args, **kwargs: {'usage': {'prompt_tokens': 2, 'completion_tokens': 3}})
    ai._completion('GigaChat-2', 's', 'p', {}, 'test')
    assert ai._reserved_tokens == 0
    assert store.one('SELECT input_tokens+output_tokens n FROM usage_log')['n'] == 5


def test_semantic_fit_requires_exact_quote(ai_card):
    store, pid, cid, sid = ai_card
    store.execute("UPDATE company_fit SET status='INSUFFICIENT_EVIDENCE'")
    result = {'status': 'FIT', 'reason': 'Гипотеза: товар дополнит ассортимент электроники.',
              'source_ids': [sid], 'evidence_quote': 'Компания продаёт электронику организациям'}
    ai, calls = service_with_response(store, sid, result)
    assert ai.assess_fit(pid, cid)['status'] == 'FIT'
    assert store.one('SELECT status FROM company_fit')['status'] == 'FIT'
    ai.assess_fit(pid, cid)
    assert len([c for c in calls if c.url.path.endswith('completions')]) == 1


def test_old_semantic_fit_cache_is_reassessed(ai_card):
    store, pid, cid, sid = ai_card
    store.execute("UPDATE company_fit SET status='INSUFFICIENT_EVIDENCE'")
    result = {'status': 'INSUFFICIENT_EVIDENCE', 'reason': 'Нужна проверка.',
              'source_ids': [sid], 'evidence_quote': 'Компания продаёт электронику организациям'}
    ai, calls = service_with_response(store, sid, result)
    card = ai._card(pid, cid, require_fit=False)
    payload = json.dumps({k: card[k] for k in ('product', 'company', 'sources')}, ensure_ascii=False, sort_keys=True)
    old_key = 'fit:' + hashlib.sha256(('fit-v3' + 'GigaChat-2' + payload).encode()).hexdigest()
    old = {**result, 'status': 'FIT', 'reason': 'Гипотеза: старая оценка.'}
    store.execute('INSERT INTO cache(key,value,expires_at) VALUES(?,?,?)', (old_key, json.dumps(old), time.time() + 3600))
    assert ai.assess_fit(pid, cid)['status'] == 'INSUFFICIENT_EVIDENCE'
    assert len([c for c in calls if c.url.path.endswith('completions')]) == 1


def test_semantic_fit_rejects_fabricated_quote(ai_card):
    store, pid, cid, sid = ai_card
    store.execute("UPDATE company_fit SET status='INSUFFICIENT_EVIDENCE'")
    bad = {'status': 'FIT', 'reason': 'Гипотеза: интересен товар.', 'source_ids': [sid],
           'evidence_quote': 'Компания срочно ищет поставщика смартфонов.'}
    ai, _ = service_with_response(store, sid, bad)
    with pytest.raises(ValueError, match='не подтвердил'):
        ai.assess_fit(pid, cid)
    assert store.one('SELECT status FROM company_fit')['status'] == 'INSUFFICIENT_EVIDENCE'


def test_bundled_gigachat_ca_fingerprint_and_validity():
    import sales_manager.ai.service as module
    path = Path(module.__file__).parent / 'certs' / 'russian_trusted_root_ca.pem'
    der = ssl.PEM_cert_to_DER_cert(path.read_text(encoding='ascii'))
    assert hashlib.sha256(der).hexdigest() == 'd26d2d0231b7c39f92cc738512ba54103519e4405d68b5bd703e9788ca8ecf31'
    context = ssl.create_default_context(cafile=str(path))
    anchors = context.get_ca_certs()
    assert len(anchors) == 1
    assert ('commonName', 'Russian Trusted Root CA') in anchors[0]['subject'][-1]
    assert anchors[0]['notAfter'] == 'Feb 27 21:04:15 2032 GMT'


def test_scoped_tls_keeps_chain_and_hostname_checks():
    context = AIService._tls()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


@pytest.mark.parametrize('payload', [[], 'unexpected', None])
def test_wrong_completion_json_shape_preserves_possible_usage(ai_card, payload):
    from sales_manager.ai.service import PossiblyChargedError
    store, _, _, _ = ai_card
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
    ai = AIService(store, MemorySecrets(gigachat_key='test'), client)
    ai._authorize = lambda: None
    with pytest.raises(PossiblyChargedError):
        ai._completion('GigaChat-2', 'system', 'prompt', {}, 'offer')
    assert ai._reserved_tokens == 0
    assert store.get_setting('ai.unconfirmed_tokens') > 0
    assert not store.query('SELECT * FROM usage_log')


@pytest.mark.parametrize('choices', [['not-an-object'], {'0': 'wrong'}, None])
def test_malformed_choices_show_validation_error_and_account_confirmed_usage(ai_card, monkeypatch, choices):
    store, product_id, company_id, source_id = ai_card
    ai, _ = service_with_response(store, source_id)
    original_request = ai._request
    def malformed(method, path, **kwargs):
        if path == '/chat/completions':
            return {'usage': {'prompt_tokens': 3, 'completion_tokens': 2}, 'choices': choices}
        return original_request(method, path, **kwargs)
    monkeypatch.setattr(ai, '_request', malformed)
    with pytest.raises(ValueError, match='некорректный JSON'):
        ai.generate_offer(product_id, company_id)
    assert not store.query('SELECT * FROM offers')
    assert store.one('SELECT input_tokens+output_tokens n FROM usage_log')['n'] == 5


def test_models_with_malformed_entries_produce_guidance_not_attribute_error(ai_card):
    store, _, _, _ = ai_card
    ai = AIService(store, MemorySecrets(gigachat_key='test'))
    ai._request = lambda *args, **kwargs: {'data': ['not-an-object', {}, {'id': None}]}
    with pytest.raises(RuntimeError, match='не вернул список моделей'):
        ai.test()


@pytest.mark.parametrize('token', ['', [], None])
def test_oauth_requires_nonempty_string_token(ai_card, token):
    store, _, _, _ = ai_card
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'access_token': token})))
    ai = AIService(store, MemorySecrets(gigachat_key='test'), client)
    with pytest.raises(RuntimeError, match='некорректный ответ авторизации'):
        ai._authorize()
    assert ai._token == ''


def test_offer_quote_must_be_in_cited_source_not_saved_hypothesis(ai_card):
    store, product_id, company_id, source_id = ai_card
    ai, _ = service_with_response(store, source_id)
    proof = ai._card(product_id, company_id)
    quote = 'Компания срочно ищет нового поставщика'
    proof['reason'] = 'Гипотеза: ' + quote
    offer = {'subject': 'Предложение', 'body': 'По вашим сведениям: «' + quote + '».',
             'reason': 'Гипотеза: расширение ассортимента.', 'source_ids': [source_id]}
    with pytest.raises(ValueError, match='Цитата'):
        ai.validate_offer(offer, proof)


@pytest.mark.parametrize('bad_url', ['https://client', 'https://client.test/about/Other',
                                   'https://client.test/about?unrelated=1'])
def test_offer_link_requires_exact_cited_page(ai_card, bad_url):
    store, product_id, company_id, source_id = ai_card
    ai, _ = service_with_response(store, source_id)
    offer = {'subject': 'Предложение', 'body': 'Информация о компании: ' + bad_url,
             'reason': 'Гипотеза: ассортимент.', 'source_ids': [source_id]}
    with pytest.raises(ValueError, match='ссылку'):
        ai.validate_offer(offer, ai._card(product_id, company_id))


def test_offer_link_accepts_anchor_of_cited_page(ai_card):
    store, product_id, company_id, source_id = ai_card
    ai, _ = service_with_response(store, source_id)
    offer = {'subject': 'Предложение', 'body': 'Информация: https://CLIENT.test/about#contacts',
             'reason': 'Гипотеза: ассортимент.', 'source_ids': [source_id]}
    assert ai.validate_offer(offer, ai._card(product_id, company_id))


def test_cited_page_numbers_are_not_business_claims(ai_card):
    store, product_id, company_id, source_id = ai_card
    ai, _ = service_with_response(store, source_id)
    proof = ai._card(product_id, company_id)
    proof['sources'][0]['url'] = 'https://client.test/reports/2026/105'
    offer = {'subject': 'Предложение', 'body': 'Источник: https://client.test/reports/2026/105',
             'reason': 'Гипотеза: ассортимент.', 'source_ids': [source_id]}
    assert ai.validate_offer(offer, proof)


@pytest.mark.parametrize('source_text,body', [
    ('Компания продала 100 столов.', 'Компания продала 100 кресел.'),
    ('Кресла стоят 100 рублей.', 'Компания продала 100 кресел.'),
    ('Спрос на кресла вырос на 100 процентов.', 'Компания продала 100 кресел.'),
    ('Компания продала 100 тысяч кресел.', 'Компания продала 100 кресел.'),
    ('Спрос на кресла изменился на -100 процентов.', 'Спрос на кресла изменился на 100 процентов.'),
    ('Спрос на кресла изменился на -0,5 процента.', 'Спрос на кресла изменился на 0,5 процента.'),
    ('Спрос на кресла вырос на 5 процентов.', 'Спрос на кресла вырос на +100 процентов.'),
    ('Кресла стоят 1000рублей.', 'Кресла стоят 100рублей.'),
    ('Компания продала 100 килограммов кресел.', 'Компания продала 100 штук кресел.'),
    ('Компания продала 100 кг кресел.', 'Компания продала 100 г кресел.'),
    ('Компания продала 100 упаковок кресел.', 'Компания продала 100 коробок кресел.'),
])
def test_numerical_claim_requires_matching_item_and_explicit_unit(ai_card, source_text, body):
    store, product_id, company_id, source_id = ai_card
    ai, _ = service_with_response(store, source_id)
    proof = ai._card(product_id, company_id)
    proof['sources'][0]['text'] = source_text
    offer = {'subject': 'Предложение', 'body': body,
             'reason': 'Гипотеза: ассортимент.', 'source_ids': [source_id]}
    with pytest.raises(ValueError, match='неподтвержд'):
        ai.validate_offer(offer, proof)
