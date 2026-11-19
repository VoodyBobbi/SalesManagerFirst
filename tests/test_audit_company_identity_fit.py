import json
from unittest.mock import Mock

import pytest

from sales_manager.core.company_listing import visible_companies
from sales_manager.research.extraction import parse_page
from sales_manager.research.fit_rules import evaluate_fit, semantic_evidence_allowed
from sales_manager.research.providers import SearchResult
from sales_manager.research.service import ResearchService
from sales_manager.storage import Store, now


@pytest.fixture
def research(tmp_path):
    store = Store(tmp_path / 'companies.sqlite3')
    store.set_setting('search.page_limit', 1)
    yield ResearchService(store, Mock(get=Mock(return_value='')))
    store.close()


def crawl(research, product_id, html, result=None):
    result = result or SearchResult('https://buyer.kz/', country='Казахстан')
    page = parse_page(html, result.url, result.country)
    page['source_id'] = research.store.put_source(result.url, page['title'], page['text'])
    research._page = Mock(return_value=page)
    research._company(product_id, result, False, lambda _: None)


@pytest.mark.parametrize('external_activity', [
    'Рядом находится кафе.',
    'В соседнем здании расположено кафе.',
    'Кафе через дорогу.',
    'До нас можно дойти через гостиницу.',
    'Клиенты компании — кафе.',
    'Партнёры открыли кафе.',
])
def test_mentioned_business_does_not_establish_own_activity(external_activity):
    pages = [{'text': external_activity + ' Мы продаём компьютеры.', 'source_id': 1}]
    assert evaluate_fit('кофе', pages, {}, 'Россия')['status'] == 'INSUFFICIENT_EVIDENCE'
    assert not semantic_evidence_allowed('кофе', pages, external_activity)


def test_opening_directions_keep_own_activity_and_reject_other_business():
    description = 'Наше кафе находится рядом с музеем.'
    pages = [{'text': 'Через дорогу работает ресторан. ' + description + ' Рядом есть ресторан.', 'source_id': 1}]
    decision = evaluate_fit('кофе', pages, {}, 'Россия')
    assert decision['status'] == 'FIT'
    assert semantic_evidence_allowed('кофе', pages, description)


@pytest.mark.parametrize('quote', [
    'Наша компания продаёт компьютеры. Рядом находится кафе.',
    'Наша компания продаёт компьютеры, а рядом находится кафе.',
])
def test_ai_citation_keeps_ownership_in_the_relevant_clause(quote):
    assert not semantic_evidence_allowed('кофе', [{'text': quote, 'source_id': 1}], quote)


