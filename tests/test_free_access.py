"""Billing proof is independent of a key, checkbox or a positive credit balance."""
import json
import sqlite3
import time
from unittest.mock import Mock

import pytest

from sales_manager.research.access import free_access_allowed, free_access_valid, verify_free_access
from sales_manager.research.providers import InvalidKeyError, RateLimitError
from sales_manager.storage import Store, now
from sales_manager.storage.migrations import MIGRATIONS, migrate


def free_tavily(**changes):
    response = {'account': {'current_plan': 'Researcher', 'plan_limit': 1000,
                            'plan_usage': 100, 'paygo_limit': 0, 'paygo_usage': 0},
                'key': {'limit': 500, 'usage': 50}}
    response['account'].update(changes)
    return response


def test_tavily_free_plan_with_disabled_paygo_is_actually_enabled():
    request = Mock(return_value=free_tavily())
    proof = verify_free_access('tavily', 'private-key', request)
    assert proof['allowed'] is True
    assert proof['key_valid'] is True
    assert proof['remaining'] == 450
    assert free_access_allowed('tavily', 'private-key', proof)
    assert free_access_valid(proof, 'private-key')
    request.assert_called_once_with('GET', 'https://api.tavily.com/usage',
                                    headers={'Authorization': 'Bearer private-key'})
    assert 'private-key' not in json.dumps(proof)


def test_documented_unlimited_tavily_key_is_limited_by_free_account():
    response = free_tavily()
    response['key']['limit'] = None
    proof = verify_free_access('tavily', 'key', Mock(return_value=response))
    assert proof['allowed'] is True
    assert proof['remaining'] == 900


@pytest.mark.parametrize('account', [
    {'current_plan': 'Bootstrap'}, {'current_plan': 'Pay as you go'},
    {'current_plan': None}, {'paygo_limit': 1}, {'paygo_limit': None},
    {'paygo_limit': False}, {'paygo_usage': 1}, {'paygo_usage': None},
    {'plan_limit': None}, {'plan_usage': '0'}, {'plan_usage': -1},
    {'plan_usage': float('nan')}, {'plan_usage': float('inf')},
    {'plan_usage': 1000}, {'plan_usage': 1001},
])
def test_remaining_credits_do_not_override_missing_free_billing_proof(account):
    proof = verify_free_access('tavily', 'key', Mock(return_value=free_tavily(**account)))
    assert not proof['allowed']
    assert not free_access_valid(proof, 'key')
    assert proof['reason']


@pytest.mark.parametrize('key_data', [{'usage': 0}, {'limit': 100, 'usage': None},
                                     {'limit': 100, 'usage': 100}, {'limit': True, 'usage': 0}])
def test_invalid_or_exhausted_key_allowance_never_allows_search(key_data):
    response = free_tavily()
    response['key'] = key_data
    assert not verify_free_access('tavily', 'key', Mock(return_value=response))['allowed']


@pytest.mark.parametrize('response', [None, [], {}, {'account': []}, {'account': {}, 'key': ''}])
def test_unexpected_account_schema_fails_closed(response):
    assert not verify_free_access('tavily', 'key', Mock(return_value=response))['allowed']


def test_firecrawl_balance_verifies_key_but_does_not_prove_free_plan():
    request = Mock(return_value={'success': True, 'data': {'remainingCredits': 500000, 'planCredits': 500000}})
    proof = verify_free_access('firecrawl', 'private-key', request)
    assert proof['key_valid'] is True
    assert proof['reported_balance'] == 500000
    assert proof['remaining'] is None
    assert proof['allowed'] is False
    assert 'Pay-as-you-go' in proof['reason']
    assert request.call_args.args[:2] == ('GET', 'https://api.firecrawl.dev/v2/team/credit-usage')


@pytest.mark.parametrize('name', ['exa', 'jina', 'serper', 'unknown'])
def test_unknown_free_mode_does_not_make_a_chargeable_probe(name):
    request = Mock()
    proof = verify_free_access(name, 'private-key', request)
    assert proof['allowed'] is False and proof['key_valid'] is None
    assert proof['reason']
    request.assert_not_called()


@pytest.mark.parametrize('name', ['tavily', 'firecrawl', 'exa', 'jina', 'serper'])
def test_empty_key_does_not_call_account_or_search(name):
    request = Mock()
    assert not verify_free_access(name, '', request)['allowed']
    request.assert_not_called()


