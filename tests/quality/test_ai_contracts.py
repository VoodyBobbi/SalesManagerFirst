"""Validate LLM outputs and injected API failures without calling GigaChat."""
import pytest
pytest.importorskip('truststore')

import httpx
from unittest.mock import Mock

from sales_manager.ai.service import AIService


def card():
    return {
        'product': 'Офисные кресла', 'company': 'Компания', 'sender': 'Аргон',
        'signature': '', 'reason': 'Гипотеза: расширение ассортимента.',
        'sources': [{'id': 42, 'url': 'https://buyer.ru/company',
                     'text': 'Компания продаёт офисную мебель. Объём продаж: 1000 штук.'}],
    }


def offer(body='Предлагаем обсудить поставки офисных кресел.'):
    return {'subject': 'Коммерческое предложение', 'body': body,
            'reason': 'Гипотеза: расширение ассортимента.', 'source_ids': [42]}


@pytest.mark.stability
def test_valid_evidence_based_draft_passes_consistently():
    checked = AIService.validate_offer(offer(), card())
    assert checked['subject'] == 'Коммерческое предложение'
    assert checked['source_ids'] == [42]


@pytest.mark.parametrize('bad', [
    {'source_ids': [999]},
    {'subject': 'Тема\nBcc:someone@other.ru'},
    {'body': 'Мы гарантируем доставку завтра.'},
    {'body': 'У нас сейчас скидка 80 процентов.'},
    {'body': 'УТП: https://unknown.ru/offer'},
    {'body': 'Цитата: «Выручка выросла на 17 процентов».'},
    {'body': 'Продано 2000 штук.'},
])
def test_evidence_validator_rejects_fabricated_claims_and_injections(bad):
    proposed = offer()
    proposed.update(bad)
    with pytest.raises(ValueError):
        AIService.validate_offer(proposed, card())


def test_wrong_schema_cannot_be_accepted():
    proposed = offer()
    proposed['irrelevant_field'] = 'yes'
    with pytest.raises(ValueError):
        AIService.validate_offer(proposed, card())


@pytest.mark.stability
def test_gigachat_429_releases_reservation_and_never_counts_as_spent(isolated_store, fake_secrets):
    ai = AIService(isolated_store, fake_secrets)
    ai._authorize = Mock()
    ai._request = Mock(side_effect=RuntimeError('GigaChat: 429'))
    for _ in range(3):
        with pytest.raises(RuntimeError):
            ai._completion('GigaChat-2', 'system', 'task', {}, 'fit')
        assert ai._reserved_tokens == 0
    assert isolated_store.get_setting('ai.unconfirmed_tokens', 0) == 0


def test_unconfirmed_usage_is_preserved_after_network_timeout(isolated_store, fake_secrets):
    ai = AIService(isolated_store, fake_secrets)
    ai._authorize = Mock()
    ai._request = Mock(side_effect=httpx.ReadTimeout('Synthetic response timeout'))
    with pytest.raises(httpx.ReadTimeout):
        ai._completion('GigaChat-2', 'system', 'task', {}, 'offer')
    assert ai._reserved_tokens == 0
    assert isolated_store.get_setting('ai.unconfirmed_tokens', 0) > 0
    assert isolated_store.get_setting('ai.uncertain_requests') == 1
