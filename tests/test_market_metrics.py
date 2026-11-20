"""Market rules exercised with saved source evidence, not preassigned outcomes."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest

from sales_manager.research.market import article_text, extract_observations, market_evidence, market_ranking
from sales_manager.research.providers import SearchResult
from sales_manager.research.safe_http import FetchError
from sales_manager.research.service import ResearchService
from sales_manager.storage import Store, now
from sales_manager.storage.migrations import MIGRATIONS, SCHEMA


@pytest.fixture
def service(tmp_path):
    store = Store(tmp_path / 'market.sqlite3')
    result = ResearchService(store, Mock(get=lambda name, default='': default))
    result._page = Mock(side_effect=FetchError('No page'))
    yield result
    store.close()


def ingest(service, text, url='https://source.example/report', country='Россия', published=''):
    service._market_evidence(SearchResult(url, text, '', country, published), lambda message: None)


def test_sales_quantity_is_saved_with_product_unit_period_market_source(service):
    ingest(service, 'В России за сентябрь 2026 года продажи смартфонов составили 500 штук.')
    row = service.store.one('SELECT o.*,p.name,s.url FROM market_observations o JOIN products p ON p.id=o.product_id JOIN sources s ON s.id=o.source_id')
    assert (row['name'], row['metric'], row['value'], row['unit'], row['period'], row['country']) == (
        'смартфон', 'sales', 500, 'units', '2026-09', 'Россия')
    assert row['url'] == 'https://source.example/report' and '500 штук' in row['excerpt']
    assert row['published_at'] == '' and row['measured'] == 1


def test_thousands_and_counted_product_do_not_capture_period_year(service):
    ingest(service, 'В России в 2026 году продано 10 000 смартфонов.')
    rows = service.store.query('SELECT value,unit FROM market_observations WHERE measured=1')
    assert rows == [{'value': 10000, 'unit': 'units'}]


def test_decimal_scale_and_explicit_unit():
    data = extract_observations('смартфон', 'В России за 2026 год продажи смартфонов составили 1,5 млн. штук.', 'Россия')
    assert len(data) == 1 and data[0]['value'] == 1500000 and data[0]['unit'] == 'units'


def test_iso_month_is_not_a_negative_demand_number():
    rows = extract_observations('смартфон', 'В России за 2026-11 продано 15 000 смартфонов.')
    assert len(rows) == 1 and rows[0]['value'] == 15000 and rows[0]['period'] == '2026-11'
    growth = extract_observations('смартфон', 'В России за 2026-11 продажи iPhone увеличились на 20%.')
    assert [(row['metric'], row['value']) for row in growth] == [('sales_growth', 20)]
    assert extract_observations('смартфон', 'В России за 2026-11 изменение продаж смартфонов: −10%.') == []


def test_previous_year_comparison_does_not_inherit_current_reporting_period():
    text = ('В России за январь — сентябрь 2026 года продажи телевизоров составили 5100000 штук. '
            'Для сравнения, годом ранее за тот же период в России было приобретено 4,7 млн телевизоров, то есть на 8,7% меньше.')
    rows = extract_observations('телевизор', text)
    assert [(row['value'], row['period']) for row in rows if row['measured']] == [(5100000, '2026-01/2026-09')]


def test_completed_purchases_are_measured_without_becoming_sales():
    text = ('Жители России увеличили траты на смартфоны. '
            'С января по сентябрь 2026 г. они купили в общей сложности 5,1 млн смартфонов.')
    rows = extract_observations('смартфон', text)
    assert [(row['metric'], row['value'], row['period']) for row in rows if row['measured']] == [
        ('purchases', 5100000, '2026-01/2026-09')]


def test_equipment_bundle_budget_is_not_server_purchase_volume(service):
    text = ('В России за ноябрь 2026 года закупка «Ростелекома» — это оборудование на 4,4 млрд руб. '
            'Речь идет о приобретении серверов, систем хранения данных (СХД) и коммутаторов.')
    rows = extract_observations('сервер', text)
    assert rows and all(not row['measured'] for row in rows)
    ingest(service, text)
    row = next(row for row in service.market_ranking(['Россия'])['rows'] if row['name'] == 'сервер')
    assert row['basis'].startswith('Закупка комплекта оборудования')
    assert '4 400' not in row['basis'] and not row['measurements']


@pytest.mark.parametrize('text', [
    'В России за 2026 год цены на смартфоны выросли на 20%.',
    'Продажи смартфонов в России за 2026 год снизились на 10%.',
    'Спрос на смартфоны упал на 15%.',
    'Прогноз: продажи смартфонов в России в 2026 году вырастут на 20%.',
])
def test_prices_declines_and_forecasts_never_become_positive_sales(service, text):
    ingest(service, text)
    assert service.store.query('SELECT * FROM market_observations') == []
    assert service.market_ranking(['Россия'])['rows'] == []


def test_growth_percentage_is_typed_growth_not_sold_units(service):
    ingest(service, 'Продажи смартфонов в России за 2026 год выросли на 20%.')
    row = service.store.one('SELECT * FROM market_observations')
    assert (row['metric'], row['unit'], row['value']) == ('sales_growth', 'percent', 20)


def test_number_is_associated_with_named_product():
    text = 'В России за 2026 год продажи смартфонов составили 500 штук, ноутбуков — 100 штук.'
    phones = extract_observations('смартфон', text, 'Россия')
    laptops = extract_observations('ноутбук', text, 'Россия')
    assert [(row['metric'], row['value']) for row in phones] == [('sales', 500)]
    assert [(row['metric'], row['value']) for row in laptops] == [('sales', 100)]


def test_actual_retail_gendered_categories_do_not_inherit_clothing_growth():
    text = ('В России за ноябрь 2026 года заказы мужской одежды увеличились на 33,4%, '
            'женской парфюмерии — на 47,2%, женских украшений — в 2,6 раза, '
            'мужской парфюмерии — на 53,2%.')
    rows = extract_observations('одежда', text)
    assert [(row['metric'], row['value']) for row in rows] == [('orders_growth', 33.4)]


def test_actual_sale_campaign_date_is_not_a_sales_quantity():
    text = ('В России в ноябре 2026 года распродажа 11.11 показала устойчивый интерес россиян '
            'к зарубежным брендам одежды, обуви, парфюмерии и косметики.')
    assert not [row for row in extract_observations('одежда', text) if row['measured']]


def test_article_inline_links_keep_one_measurement_sentence():
    html = '<article><p>В России за ноябрь 2026 года продажи <a href="/coffee">кофе</a> составили <b>500</b> штук.</p></article>'
    text = article_text(html)
    assert '\n' not in text
    assert extract_observations('кофе', text)[0]['value'] == 500


@pytest.mark.parametrize('text', [
    'В России в 2026 году просмотры карточек смартфонов выросли на 28%.',
    'Российские покупатели в 2026 году стали вдвое чаще присматривать смартфоны.',
])
def test_reported_buyer_interest_growth_is_indirect_not_sales(text):
    rows = extract_observations('смартфон', text)
    assert len(rows) == 1 and rows[0]['metric'] == 'public_signal'
    assert not rows[0]['measured'] and rows[0]['value'] is None


def test_foreign_market_in_fact_does_not_inherit_russia_from_article_context():
    text = 'Новости России. В Индии за 2015 год продажи смартфонов составили 1000 штук.'
    rows = extract_observations('смартфон', text, country='Россия')
    assert rows and rows[0]['country'] == 'Индия'


def test_legacy_wrong_typed_number_is_rechecked_without_deleting_history(service):
    text = ('В России за ноябрь 2026 года заказы мужской одежды увеличились на 33,4%, '
            'женской парфюмерии — на 47,2%.')
    ingest(service, text)
    valid = service.store.one('SELECT * FROM market_observations')
    service.store.execute('''INSERT INTO market_observations
        (product_id,source_id,metric,value,unit,period,country,excerpt,measured,event_key,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)''', (valid['product_id'], valid['source_id'], 'orders_growth', 47.2,
        'percent', valid['period'], 'Россия', text, 1, 'old-wrong-association', now()))
    row = next(row for row in service.market_ranking(['Россия'])['rows'] if row['id'] == valid['product_id'])
    assert [item['value'] for item in row['measurements']] == [33.4]
    assert service.store.one('SELECT COUNT(*) n FROM market_observations WHERE product_id=?', (valid['product_id'],))['n'] == 2


def test_actual_cdek_growth_factor_keeps_exact_public_fact_without_sales_quantity(service):
    text = ('В России за ноябрь 2026 года заказы на распродаже увеличились. '
            'Наибольший рост по сравнению с прошлогодней распродажей показали посуда и кухонные принадлежности — в 3,1 раза.')
    ingest(service, text)
    rows = service.market_ranking(['Россия'])['rows']
    row = next(item for item in rows if item['name'] == 'посуда')
    assert not row['measured'] and '3,1 раза' in row['basis']
    assert all(not item['measured'] for item in rows)


def test_country_context_not_search_country_controls_measured_market():
    data = extract_observations('смартфон', 'В Казахстане за 2026 год продажи смартфонов составили 500 штук.', 'Россия')
    assert data[0]['country'] == 'Казахстан'


def test_missing_reporting_period_keeps_public_signal_not_measurement(service):
    ingest(service, 'В России продажи смартфонов составили 500 штук.', published=now())
    row = service.store.one('SELECT * FROM market_observations')
    assert row['metric'] == 'public_signal' and row['value'] is None and row['measured'] == 0
    ranking = service.market_ranking(['Россия'])
    assert ranking['preliminary'] and 'публичным признакам' in ranking['caption']
    assert 'продаж' not in ranking['caption']


def test_same_event_reprinted_does_not_increase_event_or_position(service):
    text = 'В России за 2026 год продажи смартфонов составили 500 штук.'
    ingest(service, text, 'https://source.example/one')
    first = service.market_ranking(['Россия'])
    ingest(service, text, 'https://other.example/reprint')
    assert service.store.one('SELECT COUNT(*) n FROM market_observations')['n'] == 1
    assert service.store.one('SELECT COUNT(*) n FROM sources')['n'] == 2
    second = service.market_ranking(['Россия'])
    assert [(r['id'], r['basis']) for r in first['rows']] == [(r['id'], r['basis']) for r in second['rows']]
    assert second['rows'][0]['count'] == 2
    assert len(market_evidence(service.store, second['rows'][0]['id'], ['Россия'])) == 2


def test_search_counts_and_sales_are_never_summed(service):
    ingest(service, 'В России за 2026 год продажи смартфонов составили 500 штук.', 'https://source.example/sales')
    ingest(service, 'В России за 2026 год поисковая частотность смартфонов составила 10 000 запросов.', 'https://source.example/search')
    result = service.market_ranking(['Россия'])
    assert len(result['rows']) == 1
    measures = result['rows'][0]['measurements']
    assert {(row['metric'], row['value'], row['unit']) for row in measures} == {
        ('sales', 500, 'units'), ('searches', 10000, 'queries')}
    assert all(row['value'] != 10500 for row in measures)


def test_repeated_mentions_do_not_raise_preliminary_rank(service):
    ingest(service, 'В России в 2026 году спрос на кофе растёт.', 'https://source.example/coffee')
    ingest(service, 'В России в 2026 году спрос на ноутбуки растёт.', 'https://source.example/laptop')
    # Pin recency to equal data so only deterministic product order remains.
    service.store.execute("UPDATE market_observations SET created_at='2026-11-12T00:00:00+00:00'")
    first = service.market_ranking(['Россия'])
    for number in range(5):
        ingest(service, 'В России в 2026 году спрос на кофе растёт.', 'https://reprint.example/' + str(number))
    second = service.market_ranking(['Россия'])
    assert [r['id'] for r in first['rows']] == [r['id'] for r in second['rows']]
    assert first['preliminary'] and second['preliminary']


def test_ranking_uses_comparable_demand_and_ignores_storage_time(service):
    current = datetime.now(timezone.utc)
    year = current.year
    ingest(service, f'В России за {year} год продажи смартфонов составили 100 штук.', 'https://source.example/phone')
    ingest(service, f'В России за {year} год продажи ноутбуков составили 200 штук.', 'https://source.example/laptop')
    ingest(service, f'В России в {year} году спрос на кофе растёт.', 'https://source.example/coffee')
    ingest(service, f'В России в {year} году спрос на пылесосы растёт.', 'https://source.example/vacuum')
    before = service.market_ranking(['Россия'])['rows']
    assert [(r['name'], r['demand_percent']) for r in before] == [
        ('ноутбук', 100), ('смартфон', 50), ('кофе', None), ('пылесос', None)]
    service.store.execute('UPDATE market_observations SET created_at=CASE WHEN product_id=? THEN ? ELSE ? END',
                          (before[-1]['id'], (current - timedelta(days=1)).isoformat(),
                           (current - timedelta(days=2)).isoformat()))
    after = service.market_ranking(['Россия'])['rows']
    assert [(r['name'], r['demand_percent']) for r in after] == [(r['name'], r['demand_percent']) for r in before]


def test_incompatible_measures_do_not_create_a_percentage(service):
    ingest(service, 'В России за 2026 год продажи кофе составили 100 штук.', 'https://source.example/coffee')
    ingest(service, 'В России за 2026 год поисковая частотность смартфонов составила 10 000 запросов.', 'https://source.example/phone')
    rows = service.market_ranking(['Россия'])['rows']
    assert [(r['name'], r['demand_percent']) for r in rows] == [('кофе', None), ('смартфон', None)]


def test_brand_mentions_share_one_type_and_empty_is_not_artificial_top(service):
    assert service.market_ranking(['Россия'])['rows'] == []
    for name in ('iPhone', 'Samsung Galaxy', 'Xiaomi'):
        ingest(service, f'В России в 2026 году растёт спрос на {name}: продажи увеличились.', f'https://source.example/{name.split()[0]}')
    assert service.store.one('SELECT COUNT(*) n FROM products')['n'] == 1
    assert len(service.market_ranking(['Россия'])['rows']) == 1


def test_structured_official_import_keeps_actual_measure_no_page_fetch(service):
    excerpt = 'Импорт товара «кофе» в Казахстан за 2025: 47805677.89 USD.'
    result = SearchResult('https://official.example/api?period=2025', 'Импорт кофе', excerpt, 'Казахстан', metadata={
        'product': 'кофе', 'official_statistics': True, 'measurements': [
            {'metric': 'imports', 'value': 47805677.89, 'unit': 'USD', 'period': '2025',
             'country': 'Казахстан', 'published_at': '', 'excerpt': excerpt}]})
    service._market_evidence(result, lambda message: None)
    service._page.assert_not_called()
    row = service.store.one('SELECT * FROM market_observations')
    assert row['metric'] == 'imports' and row['unit'] == 'USD' and row['value'] == 47805677.89
    assert row['period'] == '2025' and row['published_at'] == ''
    ranking = service.market_ranking(['Казахстан'])
    assert ranking['rows'] == []  # Past imports are retained, not promoted to current demand.
    assert service.store.query("SELECT * FROM usage_log WHERE provider='gigachat'") == []


def test_incompatible_structured_metric_unit_rejected():
    data = extract_observations('кофе', 'Кофе', metadata={'measurements': [
        {'metric': 'searches', 'unit': 'RUB', 'value': 500, 'period': '2026', 'country': 'Россия', 'excerpt': 'Кофе'}]})
    assert data == []


def test_structured_number_must_match_preserved_source():
    metadata = {'measurements': [{'metric': 'imports', 'unit': 'USD', 'value': 999,
        'period': '2025', 'country': 'Казахстан', 'excerpt': 'Импорт кофе в Казахстан за 2025: 123 USD.'}]}
    assert extract_observations('кофе', 'Импорт кофе', metadata=metadata) == []


def test_month_range_is_not_mislabelled_as_last_month():
    data = extract_observations('смартфон', 'В России за январь — сентябрь 2026 продажи смартфонов составили 500 штук.')
    assert data[0]['period'] == '2026-01/2026-09'


@pytest.mark.parametrize('override', [{'unit': 'units'}, {'country': 'Россия'}, {'period': 'unknown'}])
def test_structured_units_country_period_agree_with_source(override):
    measurement = {'metric': 'imports', 'unit': 'USD', 'value': 123, 'period': '2025',
                   'country': 'Казахстан', 'excerpt': 'Импорт кофе в Казахстан за 2025: 123 USD.'}
    measurement.update(override)
    assert extract_observations('кофе', 'Кофе', metadata={'measurements': [measurement]}) == []


def test_old_schema_records_survive_and_legacy_score_is_not_a_measurement(tmp_path):
    path = tmp_path / 'version-one.sqlite3'
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA + '\nPRAGMA user_version=1;')
    connection.execute("INSERT INTO products VALUES(1,'кофе','кофе',?)", (now(),))
    connection.execute("INSERT INTO sources(id,url,text,hash,fetched_at) VALUES(1,'https://old.example','Спрос на кофе','old',?)", (now(),))
    connection.execute("INSERT INTO market_signals(product_id,source_id,country,kind,description,score,created_at) VALUES(1,1,'Россия','published_indicator','Спрос на кофе',999,?)", (now(),))
    connection.commit()
    connection.close()
    store = Store(path)
    assert store.one('PRAGMA user_version')['user_version'] == max(MIGRATIONS)
    assert store.one('SELECT name FROM products')['name'] == 'кофе'
    assert store.one('SELECT score FROM market_signals')['score'] == 999
    assert store.query('SELECT * FROM market_observations') == []
    result = market_ranking(store, ['Россия'])
    assert result['preliminary'] and result['rows'] == []
    store.close()


def test_rss_article_body_supplies_measurement_without_navigation_products(service):
    body = article_text('<nav>Продажи обуви растут.</nav><article><p>В России за октябрь 2026 года продажи кофе составили 500 штук.</p><div class="tags">Теги: #самокат #картон #обувь #спрос</div></article>')
    source = service.store.put_source('https://retail.example/news', text=body)
    service._page = Mock(return_value={'text': body, 'market_text': body, 'source_id': source})
    result = SearchResult('https://retail.example/news', 'Исследование продаж кофе',
        'Опубликовано исследование.', 'Россия', metadata={'provider': 'rss', 'feed_url': 'https://retail.example/rss'})
    service._market_evidence(result, lambda message: None)
    service._page.assert_called_once()
    assert [row['name'] for row in service.store.query('SELECT name FROM products')] == ['кофе']
    row = service.market_ranking(['Россия'])['rows'][0]
    assert 'Теги' not in row['basis'] and row['measurements'][0]['value'] == 500


def test_article_body_excludes_navigation_and_footer_products():
    html = '''<nav>Растёт спрос на смартфоны.</nav><main><article>
      <p>В России за 2026 год продажи кофе составили 500 штук.</p>
      <div class="tags">Теги: #самокат #картон #продажи</div>
      </article><aside>Продажи обуви растут.</aside></main>'''
    text = article_text(html)
    assert '500' in text and 'смартфоны' not in text and 'обуви' not in text and '#' not in text
    assert extract_observations('кофе', text)[0]['value'] == 500
    assert extract_observations('самокат', text) == []


def test_saved_tagcloud_record_is_not_shown_in_ranking(service):
    product = service.store.ensure_product('самокат')
    excerpt = 'Retail.ru Теги: #фров #самокат #онлайн-продажи Темы: Марка.'
    source = service.store.put_source('https://retail.example/old', text=excerpt)
    service.store.execute('''INSERT INTO market_observations(product_id,source_id,metric,excerpt,event_key,country,created_at)
        VALUES(?,?,'public_signal',?,'old-tag-cloud','Россия',?)''', (product['id'], source, excerpt, now()))
    assert service.market_ranking(['Россия'])['rows'] == []
    assert service.store.one('SELECT COUNT(*) n FROM market_observations')['n'] == 1


def test_delivery_service_samokat_is_not_a_vehicle_product(service):
    ingest(service, '«Самокат» и Яндекс Лавка рассказали о росте онлайн-продаж ФРОВ.')
    assert service.store.query('SELECT * FROM products') == []


@pytest.mark.parametrize('text,product', [
    ('Календарь спроса: какую одежду покупают по месяцам.', 'одежда'),
    ('Производитель кормов для животных рассказал о выводе на рынок нового продукта.', 'корм для животных'),
])
def test_calendar_and_launch_do_not_establish_demand(service, text, product):
    result = SearchResult('https://retail.example/article', text, text, 'Россия', metadata={'provider': 'rss'})
    service._market_evidence(result, lambda _: None)
    rows = service.market_ranking(['Россия'])['rows']
    assert rows == []


def test_legacy_null_description_uses_saved_evidence(service):
    product = service.store.ensure_product('кофе')
    source = service.store.put_source('https://source.example/legacy', text='В России в 2026 году спрос на кофе растёт.')
    service.store.execute('INSERT INTO market_signals(product_id,source_id,country,created_at) VALUES(?,?,?,?)',
                         (product['id'], source, 'Россия', now()))
    ranking = service.market_ranking(['Россия'])
    assert 'В России в 2026 году спрос на кофе растёт.' in ranking['rows'][0]['basis']
    assert market_evidence(service.store, product['id'], ['Россия'])[0]['fact'] == 'В России в 2026 году спрос на кофе растёт.'


def test_card_and_potato_are_not_cardboard():
    from sales_manager.research.normalization import matches_product, product_candidates
    assert not matches_product('картон', 'Карточка товара и картофель.')
    assert 'картон' not in product_candidates('Адаптировать карточки товара.')
    assert matches_product('картон', 'Упаковка из картона.')


def test_ten_actual_distinct_goods_produce_ten_rows_without_fillers(service):
    products = ['смартфон', 'ноутбук', 'планшет', 'монитор', 'принтер',
                'компьютер', 'пылесос', 'кофе', 'чай', 'краска']
    for index, product in enumerate(products):
        ingest(service, f'В России за октябрь 2026 года продажи товара «{product}» составили {100 + index} штук.',
               f'https://source.example/product-{index}', published=now())
    ranking = service.market_ranking(['Россия'])
    assert len(ranking['rows']) == 10
    assert {row['name'] for row in ranking['rows']} == set(products)
    assert all(row['measured'] and row['count'] == 1 for row in ranking['rows'])
    assert len(service.market_ranking(['Россия'], limit=3)['rows']) == 3


def test_current_publication_does_not_make_old_reporting_period_current(service):
    ingest(service, 'В России за 2020 год продажи смартфонов составили 500 штук.', published=now())
    assert service.store.one('SELECT value FROM market_observations')['value'] == 500
    assert service.market_ranking(['Россия'])['rows'] == []


def test_unknown_or_future_publication_and_future_period_are_not_current(service):
    ingest(service, 'В России спрос на кофе растёт.', published='not-a-date')
    ingest(service, 'В России за декабрь 2026 года продажи смартфонов составили 500 штук.',
           url='https://source.example/future', published=now())
    assert market_ranking(service.store, ['Россия'], as_of=datetime(2026, 11, 13, tzinfo=timezone.utc))['rows'] == []


@pytest.mark.parametrize(('end_year', 'published', 'evaluation_year', 'expected_periods'), [
    (2030, '2026-11-01', 2026, []),
    (2030, '2026-11-01', 2031, []),
    (2030, '', 2026, []),
    (2026, '2026-11-01', 2026, ['2025/2026']),
    (2030, '', 2031, ['2025/2030']),
])
def test_multi_year_measurement_respects_year_of_publication_or_evaluation(
        service, end_year, published, evaluation_year, expected_periods):
    ingest(service, f'В России за 2025–{end_year} годы продажи кофе выросли на 12%.', published=published)
    as_of = datetime(evaluation_year, 11, 20, tzinfo=timezone.utc)
    ranking = market_ranking(service.store, ['Россия'], as_of=as_of)
    assert [row['measurements'][0]['period'] for row in ranking['rows']] == expected_periods


def test_query_country_does_not_assign_public_demand_to_wrong_market(service):
    ingest(service, 'В Казахстане в 2026 году спрос на кофе растёт.', country='Россия')
    assert service.store.one('SELECT country FROM market_observations')['country'] == 'Казахстан'
    assert service.market_ranking(['Россия'])['rows'] == []
    row = service.market_ranking(['Казахстан'])['rows'][0]
    assert market_evidence(service.store, row['id'], ['Россия']) == []
    assert len(market_evidence(service.store, row['id'], ['Казахстан'])) == 1


def test_missing_country_and_plain_product_launch_do_not_become_public_demand():
    for text in ['Спрос на кофе растёт.', 'В России кофе впервые вышел на рынок.',
                 'В России рассказали, какую одежду покупают по месяцам.',
                 'В России в 2026 году растёт ассортимент кофе в продаже.']:
        assert extract_observations('кофе' if 'кофе' in text else 'одежда', text, 'Россия') == []


def test_sources_table_uses_same_filters_and_duplicate_events_as_ranking(service):
    ingest(service, 'В России за октябрь 2026 года продажи кофе составили 500 штук.', published=now())
    observation = service.store.one('SELECT * FROM market_observations')
    for url, excerpt, date in [
        ('https://other.example/good', 'В России за октябрь 2026 года продажи кофе составили 500 штук.', now()),
        ('https://other.example/wrong-market', 'В Казахстане за октябрь 2026 года продажи кофе составили 500 штук.', now()),
        ('https://other.example/old', 'В России за октябрь 2026 года продажи кофе составили 500 штук.', '2020-01-01'),
    ]:
        source = service.store.put_source(url, text=excerpt, published_at=date)
        service.store.execute('INSERT INTO market_observation_sources(observation_id,source_id,excerpt,published_at) VALUES(?,?,?,?)',
                             (observation['id'], source, excerpt, date))
    row = service.market_ranking(['Россия'])['rows'][0]
    sources = market_evidence(service.store, row['id'], ['Россия'])
    assert row['count'] == len(sources) == 2
    assert set(row['source_ids']) == {source['id'] for source in sources}
    assert len(row['measurements']) == 1
    assert {source['url'] for source in sources} == {'https://source.example/report', 'https://other.example/good'}
    assert all(source['fact'] and source['published_at'] for source in sources)


def test_measurement_source_migration_preserves_original_evidence(tmp_path):
    from sales_manager.storage.migrations import MARKET_AND_FIT
    path = tmp_path / 'version-two.sqlite3'
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA + MARKET_AND_FIT + '\nPRAGMA user_version=2;')
    connection.execute("INSERT INTO products VALUES(1,'кофе','кофе',?)", (now(),))
    connection.execute("INSERT INTO sources(id,url,text,hash,fetched_at) VALUES(1,'https://old.example','Старое свидетельство','old',?)", (now(),))
    connection.execute("INSERT INTO market_observations(product_id,source_id,metric,value,unit,period,country,published_at,excerpt,measured,event_key,created_at) VALUES(1,1,'sales',500,'units','2026','Россия','','Старое свидетельство',1,'event',?)", (now(),))
    connection.commit()
    connection.close()
    store = Store(path)
    assert store.one('PRAGMA user_version')['user_version'] == max(MIGRATIONS)
    assert store.one('SELECT value FROM market_observations')['value'] == 500
    assert store.one('SELECT * FROM market_observation_sources') == {
        'observation_id': 1, 'source_id': 1, 'excerpt': 'Старое свидетельство', 'published_at': ''}
    store.close()
