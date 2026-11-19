import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sales_manager.research import service as research_module
from sales_manager.research.access import verify_free_access
from sales_manager.research.providers import InvalidKeyError, NoSourcesError, RateLimitError
from sales_manager.research.registry import ADAPTERS
from sales_manager.research.service import ResearchService
from sales_manager.storage import Store
from sales_manager.ui.app import Window


@pytest.fixture
def research(tmp_path):
    store = Store(tmp_path / 'access.sqlite3')
    key = 'controlled-key'
    secrets = SimpleNamespace(get=lambda name, default='': key if name == 'tavily_key' else default,
                              warnings=[])
    for name in ADAPTERS:
        store.set_setting(f'search.{name}_enabled', name == 'tavily')
    account = {'account': {'current_plan': 'Researcher', 'plan_limit': 100, 'plan_usage': 0,
                           'paygo_limit': 0, 'paygo_usage': 0}, 'key': {'limit': 100, 'usage': 0}}
    proof = verify_free_access('tavily', key, Mock(return_value=account))
    proof.update(checked_at=time.time() - 601, expires_at=time.time() - 1)
    store.set_setting('search.tavily_free_access', proof)
    yield ResearchService(store, secrets)
    store.close()


@pytest.mark.parametrize('kind', ['market', 'companies'])
@pytest.mark.parametrize('error_class, reason', [
    (InvalidKeyError, 'Ключ неверный или отозван'),
    (RateLimitError, 'Временное ограничение запросов'),
])
def test_refresh_failure_explains_why_no_source_can_run(research, monkeypatch, kind, error_class, reason):
    request = Mock(side_effect=error_class(reason))
    monkeypatch.setattr(research_module, 'json_http', request)
    if kind == 'market':
        with pytest.raises(NoSourcesError, match=f'Tavily: {reason}'):
            research.research_market(['Россия'])
    else:
        product = research.store.ensure_product('кофе')
        with pytest.raises(NoSourcesError, match=f'Tavily: {reason}'):
            research.find_companies(product['id'], ['Россия'])

    assert request.call_count == 1
    assert request.call_args.args == ('GET', 'https://api.tavily.com/usage')
    assert research.store.get_setting('search.tavily_free_access') == {}
    assert not research.store.query('SELECT id FROM job_runs')


def test_refresh_failure_leaves_available_source_running(research, monkeypatch):
    research.store.set_setting('search.rss_enabled', True)
    feed = Mock(last_errors={})
    feed.discover.return_value = []
    monkeypatch.setattr(research_module, 'RSSProvider', lambda *args: feed)
    request = Mock(side_effect=InvalidKeyError('Ключ неверный или отозван'))
    monkeypatch.setattr(research_module, 'json_http', request)

    result = research.research_market(['Россия'])

    assert result['successful_sources'] == ['rss']
    request.assert_called_once()
    feed.discover.assert_called_once()


def test_search_retry_in_ui_preserves_saved_provider_diagnostic(research, monkeypatch, qtbot):
    request = Mock(side_effect=InvalidKeyError('Ключ неверный или отозван'))
    monkeypatch.setattr(research_module, 'json_http', request)
    window = Window(store=research.store, secrets=research.secrets)
    qtbot.addWidget(window)
    window.timer.stop()
    window.startup_mail_timer.stop()

    window.start_market()
    qtbot.waitUntil(lambda: not window.workers, timeout=5000)
    assert 'Tavily: Ключ неверный или отозван' in window.action_notice.text()

    window.clear_guide()
    window.start_market()
    assert 'Tavily: Ключ неверный или отозван' in window.action_notice.text()
    assert not window.workers
    request.assert_called_once()


