"""Market pass completion and article ingestion with isolated network responses."""
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import pytest

from sales_manager.research.providers import ProviderError, QuotaError, RateLimitError, SearchResult
from sales_manager.research.service import Paused, ResearchService
from sales_manager.storage import Store


@pytest.fixture
def service(tmp_path):
    store = Store(tmp_path / 'market.sqlite3')
    research = ResearchService(store, Mock(get=lambda *args: ''))
    yield research
    store.close()


def task(provider):
    return {'phase': 'search', 'provider': provider, 'query': 'кофе', 'country': 'Россия', 'market': True}


def test_one_unavailable_source_finishes_and_keeps_details_in_diagnostics(service):
    good = Mock(search=Mock(return_value=[]))
    bad = Mock(search=Mock(side_effect=ProviderError('Сервер недоступен')))
    tasks = [task('good'), task('bad')]
    first = service._run('market', None, ['Россия'], tasks, {'good': good, 'bad': bad}, lambda _: None, None)
    assert first['status'] == 'complete'
    assert first['reason'] == 'sources_exhausted'
    assert 'BAD:' not in first['message']
    assert any('Сервер недоступен' in row['message'] for row in service.store.query('SELECT message FROM job_diagnostics'))
    assert 'Найдено товаров: 0' in first['message']
    assert first['successful_sources'] == ['good']
    assert first['unavailable_sources'] == ['bad']
    assert service.store.one("SELECT COUNT(*) n FROM job_queue WHERE status='pending'")['n'] == 0
    service.store.execute("DELETE FROM settings WHERE key='search.bad_status'")
    bad.search.side_effect = None
    bad.search.return_value = []
    second = service._run('market', None, ['Россия'], tasks, {'good': good, 'bad': bad}, lambda _: None, None)
    assert second['run_id'] != first['run_id']
    assert second['status'] == 'complete'
    assert good.search.call_count == 1
    assert bad.search.call_count == 2


def test_all_sources_unavailable_complete_without_fake_goods(service):
    bad = Mock(search=Mock(side_effect=ProviderError('Недоступен')))
    result = service._run('market', None, ['Россия'], [task('bad')], {'bad': bad}, lambda _: None, None)
    assert result['status'] == 'complete' and result['reason'] == 'sources_exhausted'
    assert service.store.one('SELECT status FROM job_queue')['status'] == 'done'
    assert result['count'] == 0
    assert not service.store.query('SELECT * FROM products')
    saved = service.store.one('SELECT reason,message FROM job_runs')
    assert saved['reason'] == 'sources_exhausted'
    assert saved['message'] == result['message']
    assert 'BAD:' not in saved['message']
    assert any('Недоступен' in row['message'] for row in service.store.query('SELECT message FROM job_diagnostics'))


def test_request_batches_continue_automatically_and_summary_is_persisted(service):
    service.store.set_setting('search.request_limit', 1)
    def query(*args, **kwargs):
        service._checkpoint()
        service._requests += 1
        return []
    provider = Mock(search=query)
    tasks = [task('good'), {**task('good'), 'query': 'посуда'}]
    result = service._run('market', None, ['Россия'], tasks, {'good': provider}, lambda _: None, None)
    assert result['status'] == 'complete'
    assert result['requests'] == 2
    assert 'Использовано источников:' in result['message']
    assert 'Найдено товаров: 0' in result['message']
    assert service.store.one('SELECT message FROM job_runs')['message'] == result['message']
    assert service.store.one("SELECT COUNT(*) n FROM job_queue WHERE status='done'")['n'] == 2


def test_failed_rss_is_skipped_in_current_run_if_other_source_answered(service):
    rss = Mock(discover=Mock(side_effect=ProviderError('RSS не отвечает')))
    good = Mock(search=Mock(return_value=[]))
    tasks = [{**task('rss'), 'discover': True}, task('good')]
    first = service._run('market', None, ['Россия'], tasks, {'rss': rss, 'good': good}, lambda _: None, None)
    assert first['status'] == 'complete'
    assert 'RSS' not in first['message']
    assert any('RSS' in row['message'] for row in service.store.query('SELECT message FROM job_diagnostics'))
    assert rss.discover.call_count == 1 and good.search.call_count == 1
    assert not service.store.query("SELECT id FROM job_queue WHERE status='pending'")


