"""Regression coverage for the five observed defects; no real keys/network."""
import json
from unittest.mock import Mock, patch

import httpx
import pytest

from sales_manager.ai.service import AIService, PossiblyChargedError
from sales_manager.mail.service import MailService
from sales_manager.research.providers import ProviderError
from sales_manager.research.service import ResearchService
from sales_manager.storage.settings_save import apply_settings, validate_extra_urls
from sales_manager.storage.store import Store


class FakeSecrets:
    def __init__(self, **initial):
        self.data = dict(initial)
        self.break_on = None

    def get(self, key, default=''):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        if key == self.break_on:
            self.break_on = None
            raise RuntimeError('Credential Manager отказал')


@pytest.fixture
def case(tmp_path):
    store = Store(tmp_path / 'test.db')
    yield store, FakeSecrets(gigachat_key='test-key')
    store.close()


def offer(body, sid=1):
    return {'subject': 'Предложение', 'body': body, 'reason': 'Гипотеза: расширение ассортимента.', 'source_ids': [sid]}


def card(text, sid=1):
    return {'product': 'Офисные кресла', 'company': 'Мебель', 'sender': 'Аргон',
            'signature': '', 'reason': 'Гипотеза: расширение ассортимента.',
            'sources': [{'id': sid, 'url': 'https://buyer.test/report', 'text': text}]}


def test_numerical_validation_rejects_substrings_and_phone_digitas():
    proof = card('Продано 1000 кресел. Телефон +7 999 123-45-67.')
    with pytest.raises(ValueError, match='неподтвержд'):
        AIService.validate_offer(offer('По данным компании, продано 100 кресел.'), proof)
    with pytest.raises(ValueError, match='неподтвержд'):
        AIService.validate_offer(offer('Компания работает в 7 городах.'), proof)


def test_numerical_validation_accepts_exact_and_thousands_grouping():
    proof = card('Компания продала 1 000 офисных кресел.')
    assert AIService.validate_offer(offer('Компания продала 1000 кресел.'), proof)['source_ids'] == [1]
    proof['sources'][0]['text'] = 'Компания продала 1000 офисных кресел.'
    assert AIService.validate_offer(offer('Компания продала 1 000 кресел.'), proof)


def test_numerical_validation_rejects_same_number_different_fact():
    proof = card('Компания расширилась на 7 сотрудников.')
    with pytest.raises(ValueError, match='неподтвержд'):
        AIService.validate_offer(offer('Компания работает в 7 городах.'), proof)


def test_sender_signature_numbers_do_not_claim_recipient_facts():
    proof = card('Поставщик офисных кресел для бизнеса.')
    proof['signature'] = 'Контакт: +7 999 123-45-67'
    assert AIService.validate_offer(offer('Предлагаем обсудить сотрудничество. Контакт: +7 999 123-45-67'), proof)


def test_failed_validation_keeps_settings_and_keys(case):
    store, secrets = case
    store.set_setting('sender.name', 'До')
    with pytest.raises(ValueError):
        validate_extra_urls({'rss_urls': ['not-a-url']})
    assert store.get_setting('sender.name') == 'До'
    assert secrets.get('gigachat_key') == 'test-key'


def test_credentials_failure_rolls_back_settings_and_mail(case):
    store, secrets = case
    store.set_setting('sender.name', 'Старое')
    secrets.break_on = 'tavily_key'
    with pytest.raises(RuntimeError, match='Credential Manager'):
        apply_settings(store, secrets, {'sender.name': 'Новое'},
                       {'gigachat_key': 'changed', 'tavily_key': 'new'},
                       ('owner@gmail.com', 'gmail', 'imap.gmail.com', 993, 'smtp.gmail.com', 465), 'pass')
    assert store.get_setting('sender.name') == 'Старое'
    assert store.one('SELECT * FROM mail_accounts') is None
    assert secrets.get('gigachat_key') == 'test-key'
    assert secrets.get('tavily_key') == ''
    assert secrets.get('mail_password') == ''


def test_successful_settings_commit_changes_together(case):
    store, secrets = case
    apply_settings(store, secrets, {'sender.name': 'Новое', 'ai.token_limit': 12345},
                   {'gigachat_key': 'new'},
                   ('owner@gmail.com', 'gmail', 'imap.gmail.com', 993, 'smtp.gmail.com', 465), 'pass')
    assert store.get_setting('sender.name') == 'Новое'
    assert store.get_setting('ai.token_limit') == 12345
    assert secrets.get('gigachat_key') == 'new'
    assert secrets.get('mail_password') == 'pass'
    assert store.one('SELECT email,connected FROM mail_accounts') == {'email': 'owner@gmail.com', 'connected': 0}


@pytest.mark.parametrize('at', ['ehlo', 'starttls', 'login'])
def test_smtp_587_always_closed_on_failure(case, at):
    store, secrets = case
    client = Mock()
    client.has_extn.return_value = True
    getattr(client, at).side_effect = RuntimeError('network fail')
    mail = MailService(store, secrets)
    with patch('sales_manager.mail.service.smtplib.SMTP', return_value=client), patch.object(mail, '_tls', return_value=None):
        with pytest.raises(RuntimeError, match='network fail'):
            mail._smtp({'smtp_host': 'smtp.example.org', 'smtp_port': 587, 'email': 'test@example.org'}, 'pw')
    client.quit.assert_called_once()