@pytest.mark.parametrize('paygo', [0, 10])
def test_transient_tavily_access_retries_after_cooldown_and_rechecks_free_mode(research, monkeypatch, paygo):
    account = {'account': {'current_plan': 'Researcher', 'plan_limit': 100, 'plan_usage': 0,
                           'paygo_limit': paygo, 'paygo_usage': 0}, 'key': {'limit': 100, 'usage': 0}}
    request = Mock(side_effect=[RateLimitError('Временное ограничение запросов', retry_after=60)] + [account] * 3)
    search = Mock(return_value={'results': []})
    monkeypatch.setattr(research_module, 'json_http', request)
    monkeypatch.setattr(research_module, 'CATEGORIES', {'Напитки': ['кофе']})
    monkeypatch.setattr(research, '_json', search)

    for _ in range(2):
        with pytest.raises(NoSourcesError, match='Временное ограничение запросов'):
            research.research_market(['Россия'])
    request.assert_called_once()
    search.assert_not_called()
    assert research.store.get_setting('search.tavily_free_access') == {}
    assert 'tavily' not in research.available_sources('market')
    assert 'tavily' in research.configured_sources('market')
    status = research.store.get_setting('search.tavily_status')
    research.store.set_setting('search.tavily_status', {**status, 'retry_until': time.time() - 1})

    if paygo:
        with pytest.raises(NoSourcesError, match='Pay-as-you-go'):
            research.research_market(['Россия'])
        search.assert_not_called()
    else:
        assert research.research_market(['Россия'])['successful_sources'] == ['tavily']
        assert search.call_args.args[:2] == ('POST', 'https://api.tavily.com/search')
    assert all(call.args == ('GET', 'https://api.tavily.com/usage') for call in request.call_args_list)


def test_replacement_tavily_key_cannot_inherit_account_retry(research, monkeypatch):
    request = Mock(side_effect=RateLimitError('Временное ограничение запросов'))
    monkeypatch.setattr(research_module, 'json_http', request)
    with pytest.raises(NoSourcesError, match='Временное ограничение'):
        research.research_market(['Россия'])
    status = research.store.get_setting('search.tavily_status')
    research.store.set_setting('search.tavily_status', {**status, 'retry_until': time.time() - 1})
    monkeypatch.setattr(research.secrets, 'get',
                        lambda name, default='': 'replacement-key' if name == 'tavily_key' else default)

    assert 'tavily' not in research.configured_sources('market')
    with pytest.raises(NoSourcesError, match='Нет доступных источников'):
        research.research_market(['Россия'])
    request.assert_called_once()


@pytest.mark.parametrize('status, reason, marker, should_retry', [
    ('Временное ограничение', 'Ограничение запросов', {}, True),
    ('Недоступен', 'Сервер временно недоступен. Найденное сохранено; используем другие источники.', {}, True),
    ('Недоступен', 'Источник временно недоступен или не прошёл проверку TLS.', {}, True),
    ('Недоступен', 'Доступ запрещён. Проверьте права ключа и ограничения сервиса.', {}, False),
    ('Временное ограничение', 'Ограничение запросов', {'retry_access': False}, False),
])
def test_saved_legacy_tavily_failure_retries_only_identifiable_transient_checks(
        research, monkeypatch, status, reason, marker, should_retry):
    old_proof = research.store.get_setting('search.tavily_free_access')
    research.store.set_setting('search.tavily_free_access', {})
    saved_status = {'status': status, 'message': reason, 'checked_at': 'previous-version',
                    'key_hash': old_proof['key_hash'], 'access_verification': True,
                    'retry_until': time.time() + 60, **marker}
    research.store.set_setting('search.tavily_status', saved_status)
    account = {'account': {'current_plan': 'Researcher', 'plan_limit': 100, 'plan_usage': 0,
                           'paygo_limit': 0, 'paygo_usage': 0}, 'key': {'limit': 100, 'usage': 0}}
    request = Mock(return_value=account)
    search = Mock(return_value={'results': []})
    monkeypatch.setattr(research_module, 'json_http', request)
    monkeypatch.setattr(research_module, 'CATEGORIES', {'Напитки': ['кофе']})
    monkeypatch.setattr(research, '_json', search)

    assert ('tavily' in research.configured_sources('market')) == should_retry
    with pytest.raises(NoSourcesError):
        research.research_market(['Россия'])
    request.assert_not_called()
    search.assert_not_called()
    research.store.set_setting('search.tavily_status', {**saved_status, 'retry_until': time.time() - 1})

    if should_retry:
        assert research.research_market(['Россия'])['successful_sources'] == ['tavily']
        assert request.call_args.args == ('GET', 'https://api.tavily.com/usage')
        assert search.call_args.args[:2] == ('POST', 'https://api.tavily.com/search')
    else:
        with pytest.raises(NoSourcesError):
            research.research_market(['Россия'])
        request.assert_not_called()
        search.assert_not_called()