def test_resumed_web_query_reconsiders_an_earlier_provider_after_key_replacement(service):
    keys = {'tavily_key': 'old-key', 'serper_key': 'same-key'}
    service.secrets.get = Mock(side_effect=lambda key, default='': keys.get(key, default))
    tavily = Mock(search=Mock(side_effect=QuotaError('Квота закончилась')))
    serper = Mock(search=Mock(side_effect=Paused('Достигнут лимит запросов.')))
    payload = {**task('web'), 'route': ['tavily', 'serper'], 'next_provider': 0}
    service.billing_blocked_sources = lambda: []  # Mock providers have no billing account.
    first = service._run('market', None, ['Россия'], [payload], {'tavily': tavily, 'serper': serper}, lambda _: None, None)
    assert first['status'] == 'interrupted'
    keys['tavily_key'] = 'new-key'
    tavily.search.side_effect = None
    tavily.search.return_value = []
    serper.search.side_effect = ProviderError('Serper недоступен')
    second = service._run('market', None, ['Россия'], [payload], {'tavily': tavily, 'serper': serper}, lambda _: None, None)
    assert second['run_id'] == first['run_id']
    assert second['status'] == 'complete'
    # Replaced credentials reopen an earlier failed provider in the saved route.
    assert tavily.search.call_count >= 2
    assert 'tavily' in second['successful_sources']


def test_short_rate_limit_has_one_retry_and_long_delay_is_deferred(service):
    provider = Mock(search=Mock(side_effect=[RateLimitError('Пауза', retry_after=.001), []]))
    assert service._search('news', provider, 'кофе', 'Россия') == []
    assert provider.search.call_count == 2
    provider.search.side_effect = RateLimitError('Пауза', retry_after=60)
    with pytest.raises(RateLimitError):
        service._search('news', provider, 'посуда', 'Россия')
    assert provider.search.call_count == 3


def test_rss_article_body_provides_measurement_and_second_source_is_preserved(service):
    stamp = datetime.now(timezone.utc).isoformat()
    period = stamp[:7]
    body = f'В России за {period} продано 15000 смартфонов.'
    def page(url, country):
        source_id = service.store.put_source(url, 'Отчёт о рынке', body, stamp)
        return {'market_text': body, 'source_id': source_id}
    with patch.object(service, '_page', side_effect=page) as fetch:
        for url in ('https://one.example/report', 'https://two.example/report'):
            service._market_evidence(SearchResult(url, 'Рынок смартфонов', 'Подробности в статье.',
                'Россия', stamp, {'provider': 'rss'}), lambda _: None)
    assert fetch.call_count == 2
    observations = service.store.query('SELECT * FROM market_observations')
    assert len(observations) == 1
    assert observations[0]['metric'] == 'sales' and observations[0]['value'] == 15000
    assert len(service.store.query('SELECT * FROM market_observation_sources')) == 2
    ranking = service.market_ranking(['Россия'])
    assert ranking['rows'][0]['count'] == 2
    assert len(ranking['rows'][0]['source_ids']) == 2
    assert not service.store.query("SELECT * FROM usage_log WHERE provider='gigachat'")


def test_rss_partial_failure_finishes_without_repeating_successful_feeds(service):
    rss = Mock(discover=Mock(return_value=[]), last_errors={'https://bad.example/rss': 'HTTP 503'})
    payload = {**task('rss'), 'discover': True, 'query': '*'}
    result = service._run('market', None, ['Россия'], [payload], {'rss': rss}, lambda _: None, None)
    assert result['status'] == 'complete'
    assert 'RSS' not in result['message']
    assert any('RSS' in row['message'] for row in service.store.query('SELECT message FROM job_diagnostics'))
    rss.discover.assert_called_once()
    assert service.store.one('SELECT status FROM job_queue')['status'] == 'done'
    again = service._run('market', None, ['Россия'], [payload], {'rss': rss}, lambda _: None, None)
    assert again['status'] == 'complete'
    assert rss.discover.call_count == 2  # A new explicit research run may retry.