def test_smtp_587_closes_when_starttls_unsupported(case):
    store, secrets = case
    client = Mock()
    client.has_extn.return_value = False
    mail = MailService(store, secrets)
    with patch('sales_manager.mail.service.smtplib.SMTP', return_value=client), patch.object(mail, '_tls', return_value=None):
        with pytest.raises(RuntimeError, match='не поддерживает'):
            mail._smtp({'smtp_host': 'smtp.example.org', 'smtp_port': 587, 'email': 'test@example.org'}, 'pw')
    client.quit.assert_called_once()


def test_completion_read_timeout_counts_possible_usage_and_survives_restart(case):
    store, secrets = case
    store.set_setting('ai.token_limit', 2300)
    ai = AIService(store, secrets)
    ai._authorize = Mock()
    ai._request = Mock(side_effect=httpx.ReadTimeout('GigaChat read failed'))
    with pytest.raises(httpx.ReadTimeout):
        ai._completion('GigaChat-2', 'system', 'prompt', {}, 'offer')
    assert ai._reserved_tokens == 0
    value = store.get_setting('ai.unconfirmed_tokens')
    assert value > 0
    assert store.get_setting('ai.uncertain_requests') == 1
    restarted = AIService(store, secrets)
    restarted._authorize = Mock()
    restarted._request = Mock(return_value={'usage': {'prompt_tokens': 2, 'completion_tokens': 1}})
    with pytest.raises(ValueError, match='Лимит токенов'):
        restarted._completion('GigaChat-2', 'system', 'prompt', {}, 'offer')
    restarted._request.assert_not_called()


def test_completion_429_does_not_count_as_unknown(case):
    store, secrets = case
    ai = AIService(store, secrets)
    ai._authorize = Mock()
    ai._request = Mock(side_effect=RuntimeError('GigaChat: 429'))
    with pytest.raises(RuntimeError):
        ai._completion('GigaChat-2', 'test', 'test', {}, 'fit')
    assert ai._reserved_tokens == 0
    assert store.get_setting('ai.unconfirmed_tokens', 0) == 0


def test_http_timeout_translates_to_possible_usage_only_for_completion(case):
    store, secrets = case
    def transport(request):
        raise httpx.ReadTimeout('read', request=request)
    ai = AIService(store, secrets, httpx.Client(transport=httpx.MockTransport(transport)))
    ai._authorize = Mock()
    ai._token = 'bearer'
    with pytest.raises(PossiblyChargedError):
        ai._request('POST', '/chat/completions', json={})
    with pytest.raises(RuntimeError) as error:
        ai._request('GET', '/models')
    assert not isinstance(error.value, PossiblyChargedError)


def test_partial_rss_does_not_keep_pending_job(case):
    store, secrets = case
    research = ResearchService(store, secrets)
    rss = Mock(discover=Mock(return_value=[]), last_errors={'https://broken.example/rss': '503'})
    job = {'phase': 'search', 'provider': 'rss', 'country': 'Россия', 'query': '*', 'market': True, 'discover': True}
    result = research._run('market', None, ['Россия'], [job], {'rss': rss}, lambda _: None, None)
    assert result['status'] == 'complete'
    assert 'RSS' not in result['message']
    assert store.one("SELECT message FROM job_diagnostics WHERE message LIKE '%RSS%'")
    assert store.one('SELECT status FROM job_queue')['status'] == 'done'


def test_only_rss_failure_completes_with_honest_exhausted_summary(case):
    store, secrets = case
    research = ResearchService(store, secrets)
    rss = Mock(discover=Mock(side_effect=ProviderError('RSS не отвечает')), last_errors={})
    job = {'phase': 'search', 'provider': 'rss', 'country': 'Россия', 'query': '*', 'market': True, 'discover': True}
    first = research._run('market', None, ['Россия'], [job], {'rss': rss}, lambda _: None, None)
    assert first['status'] == 'complete' and first['sources_used'] == 0
    second = research._run('market', None, ['Россия'], [job], {'rss': rss}, lambda _: None, None)
    assert second['status'] == 'complete' and second['sources_used'] == 0
    assert 'Доступные источники исчерпаны' in second['message']
    assert 'RSS' not in second['message']
    assert store.one("SELECT message FROM job_diagnostics WHERE message LIKE '%RSS%'")
    assert rss.discover.call_count == 2
    assert store.one('SELECT status FROM job_queue')['status'] == 'done'


def test_numerical_claim_requires_the_source_actually_cited():
    proof = card('Поставщик офисной мебели', sid=1)
    proof['sources'].append({'id': 2, 'url': 'https://buyer.test/other',
                             'text': 'В другом источнике продано 1000 кресел.'})
    with pytest.raises(ValueError, match='неподтвержд'):
        AIService.validate_offer(offer('Компания продала 1000 кресел.', sid=1), proof)
    assert AIService.validate_offer(offer('Компания продала 1000 кресел.', sid=2), proof)


def test_success_response_broken_json_is_uncertain_usage(case):
    store, secrets = case
    def transport(request):
        return httpx.Response(200, text='not-json', request=request)
    ai = AIService(store, secrets, httpx.Client(transport=httpx.MockTransport(transport)))
    ai._authorize = Mock()
    ai._token = 'test-bearer'
    with pytest.raises(PossiblyChargedError):
        ai._completion('GigaChat-2', 'system', 'prompt', {}, 'offer')
    assert store.get_setting('ai.unconfirmed_tokens', 0) > 0
