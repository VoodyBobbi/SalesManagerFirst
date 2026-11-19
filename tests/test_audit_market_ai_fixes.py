"""Observed market and offer regressions, including false-positive controls."""
import json
from types import SimpleNamespace

import pytest

from sales_manager.ai.service import AIService
from sales_manager.research.fit_rules import evaluate_fit
from sales_manager.research.market import article_text, country_from, extract_observations, period_from
from sales_manager.research.normalization import matches_product, normalize_product, product_candidates
from sales_manager.storage import Store, now


@pytest.mark.parametrize('product,form', [
    ('чай', 'чая'), ('сыр', 'сыра'), ('мыло', 'мыла'), ('крем', 'кремов'),
    ('мясо', 'мяса'), ('рыба', 'рыбы'), ('крупа', 'крупы'), ('мука', 'муки'),
    ('овощи', 'овощей'), ('книга', 'книг'), ('ткань', 'тканей'), ('обои', 'обоев'),
    ('труба', 'труб'), ('дрель', 'дрели'), ('насос', 'насосов'), ('диван', 'диванов'),
    ('болт', 'болтов'),
])
def test_short_inflections_produce_grounded_market_facts(product, form):
    text = f'В России за ноябрь 2026 года продажи {form} выросли на 20%.'
    assert matches_product(product, text)
    assert product in product_candidates(text)
    facts = extract_observations(product, text)
    assert any(fact['metric'] == 'sales_growth' and fact['value'] == 20 for fact in facts)


@pytest.mark.parametrize('product,unrelated', [
    ('сыр', 'сырье'), ('крем', 'кремний'), ('чай', 'случай'), ('болт', 'болтовня'),
    ('ткань', 'тканевый'), ('диван', 'диванный'), ('мука', 'мукомольный'),
])
def test_short_forms_do_not_match_unrelated_word_extensions(product, unrelated):
    assert not matches_product(product, unrelated)


def test_tv_advertising_does_not_invent_television_demand():
    assert 'телевизор' not in product_candidates('В России вырос спрос на кофе после рекламы на ТВ.')
    assert normalize_product('Smart ТВ') == 'телевизор'


@pytest.mark.parametrize('product', ['мебель', 'офисная мебель'])
def test_generic_furniture_has_its_activity_relationship(product):
    result = evaluate_fit(product, [{'source_id': 1, 'text': 'Наш магазин офисной мебели работает в России.'}], {}, 'Россия')
    assert result['status'] == 'FIT'
    assert result['profile']['rule'].startswith('activity:')


@pytest.mark.parametrize('origin', ['китайских смартфонов', 'смартфонов из США', 'смартфонов из ЕС'])
def test_import_origin_does_not_erase_explicit_local_market(origin):
    text = f'В России за ноябрь 2026 года продажи {origin} выросли на 20%.'
    assert country_from(text) == 'Россия'
    assert any(fact['country'] == 'Россия' and fact['value'] == 20
               for fact in extract_observations('смартфон', text))


@pytest.mark.parametrize('geography', ['в России и Казахстане', 'в России и в Китае', 'в России, Казахстане'])
def test_multiple_markets_remain_ambiguous(geography):
    text = f'Продажи смартфонов {geography} за ноябрь 2026 года выросли на 20%.'
    assert country_from(text) == ''
    assert not extract_observations('смартфон', text, country='Россия')


@pytest.mark.parametrize('tail', [
    'что совпадает с данными продавцов', 'что попадает в отчёт продавцов',
    'что может быть связано с рекламой', 'компания планирует расширить сеть',
])
def test_real_growth_survives_unrelated_words_and_explanations(tail):
    text = f'В России за ноябрь 2026 года продажи смартфонов выросли на 20%, {tail}.'
    assert any(fact['metric'] == 'sales_growth' and fact['value'] == 20
               for fact in extract_observations('смартфон', text))


@pytest.mark.parametrize('claim', [
    'продажи смартфонов падают', 'наблюдается спад продаж смартфонов',
    'продажи смартфонов могут вырасти на 20%', 'компания планирует увеличить продажи смартфонов на 20%',
])
def test_declines_and_forecasts_stay_excluded(claim):
    assert not extract_observations('смартфон', f'В России за ноябрь 2026 года {claim}.')


