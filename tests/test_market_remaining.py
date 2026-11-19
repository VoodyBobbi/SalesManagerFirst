from datetime import datetime, timezone
from unittest.mock import Mock, patch

from sales_manager.research.market import import_statistics
from sales_manager.research.normalization import branch_identity, normalize_product, product_candidates
from sales_manager.research.providers import ComtradeProvider, SearchResult
from sales_manager.research.providers import ProviderError
from sales_manager.research.service import ResearchService
from sales_manager.storage import Store


def test_explicit_type_precedes_brand_and_branches_keep_models(tmp_path):
    assert normalize_product('Пылесос Xiaomi') == 'пылесос'
    assert 'смартфон' not in product_candidates('Продажи пылесосов Xiaomi')
    assert 'смартфон' not in product_candidates('Продажи пылесосов Xiaomi Redmi')
    assert normalize_product('Пылесос Xiaomi Redmi') == 'пылесос'
    assert normalize_product('iPhone 15') == normalize_product('Samsung Galaxy S24') == 'смартфон'
    store = Store(tmp_path / 'branches.sqlite')
    old = store.ensure_product('смартфон', 'смартфон')
    apple = store.ensure_product('iPhone 15', branch_identity('iPhone 15'))
    samsung = store.ensure_product('Samsung Galaxy S24', branch_identity('Samsung Galaxy S24'))
    assert len({old['id'], apple['id'], samsung['id']}) == 3
    assert store.one('SELECT name FROM products WHERE id=?', (old['id'],))['name'] == 'смартфон'
    service = ResearchService(store, Mock(get=lambda *args: ''))
    with patch.object(service, '_providers', return_value={'gdelt': Mock()}), patch.object(service, '_run', return_value={}) as run:
        service.find_companies(apple['id'], ['Россия'])
    queries = [task['query'] for task in run.call_args.args[3]]
    assert any('iPhone 15' in query for query in queries)
    store.close()


def test_distinct_sellers_and_verbatim_reprints(service_for_market):
    service = service_for_market
    stamp = datetime.now(timezone.utc).isoformat()
    first = f'В России за {stamp[:7]} продавец Альфа продал 100 смартфонов.'
    second = f'В России за {stamp[:7]} продавец Бета продал 100 смартфонов.'
    for url, text in [('https://a.example/a', first), ('https://b.example/b', first), ('https://c.example/c', second)]:
        service._market_evidence(SearchResult(url, 'Отчёт', text, 'Россия', stamp,
            {'official_statistics': True}), lambda _: None)
    assert len(service.store.query('SELECT * FROM market_observations')) == 2
    assert len(service.store.query('SELECT * FROM market_observation_sources')) == 3


def test_old_model_workspace_remains_and_market_uses_generic_kind(service_for_market):
    service = service_for_market
    old = service.store.ensure_product('iPhone 15', 'смартфон')
    stamp = datetime.now(timezone.utc).isoformat()
    text = f'В России за {stamp[:7]} продано 100 смартфонов.'
    service._market_evidence(SearchResult('https://example.org/report', 'Отчёт', text, 'Россия', stamp,
        {'official_statistics': True}), lambda _: None)
    result = service.market_ranking(['Россия'])['rows']
    assert len(result) == 1 and result[0]['name'] == 'смартфон'
    assert result[0]['id'] != old['id']
    assert service.store.one('SELECT name FROM products WHERE id=?', (old['id'],))['name'] == 'iPhone 15'


def test_sellers_in_preceding_article_context_are_distinct_but_reprints_merge(service_for_market):
    service = service_for_market
    stamp = datetime.now(timezone.utc).isoformat()
    result = f'В России за {stamp[:7]} продано 100 смартфонов.'
    articles = [('https://a.example/a', f'Продавец Альфа сообщил результаты. {result}'),
                ('https://b.example/b', f'Продавец Альфа сообщил результаты. {result}'),
                ('https://c.example/c', f'Продавец Бета сообщил результаты. {result}')]
    for url, text in articles:
        service._market_evidence(SearchResult(url, 'Отчёт', text, 'Россия', stamp,
            {'official_statistics': True}), lambda _: None)
    assert len(service.store.query('SELECT * FROM market_observations')) == 2
    assert len(service.store.query('SELECT * FROM market_observation_sources')) == 3
    assert len(service.market_ranking(['Россия'])['rows'][0]['measurements']) == 2


import pytest

@pytest.fixture
def service_for_market(tmp_path):
    store = Store(tmp_path / 'market.sqlite')
    yield ResearchService(store, Mock(get=lambda *args: ''))
    store.close()


@pytest.mark.parametrize('name', ['gdelt', 'web'])
def test_disabled_source_no_longer_blocks_saved_queue(service_for_market, name):
    service = service_for_market
    task = {'phase': 'search', 'provider': name, 'query': 'кофе', 'country': 'Россия', 'market': True}
    provider = Mock(search=Mock(side_effect=__import__('sales_manager.research.service', fromlist=['Paused']).Paused('Закрытие приложения')))
    if name == 'web':
        task['route'] = ['firecrawl']
    first = service._run('market', None, ['Россия'], [task], {'firecrawl' if name == 'web' else name: provider}, lambda _: None, None)
    assert first['status'] == 'interrupted'
    second = service._run('market', None, ['Россия'], [], {}, lambda _: None, None)
    assert second['run_id'] == first['run_id'] and second['status'] == 'complete'
    saved = service.store.one('SELECT status,error FROM job_queue')
    assert saved['status'] == 'done' and saved['error']
    assert provider.search.call_count == 1


def test_comtrade_runs_and_is_saved_separately_from_current_demand(service_for_market):
    service = service_for_market
    year = str(datetime.now(timezone.utc).year - 1)
    row = {'reporterCode': 643, 'period': year, 'cmdCode': '0901', 'flowCode': 'M',
           'partnerCode': 0, 'primaryValue': 100.0, 'netWgt': 20.0}
    provider = ComtradeProvider(Mock(return_value={'data': [row]}), year=year)
    payload = {'phase':'search','provider':'comtrade','query':'кофе','country':'Россия','market':True}
    result = service._run('imports', None, ['Россия'], [payload], {'comtrade':provider}, lambda _:None, None)
    assert result['status'] == 'complete'
    stats = import_statistics(service.store, ['Россия'])
    assert {(r['period'], r['unit'], r['value']) for r in stats} == {(year, 'USD', 100), (year, 'kg', 20)}
    assert service.market_ranking(['Россия'])['rows'] == []


def test_paused_comtrade_query_retries_when_market_is_resumed(service_for_market):
    service = service_for_market
    provider = Mock(search=Mock(side_effect=__import__('sales_manager.research.service', fromlist=['Paused']).Paused('Закрытие приложения')))
    payload = {'phase': 'search', 'provider': 'comtrade', 'query': 'кофе', 'country': 'Россия', 'market': True}
    first = service._run('market', None, ['Россия'], [payload], {'comtrade': provider}, lambda _: None, None)
    assert first['status'] == 'interrupted'
    provider.search.reset_mock()
    provider.search.side_effect = None
    provider.search.return_value = []
    with patch.object(service, '_providers', return_value={'comtrade': provider}):
        second = service.research_market(['Россия'])
    assert second['run_id'] == first['run_id'] and second['status'] == 'complete'
    provider.search.assert_not_called()  # Import tasks are retired, not retried by standard market research.
    row = service.store.one("SELECT status,error FROM job_queue WHERE run_id=? AND json_extract(payload,'$.query')='кофе'", (first['run_id'],))
    assert row['status'] == 'done' and row['error']
