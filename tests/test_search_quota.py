from unittest.mock import Mock

import pytest

from sales_manager.research.quota import fetch_quota


def test_tavily_remaining_uses_both_plan_and_key_without_paygo():
    request = Mock(return_value={'key': {'limit': 1000, 'usage': 100},
                                 'account': {'plan_limit': 1000, 'plan_usage': 800,
                                             'paygo_limit': 9999, 'paygo_usage': 0}})
    assert fetch_quota('tavily', 'private-key', request) == 200
    assert request.call_args.args == ('GET', 'https://api.tavily.com/usage')
    assert request.call_args.kwargs['headers']['Authorization'] == 'Bearer private-key'
    request.return_value['key']['usage'] = 950
    assert fetch_quota('tavily', 'private-key', request) == 50
    request.return_value['key']['usage'] = 1001
    assert fetch_quota('tavily', 'private-key', request) == 0


def test_firecrawl_uses_server_remaining_without_subtracting_local_usage():
    request = Mock(return_value={'success': True, 'data': {'remainingCredits': 87}})
    assert fetch_quota('firecrawl', 'private-key', request) == 87
    assert request.call_args.args == ('GET', 'https://api.firecrawl.dev/v2/team/credit-usage')


@pytest.mark.parametrize('value', [None, True, -1, '100', float('nan'), float('inf')])
def test_invalid_balance_is_unknown_not_fabricated(value):
    request = Mock(return_value={'success': True, 'data': {'remainingCredits': value}})
    assert fetch_quota('firecrawl', 'key', request) is None


def test_unsupported_or_keyless_quota_never_issues_request():
    request = Mock()
    assert fetch_quota('serper', 'key', request) is None
    assert fetch_quota('firecrawl', '', request) is None
    request.assert_not_called()


def test_partial_tavily_account_is_unknown():
    request = Mock(return_value={'key': {'limit': 1000, 'usage': 10}})
    assert fetch_quota('tavily', 'key', request) is None


@pytest.mark.parametrize('balance', [None, 0, 980])
def test_unproven_billing_blocks_source_regardless_of_quota(tmp_path, balance):
    from sales_manager.research.service import ResearchService
    from sales_manager.research.providers import ProviderError
    from sales_manager.storage import Store
    store = Store(tmp_path / 'quota.sqlite3')
    keys = {'tavily_key': 'controlled-secret'}
    service = ResearchService(store, Mock(get=lambda name, default='': keys.get(name, default)))
    store.set_setting('search.tavily_enabled', True)
    store.set_setting('search.free_confirmed', True)
    # A previous check, even if it reported available quota, is not proof
    # that the provider cannot bill the account on the next request.
    service._record_status('tavily')
    state = store.get_setting('search.tavily_status')
    state['quota'] = balance
    store.set_setting('search.tavily_status', state)
    service._json = Mock()
    # Only a non-search account inspection is permitted before a free billing
    # mode is proved; the old quota field/checkbox remains insufficient.
    service._access_request = Mock(return_value={
        'key': {'limit': 1000, 'usage': 0},
        'account': {'plan_limit': 1000, 'plan_usage': 0,
                    'paygo_limit': 1000, 'paygo_usage': 0}})
    with pytest.raises(ProviderError, match='бесплатный тариф'):
        service.test_provider('tavily')
    state = service.provider_status('tavily')
    assert state['status'] == 'Отключён: оплата не проверена'
    assert state['quota'] is None
    service._json.assert_not_called()
    service._access_request.assert_called_once()
    assert 'tavily' not in service._providers()
    assert 'controlled-secret' not in str(store.query('SELECT * FROM settings'))
    keys['tavily_key'] = 'rotated-secret'
    assert service.provider_status('tavily')['quota'] is None
    store.close()