@pytest.mark.parametrize('expression,expected', [
    ('за I квартал 2026 года', '2026-Q1'), ('за IV квартал 2026', '2026-Q4'),
    ('за 1 кв. 2026', '2026-Q1'), ('за первое полугодие 2026', '2026-01/2026-06'),
    ('за второе полугодие 2026', '2026-07/2026-12'), ('за 9 месяцев 2026 года', '2026-01/2026-09'),
])
def test_explicit_reporting_periods_are_preserved(expression, expected):
    text = f'В России {expression} продажи смартфонов составили 100 штук.'
    assert period_from(text) == expected
    assert any(fact['period'] == expected and fact['value'] == 100
               for fact in extract_observations('смартфон', text))


@pytest.mark.parametrize('wrapper', ['article', 'main', 'div class="story"'])
def test_article_extraction_keeps_div_facts_and_removes_navigation(wrapper):
    tag = wrapper.split()[0]
    fact = 'В России за ноябрь 2026 года продажи смартфонов выросли на 20%.'
    html = ('<html><body><nav>В Китае выросли продажи ноутбуков.</nav>'
            f'<{wrapper}><h1>Отчёт</h1><p>Публикация результатов.</p><div>{fact}</div>'
            f'</{tag}><footer>Подписка</footer></body></html>')
    text = article_text(html)
    assert fact in text and 'В Китае' not in text and 'Подписка' not in text
    assert any(fact['value'] == 20 for fact in extract_observations('смартфон', text))


def offer(body):
    return {'subject': 'Сотрудничество', 'body': body,
            'reason': 'Гипотеза: расширение ассортимента.', 'source_ids': [1]}


def card(text='', signature=''):
    return {'product': 'Техника', 'company': 'Покупатель', 'sender': 'Аргон', 'signature': signature,
            'sources': [{'id': 1, 'url': 'https://buyer.test/', 'text': text}]}


@pytest.mark.parametrize('symbol,currency', [('$', 'долларов'), ('€', 'евро')])
def test_prefix_currency_confirms_same_currency_and_rejects_other(symbol, currency):
    source = f'Стоимость оборудования составляет {symbol}100.'
    body = f'Стоимость оборудования составляет 100 {currency}.'
    assert AIService.validate_offer(offer(body), card(source))['body'] == body
    with pytest.raises(ValueError, match='неподтвержд'):
        AIService.validate_offer(offer('Стоимость оборудования составляет 100 рублей.'), card(source))


def test_signature_yo_variation_is_replaced_by_exact_saved_signature():
    signature = 'Артём, ООО «Берёзка», офис 12'
    result = AIService.validate_offer(offer('Обсудим сотрудничество.\nАртем, ООО «Березка», офис 12'), card(signature=signature))
    assert result['body'] == 'Обсудим сотрудничество.\n\n' + signature
    with pytest.raises(ValueError, match='неподтвержд'):
        AIService.validate_offer(offer('Обсудим сотрудничество.\nАртем, ООО «Березка», офис 13'), card(signature=signature))


def test_offer_window_keeps_existing_ai_citation_from_long_saved_source(tmp_path):
    store = Store(tmp_path / 'quote.sqlite3')
    try:
        pid = store.ensure_product('Принтер')['id']
        cid = store.execute('INSERT INTO companies(name,created_at) VALUES(?,?)', ('Типография', now()))
        quote = 'Мы печатаем книги и учебники для школ.'
        sid = store.put_source('https://buyer.test/about', text=('Навигация. ' * 250) + quote)
        store.execute('INSERT INTO company_fit(product_id,company_id,status,reason,source_ids) VALUES(?,?,?,?,?)',
                      (pid, cid, 'FIT', 'Гипотеза: печать книг. Основание: «' + quote + '»', json.dumps([sid])))
        ai = AIService(store, SimpleNamespace(get=lambda *_: ''))
        source = ai._card(pid, cid)['sources'][0]
        assert quote in source['text'] and len(source['text']) <= 1200
    finally:
        store.close()