def test_changed_website_name_keeps_company_and_saved_offer(research):
    store = research.store
    product_id = store.ensure_product('кофе')['id']
    crawl(research, product_id, '<title>Альфа</title><p>Наше кафе работает каждый день.</p>')
    company_id = store.one('SELECT id FROM companies')['id']
    store.execute('UPDATE company_fit SET fit_key=?, reason=?, manual=1', ('manual:checked', 'Вручную проверено'))
    store.execute('INSERT INTO offers(product_id,company_id,subject,body,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                  (product_id, company_id, 'Тема', 'Ручная правка', now(), now()))

    crawl(research, product_id, '<meta property="og:site_name" content="Альфа — магазин">'
                               '<p>Рядом находится кафе. Мы продаём компьютеры.</p>')

    assert store.query('SELECT id FROM companies') == [{'id': company_id}]
    assert store.one('SELECT name FROM companies WHERE id=?', (company_id,))['name'] == 'Альфа — магазин'
    assert store.one('SELECT reason FROM company_fit')['reason'] == 'Вручную проверено'
    assert store.one('SELECT body FROM offers WHERE company_id=?', (company_id,))['body'] == 'Ручная правка'


@pytest.mark.parametrize('ai_key', ['', 'accepted-ai'])
def test_recrawl_rechecks_previously_accepted_automatic_activity(research, ai_key):
    store = research.store
    product_id = store.ensure_product('кофе')['id']
    quote = 'Рядом находится кафе.'
    html = '<title>Альфа</title><p>' + quote + ' Мы продаём компьютеры.</p>'
    crawl(research, product_id, html)
    store.execute("UPDATE company_fit SET status='FIT', fit_key='prior-rule', ai_checked_key=?, reason=?",
                  (ai_key, 'Гипотеза: возможно предложение кофе. Основание: «' + quote + '»'))
    crawl(research, product_id, html)
    assert store.one('SELECT status FROM company_fit')['status'] == 'INSUFFICIENT_EVIDENCE'


def test_shared_url_does_not_merge_different_legal_identities(research):
    domain = 'buyer.kz'
    url = 'https://buyer.kz/'
    first = research._save_company('Альфа', domain, url, 'Казахстан', '1111111111', False)
    second = research._save_company('Бета', domain, url, 'Казахстан', '2222222222', False)
    unknown = research._save_company('Другой магазин', domain, url, 'Казахстан', '', False)

    assert len({first, second, unknown}) == 3
    assert research._save_company('Альфа — обновлено', domain, url, 'Казахстан', '1111111111', False) == first


def test_site_without_branch_evidence_does_not_merge_into_mapped_branch(research):
    product_id = research.store.ensure_product('кофе')['id']
    branch = SearchResult('https://buyer.kz/', 'Кафе «Альфа»', country='Казахстан', metadata={
        'provider': 'overpass', 'source_url': 'https://www.openstreetmap.org/node/11',
        'tags': {'amenity': 'cafe', 'addr:street': 'Главная', 'addr:housenumber': '10'},
    })
    page = '<title>Альфа</title><p>Рядом находится кафе. Мы продаём компьютеры.</p>'
    crawl(research, product_id, page, branch)
    crawl(research, product_id, page)
    crawl(research, product_id, page, branch)

    assert research.store.query('SELECT status FROM company_fit ORDER BY company_id') == [
        {'status': 'FIT'}, {'status': 'INSUFFICIENT_EVIDENCE'},
    ]


def test_payment_operator_inn_does_not_merge_two_websites(research):
    pid = research.store.ensure_product('кофе')['id']
    for host in ('alpha-shop.ru', 'beta-shop.ru'):
        crawl(research, pid, '<title>Наше кафе</title><h2>Реквизиты платёжного оператора</h2>'
                             f'<p>ИНН <b>7701234560</b></p><p>info@{host}</p>',
              SearchResult(f'https://{host}/', country='Россия'))

    rows = visible_companies(research.store, pid)
    assert {row['email'] for row in rows} == {'info@alpha-shop.ru', 'info@beta-shop.ru'}
    assert all(not row['legal_id'] for row in rows)


def test_candidates_needing_review_remain_visible_without_contact(research):
    store = research.store
    pid = store.ensure_product('кофе')['id']
    cid = store.execute('INSERT INTO companies(name,created_at) VALUES(?,?)', ('Неизвестная компания', now()))
    store.execute('INSERT INTO company_fit(product_id,company_id,status,reason,source_ids,checked_at,potential) '
                  'VALUES(?,?,?,?,?,?,?)', (pid, cid, 'INSUFFICIENT_EVIDENCE', 'Нужна проверка.', '[]', now(), 0))
    rows = visible_companies(store, pid)
    assert [row['id'] for row in rows] == [cid]
    assert rows[0]['fit_status'] == 'INSUFFICIENT_EVIDENCE' and not rows[0]['email']


def test_branch_contacts_keep_their_own_page_source_and_second_page_branches(research):
    store = research.store
    store.set_setting('search.page_limit', 3)
    pid = store.ensure_product('Кофе')['id']
    first_url = 'https://buyer.kz/'
    branch_url = 'https://buyer.kz/contacts'
    first = parse_page('<title>Компания</title><a href="/contacts">Контакты филиалов</a>', first_url, 'Казахстан')
    second = parse_page('<title>Филиалы</title><div class="branch"><h3>Алматы</h3>'
                        '<address>Алматы, Абая 10</address><a href="mailto:branch@buyer.kz">Email</a></div>',
                        branch_url, 'Казахстан')
    first['source_id'] = store.put_source(first_url, first['title'], first['text'])
    second['source_id'] = store.put_source(branch_url, second['title'], second['text'])
    research._page = Mock(side_effect=lambda url, country: first if url == first_url else second)
    research._company(pid, SearchResult(first_url, country='Казахстан'), False, lambda _: None)
    branch = store.one("SELECT c.id FROM companies c WHERE c.branch_name='Алматы'")
    assert branch
    contact = store.one("SELECT source_id FROM company_contacts WHERE company_id=? AND value='branch@buyer.kz'", (branch['id'],))
    assert contact
    source = store.one('SELECT url,text FROM sources WHERE id=?', (contact['source_id'],))
    assert source['url'] == branch_url
    assert 'branch@buyer.kz' in source['text'] and 'Алматы, Абая 10' in source['text']


def test_branch_does_not_inherit_parent_activity_without_branch_evidence(research):
    store = research.store
    store.set_setting('search.page_limit', 2)
    pid = store.ensure_product('Кофе')['id']
    first_url = 'https://branchco.kz/'
    branch_url = 'https://branchco.kz/contacts'
    first = parse_page('<title>BranchCo</title><a href="/contacts">Филиалы</a>', first_url, 'Казахстан')
    second = parse_page('<title>Филиалы</title><div class="branch"><h3>Алматы</h3>'
                        '<address>Алматы, Абая 10</address></div>', branch_url, 'Казахстан')
    first['source_id'] = store.put_source(first_url, first['title'], first['text'])
    second['source_id'] = store.put_source(branch_url, second['title'], second['text'])
    research._page = Mock(side_effect=lambda url, country: first if url == first_url else second)
    result = SearchResult(first_url, title='BranchCo', country='Казахстан', metadata={
        'provider': 'overpass', 'source_url': 'https://www.openstreetmap.org/node/15',
        'structured_text': 'amenity=cafe', 'tags': {'amenity': 'cafe'},
    })

    research._company(pid, result, False, lambda _: None)

    parent = store.one("SELECT f.status FROM company_fit f JOIN companies c ON c.id=f.company_id "
                       "WHERE c.name='BranchCo' ORDER BY c.id LIMIT 1")
    branch = store.one("SELECT f.status,c.branch_name FROM company_fit f JOIN companies c ON c.id=f.company_id "
                       "WHERE c.branch_name='Алматы'")
    assert parent['status'] == 'FIT'
    assert branch == {'status': 'INSUFFICIENT_EVIDENCE', 'branch_name': 'Алматы'}


def test_recrawl_clears_known_external_inn_and_reuses_confirmed_own_inn(research):
    store = research.store
    pid = store.ensure_product('кофе')['id']
    agency_customer = SearchResult('https://alpha-shop.ru/', country='Россия')
    payment_company = SearchResult('https://beta-shop.ru/', country='Россия')
    operator_id = '7701234560'
    customer_own_id = '9900000017'
    html = '<title>Альфа</title><p>Наше кафе открыто.</p><p>Реквизиты платёжного оператора: ИНН 7701234560.</p>'
    crawl(research, pid, html, agency_customer)
    alpha = store.one('SELECT id FROM companies')['id']
    store.execute('UPDATE companies SET legal_id=? WHERE id=?', (operator_id, alpha))
    crawl(research, pid, '<title>Бета</title><p>Реквизиты организации: ИНН 7701234560.</p>', payment_company)

    beta = store.one("SELECT id FROM companies WHERE domain='beta-shop.ru'")['id']
    assert beta != alpha
    crawl(research, pid, html + '<p>Наши реквизиты: ИНН 9900000017.</p>', agency_customer)
    assert store.one('SELECT legal_id FROM companies WHERE id=?', (alpha,))['legal_id'] == customer_own_id
    assert store.one("SELECT id FROM audit_events WHERE event='company_legal_id_retired'")

    crawl(research, pid, '<title>Бета</title><p>Реквизиты организации: ИНН 7701234560.</p>',
          SearchResult('https://new-beta-shop.ru/', country='Россия'))
    assert store.one('SELECT COUNT(*) AS n FROM companies')['n'] == 2


@pytest.mark.parametrize('credit', [
    '<b>Разработка</b> сайта: студия Бета. <a href="mailto:sales@studio-beta.ru">Заказать сайт</a>',
    '<a href="mailto:sales@studio-beta.ru">Разработка сайта</a>',
    'Разработчик сайта: sales@studio-beta.ru. Контакты компании: info@alpha-shop.ru.',
])
def test_explicit_website_credit_cannot_be_the_company_contact(research, credit):
    store = research.store
    pid = store.ensure_product('кофе')['id']
    result = SearchResult('https://alpha-shop.ru/', country='Россия')
    html = '<title>Альфа</title><p>Наше кафе. Контакты кафе: info@alpha-shop.ru.</p><footer>' + credit + '</footer>'
    crawl(research, pid, html, result)
    company = visible_companies(store, pid)[0]
    assert company['email'] == 'info@alpha-shop.ru'
    assert store.query('SELECT value FROM company_contacts') == [{'value': 'info@alpha-shop.ru'}]


def test_web_studio_service_description_keeps_its_contact():
    html = '<p>Разработка сайта любой сложности. Написать: <a href="mailto:sales@alpha-studio.ru">Почта</a></p>'
    page = parse_page(html, 'https://alpha-studio.ru/', 'Россия')
    assert page['emails'] == ['sales@alpha-studio.ru']


def test_recrawl_retires_developer_email_but_preserves_manual_address(research):
    store = research.store
    pid = store.ensure_product('кофе')['id']
    original = SearchResult('https://alpha-shop.ru/', country='Россия')
    base = '<title>Альфа</title><p>Наше кафе открыто.</p>'
    crawl(research, pid, base + '<p>sales@studio-beta.ru</p>', original)
    company = store.one('SELECT id FROM companies')['id']
    store.execute("INSERT INTO company_contacts(company_id,kind,value) VALUES(?,'email',?)",
                  (company, 'info@alpha-shop.ru'))
    fresh = base + '<p>Контакты кафе: info@alpha-shop.ru.</p><footer>Разработка сайта: sales@studio-beta.ru.</footer>'
    crawl(research, pid, fresh, original)

    assert store.query('SELECT value,source_id FROM company_contacts') == [
        {'value': 'info@alpha-shop.ru', 'source_id': None},
    ]
    assert store.one("SELECT id FROM audit_events WHERE event='contact_retired'")


def http_pages(research, pages):
    research.fetcher._check_robots = Mock()
    research.fetcher._request = Mock(side_effect=lambda url, *_args, **_kwargs: pages[url])
    research._force_refresh = True


def html_response(html):
    return 200, {'content-type': 'text/html; charset=utf-8'}, html.encode()


def test_contact_redirect_never_adds_foreign_fit_and_retires_legacy_contact(research):
    store = research.store
    pid = store.ensure_product('кофе')['id']
    store.set_setting('search.page_limit', 2)
    original = 'https://alpha-shop.ru/'
    contacts = original + 'contacts'
    outside = 'https://outside-cafe.ru/'
    root = '<title>Альфа</title><p>Мы продаём компьютеры.</p><a href="/contacts">Контакты</a>'
    cafe = '<title>Бета</title><p>Наше кафе работает.</p><p>info@outside-cafe.ru</p>'
    http_pages(research, {original: html_response(root), contacts: html_response(cafe)})
    result = SearchResult(original, country='Россия')
    research._company(pid, result, False, lambda _: None)
    assert visible_companies(store, pid)[0]['fit_status'] == 'FIT'
    old_contact_source = store.one("SELECT source_id FROM company_contacts WHERE kind='email'")['source_id']
    store.execute('UPDATE sources SET url=? WHERE id=?', (outside, old_contact_source))
    http_pages(research, {original: html_response(root), contacts: (302, {'location': outside}, b''),
                          outside: html_response(cafe)})

    research._company(pid, result, False, lambda _: None)
    assert store.one('SELECT status FROM company_fit')['status'] == 'INSUFFICIENT_EVIDENCE'
    assert store.query('SELECT value FROM company_contacts') == []
    assert store.one("SELECT id FROM audit_events WHERE event='contact_retired'")


def test_company_first_page_rejects_directory_but_keeps_canonical_website(research):
    store = research.store
    pid = store.ensure_product('кофе')['id']
    original = 'http://alpha-shop.ru/'
    canonical_url = 'https://www.alpha-shop.ru/'
    directory_url = 'https://2gis.ru/firm/42'
    cafe = '<title>Альфа</title><p>Наше кафе работает.</p><p>info@alpha-shop.ru</p>'
    http_pages(research, {original: (301, {'location': canonical_url}, b''), canonical_url: html_response(cafe)})
    research._company(pid, SearchResult(original, country='Россия'), False, lambda _: None)
    assert visible_companies(store, pid)[0]['url'] == canonical_url
    redirect_url = 'https://old-buyer.ru/'
    http_pages(research, {redirect_url: (302, {'location': directory_url}, b''), directory_url: html_response(cafe)})
    research._company(pid, SearchResult(redirect_url, country='Россия'), False, lambda _: None)
    row = store.one("SELECT c.url,f.status FROM companies c JOIN company_fit f ON c.id=f.company_id "
                    "WHERE c.domain='old-buyer.ru'")
    assert row == {'url': '', 'status': 'INSUFFICIENT_EVIDENCE'}
    visible = visible_companies(store, pid)
    assert len(visible) == 2
    assert next(row for row in visible if row['domain'] == 'old-buyer.ru')['fit_status'] == 'INSUFFICIENT_EVIDENCE'


@pytest.mark.parametrize('reported', ['Мы не закупаем кофе', 'Кофе отсутствует в ассортименте',
                                      'Компания ликвидирована'])
def test_partner_statement_never_vetoes_company_activity(reported):
    own = 'Наше кафе готовит завтраки.'
    page = {'text': f'Наш партнёр сообщил: «{reported}». {own}'}
    assert evaluate_fit('кофе', [page], {}, 'Россия')['status'] == 'FIT'
    assert semantic_evidence_allowed('кофе', [page], own)


def test_own_refusal_remains_a_decisive_company_statement():
    page = {'text': 'Наше кафе готовит завтраки. Мы не закупаем кофе.'}
    assert evaluate_fit('кофе', [page], {}, 'Россия')['status'] == 'NOT_FIT'
    assert not semantic_evidence_allowed('кофе', [page], 'Наше кафе готовит завтраки.')


@pytest.mark.parametrize('separator', [', но ', ', зато ', ', ', ' и '])
def test_purchase_statement_and_refusal_apply_to_their_own_products(separator):
    positive = 'закупаем кофе ежедневно.'
    page = {'text': 'Мы не закупаем чай' + separator + positive}
    assert evaluate_fit('кофе', [page], {}, 'Россия')['status'] == 'FIT'
    assert evaluate_fit('чай', [page], {}, 'Россия')['status'] == 'NOT_FIT'
    assert semantic_evidence_allowed('кофе', [page], positive)


def test_documented_assortment_does_not_inherit_another_products_denial():
    page = {'text': 'Мы не продаём ноутбуки, но продаём смартфоны оптом.'}
    assert evaluate_fit('смартфон', [page], {}, 'Россия')['status'] == 'FIT'
    assert evaluate_fit('ноутбук', [page], {}, 'Россия')['status'] == 'INSUFFICIENT_EVIDENCE'
    assert semantic_evidence_allowed('смартфон', [page], page['text'])


def test_additive_commerce_clause_keeps_both_products():
    page = {'text': 'Мы продаём не только смартфоны, но и ноутбуки.'}
    assert evaluate_fit('смартфон', [page], {}, 'Россия')['status'] == 'FIT'
    assert evaluate_fit('ноутбук', [page], {}, 'Россия')['status'] == 'FIT'


def test_partner_reporter_without_possessive_does_not_supply_refusal():
    page = {'text': 'Партнёр сообщил: «Мы не закупаем кофе». Наше кафе закупает кофе.'}
    assert evaluate_fit('кофе', [page], {}, 'Россия')['status'] == 'FIT'


def test_company_activity_does_not_include_list_of_clients():
    html = ('<title>Студия</title><p>Мы создаём программы.</p><h2>Клиенты</h2>'
            '<ul><li>Логистика.</li><li><strong>Кафе</strong> Север</li></ul>')
    page = parse_page(html, 'https://alpha-shop.ru/', 'Россия')
    assert evaluate_fit('кофе', [page], {}, 'Россия')['status'] == 'INSUFFICIENT_EVIDENCE'
    assert not semantic_evidence_allowed('кофе', [page], 'Кафе Север')
    own = parse_page(html + '<h2>О нас</h2><p>Наше кафе работает.</p>', 'https://alpha-shop.ru/', 'Россия')
    assert evaluate_fit('кофе', [own], {}, 'Россия')['status'] == 'FIT'


def test_adjacent_local_labels_keep_third_party_contact_and_inn_separate():
    html = ('<p>Контакты кафе: info@alpha-shop.ru</p>'
            '<footer><p>Разработка сайта</p><p><a href="mailto:sales@studio-beta.ru">Студия Бета</a></p></footer>'
            '<div><p><b>Реквизиты платёжного оператора</b></p><p>ИНН 7701234560</p></div>')
    page = parse_page(html, 'https://alpha-shop.ru/', 'Россия')
    assert page['emails'] == ['info@alpha-shop.ru']
    assert page['developer_emails'] == ['sales@studio-beta.ru']
    assert not page['legal_id']
    assert page['external_legal_ids'] == ['7701234560']


def test_resumed_company_task_uses_all_selected_countries_once(research):
    store = research.store
    pid = store.ensure_product('кофе')['id']
    countries = ['Россия', 'Казахстан', 'Беларусь']
    run = store.execute(
        "INSERT INTO job_runs(kind,product_id,status,countries,created_at,updated_at) VALUES('companies',?,'interrupted',?,?,?)",
        (pid, json.dumps(countries, ensure_ascii=False), now(), now()))
    url = 'https://alpha-shop.com/'
    for country in countries:
        research._enqueue(run, {'phase': 'company', 'result': SearchResult(url, country=country).to_dict(),
                                'manual': False})
    assert store.one('SELECT COUNT(*) AS n FROM job_queue')['n'] == 1
    html = ('<title>Альфа</title><p>Наше кафе открыто в Беларуси. Доставляем в Россию.</p>'
            '<p><a href="tel:8 (029) 123-45-67">Позвонить</a></p>')
    page = parse_page(html, url, 'Россия')
    page['source_id'] = store.put_source(url, page['title'], page['text'])
    research._page = Mock(return_value=page)

    result = research._run('companies', pid, countries, [], {}, lambda _: None, None, run_id=run)

    assert result['status'] == 'complete'
    research._page.assert_called_once_with(url, 'Россия')
    assert store.query('SELECT country FROM companies') == [{'country': 'Беларусь'}]
    assert store.query('SELECT value FROM company_contacts') == [{'value': '80291234567'}]