def test_market_scheduler_uses_feed_discovery_not_encyclopedia_categories(service):
    providers = {name: Mock() for name in ('rss', 'mediawiki', 'wikidata', 'comtrade', 'catalog')}
    with patch.object(service, '_providers', return_value=providers), patch.object(service, '_run', return_value={}) as run:
        service.research_market(['Россия'])
    tasks = run.call_args.args[3]
    assert tasks[0]['provider'] == 'rss' and tasks[0]['discover']
    assert not any(t['provider'] == 'comtrade' for t in tasks)
    assert {t['provider'] for t in tasks} == {'rss'}


def test_public_basis_displays_own_factor_instead_of_generic_intro_or_neighbour_number():
    from sales_manager.research.market import _public_basis
    item = {'name': 'ювелирные украшения', 'period': '', 'published_at': '2026-11-13', 'country': 'Россия',
            'excerpt': 'Особенно заметно увеличился спрос на товары для дома, украшения и косметические наборы.\n'
                       'Заказы мужской одежды увеличились на 33,4%, женских украшений — в 2,6 раза.'}
    result = _public_basis(item)
    assert '2,6 раза' in result
    assert '33,4' not in result
    assert 'косметические наборы' not in result


def test_market_row_prefers_specific_saved_factor_to_generic_saved_intro(service):
    text = ('В России особенно заметно увеличился спрос на украшения и косметические наборы. '
            'Заказы женских украшений выросли в 2,6 раза.')
    stamp = datetime.now(timezone.utc).isoformat()
    service._market_evidence(SearchResult('https://report.example/retail', 'Отчёт о покупках', text,
        'Россия', stamp, {'official_statistics': True}), lambda _: None)
    jewelry = next(row for row in service.market_ranking(['Россия'])['rows'] if row['name'] == 'ювелирные украшения')
    assert '2,6 раза' in jewelry['basis']
    assert 'косметические наборы' not in jewelry['basis']


@pytest.mark.parametrize('text', [
    '«Арнест» приобрел российские активы Reckitt Benckiser в сегменте бытовой химии.',
    'В России компания купила завод моющих средств за 100 млн рублей в 2026 году.',
    'Группа закрыла сделку по покупке российского подразделения, выпускающего моющие средства.',
])
def test_purchase_of_business_assets_is_not_demand_for_its_products(service, text):
    stamp = datetime.now(timezone.utc).isoformat()
    service._market_evidence(SearchResult('https://report.example/deal', 'Сделка с активами', text,
        'Россия', stamp, {'official_statistics': True}), lambda _: None)
    assert not service.market_ranking(['Россия'])['rows']
    assert not service.store.query('SELECT * FROM market_observations')


@pytest.mark.parametrize('text', [
    'В России за 2026 год россияне купили на 19% новых ноутбуков, чем годом ранее.',
    'В России за 2026 год ноутбуки покупают на 19% хуже, чем в 2025 году.',
    'В России продажи ноутбуков в 2026 году сильно просели.',
])
def test_decline_or_incomplete_comparison_is_not_a_positive_laptop_signal(service, text):
    stamp = datetime.now(timezone.utc).isoformat()
    service._market_evidence(SearchResult('https://report.example/laptops', 'Ноутбуки в России', text,
        'Россия', stamp, {'official_statistics': True}), lambda _: None)
    assert not service.market_ranking(['Россия'])['rows']


def test_actual_product_purchase_survives_corporate_transaction_filter(service):
    stamp = datetime.now(timezone.utc).isoformat()
    text = f'В России за {stamp[:7]} компания приобрела 500 ноутбуков для сотрудников.'
    service._market_evidence(SearchResult('https://report.example/purchase', 'Закупка ноутбуков', text,
        'Россия', stamp, {'official_statistics': True}), lambda _: None)
    rows = service.market_ranking(['Россия'])['rows']
    assert len(rows) == 1 and rows[0]['name'] == 'ноутбук'
    assert rows[0]['measurements'][0]['value'] == 500