@pytest.mark.parametrize('error', [InvalidKeyError('Invalid'), RateLimitError('429')])
def test_authentication_and_rate_limit_errors_remain_distinct(error):
    with pytest.raises(type(error)):
        verify_free_access('tavily', 'key', Mock(side_effect=error))


@pytest.mark.parametrize('changes', [
    {'allowed': 1}, {'provider': 'exa'}, {'proof_version': 'old'},
    {'expires_at': 0}, {'expires_at': float('inf')}, {'expires_at': True},
    {'expires_at': time.time() + 99999}, {'checked_at': None},
    {'checked_at': time.time() - 700}, {'checked_at': time.time() + 100},
    {'remaining': 0}, {'remaining': None}, {'remaining': True}, {'remaining': float('nan')},
])
def test_saved_proof_cannot_survive_expiry_or_malformed_fields(changes):
    proof = verify_free_access('tavily', 'key', Mock(return_value=free_tavily()))
    proof.update(changes)
    assert not free_access_valid(proof, 'key')


def test_replacing_key_or_provider_invalidates_saved_proof():
    proof = verify_free_access('tavily', 'first', Mock(return_value=free_tavily()))
    assert not free_access_valid(proof, 'second')
    assert not free_access_allowed('firecrawl', 'first', proof)
    assert not free_access_valid(None, 'first')


def test_schema_four_upgrade_preserves_existing_research_and_user_data(tmp_path):
    path = tmp_path / 'old.sqlite3'
    connection = sqlite3.connect(path)
    for version in range(1, 5):
        connection.executescript(MIGRATIONS[version])
    connection.execute('PRAGMA user_version=4')
    connection.execute("INSERT INTO products(id,name,normalized,created_at) VALUES(1,'Товар','товар',?)", (now(),))
    connection.execute("INSERT INTO settings VALUES('sender.name','\"Пользователь\"')")
    connection.execute("INSERT INTO sources(id,url,text,hash,fetched_at) VALUES(1,'https://test.ru','Факты','hash',?)", (now(),))
    connection.execute("INSERT INTO market_observations(product_id,source_id,metric,unit,country,excerpt,event_key,created_at) VALUES(1,1,'imports','USD','Россия','Импорт','import',?)", (now(),))
    connection.execute("INSERT INTO job_runs(id,kind,status,created_at,updated_at) VALUES(1,'market','paused',?,?)", (now(), now()))
    connection.execute("INSERT INTO job_queue(run_id,task_key,payload,status) VALUES(1,'a','{}','pending')")
    connection.commit()
    migrate(connection)
    assert connection.execute('PRAGMA user_version').fetchone()[0] == 6
    assert connection.execute('SELECT name FROM products').fetchone()[0] == 'Товар'
    assert connection.execute('SELECT metric FROM market_observations').fetchone()[0] == 'imports'
    assert connection.execute('SELECT status FROM job_queue').fetchone()[0] == 'pending'
    assert json.loads(connection.execute('SELECT value FROM settings WHERE key=?', ('sender.name',)).fetchone()[0]) == 'Пользователь'
    connection.execute("INSERT INTO job_source_responses VALUES(1,'rss',?)", (now(),))
    connection.execute("INSERT INTO job_diagnostics(run_id,provider,message,created_at) VALUES(1,'gdelt','429',?)", (now(),))
    connection.commit()
    migrate(connection)
    connection.close()
    reopened = Store(path)
    try:
        assert reopened.one('SELECT provider FROM job_source_responses')['provider'] == 'rss'
        assert reopened.one('SELECT message FROM job_diagnostics')['message'] == '429'
        assert reopened.one('SELECT COUNT(*) n FROM market_observations')['n'] == 1
        with pytest.raises(sqlite3.IntegrityError):
            reopened.execute("INSERT INTO job_source_responses VALUES(1,'rss',?)", (now(),))
    finally:
        reopened.close()


def test_market_diagnostics_and_responses_are_scoped_to_real_runs(tmp_path):
    store = Store(tmp_path / 'new.sqlite3')
    try:
        with pytest.raises(sqlite3.IntegrityError):
            store.execute("INSERT INTO job_source_responses VALUES(999,'rss',?)", (now(),))
        with pytest.raises(sqlite3.IntegrityError):
            store.execute("INSERT INTO job_diagnostics(run_id,message,created_at) VALUES(999,'Error',?)", (now(),))
    finally:
        store.close()
