"""Research regressions: malformed sites, current contacts and article links."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sales_manager.core.contacts import company_emails
from sales_manager.research.extraction import parse_feed, parse_page
from sales_manager.research.providers import SearchResult
from sales_manager.research.safe_http import FetchError, SafeFetcher
from sales_manager.research.service import ResearchService
from sales_manager.storage import Store, now


@pytest.fixture
def service(tmp_path):
    store = Store(tmp_path / 'research.sqlite3')
    service = ResearchService(store, SimpleNamespace(get=lambda _: ''))
    store.set_setting('search.page_limit', 1)
    yield service
    store.close()


def saved_page(service, text='', emails=(), url='https://buyer.ru/'):
    sid = service.store.put_source(url, 'Покупатель', text + ' ' + ' '.join(emails))
    return dict(url=url, title='Покупатель', name='Покупатель', text=text, emails=list(emails),
                phones=[], legal_id='', links=[], source_id=sid)


def crawl(service, page):
    service._page = lambda *_: page
    product = service.store.ensure_product('кофе')
    service._company(product['id'], SearchResult(page['url'], 'Покупатель', country='Россия'),
                     False, lambda _: None)
    return service.store.one('SELECT id FROM companies')['id']


def test_successful_refresh_retires_old_email_but_keeps_history_and_permission(service):
    old = saved_page(service, emails=['old@buyer.ru'])
    cid = crawl(service, old)
    service.store.execute('INSERT INTO permissions(company_id,email,basis,evidence,created_at) VALUES(?,?,?,?,?)',
                          (cid, 'old@buyer.ru', 'Согласие', 'Письмо', now()))
    fresh = saved_page(service, emails=['new@buyer.ru'])
    crawl(service, fresh)
    assert [row['email'] for row in company_emails(service.store, cid)] == ['new@buyer.ru']
    assert service.store.one('SELECT email FROM permissions')['email'] == 'old@buyer.ru'
    assert 'old@buyer.ru' in service.store.one('SELECT text FROM sources WHERE id=?', (old['source_id'],))['text']
    archived = json.loads(service.store.one("SELECT details FROM audit_events WHERE event='contact_retired'")['details'])
    assert archived['value'] == 'old@buyer.ru' and archived['source_id'] == old['source_id']


def test_successful_refresh_keeps_manual_and_unvisited_contacts(service):
    cid = crawl(service, saved_page(service, emails=['old@buyer.ru']))
    other = service.store.put_source('https://buyer.ru/other', 'Другой отдел', 'other@buyer.ru')
    for value, sid in [('manual@buyer.ru', None), ('other@buyer.ru', other)]:
        service.store.execute('INSERT INTO company_contacts(company_id,kind,value,source_id,checked_at) VALUES(?,?,?,?,?)',
                              (cid, 'email', value, sid, now()))
    crawl(service, saved_page(service, emails=['new@buyer.ru']))
    assert {row['email'] for row in company_emails(service.store, cid)} == {
        'new@buyer.ru', 'manual@buyer.ru', 'other@buyer.ru'}


def test_partial_crawl_does_not_retire_contacts(service):
    cid = crawl(service, saved_page(service, emails=['old@buyer.ru']))
    fresh = saved_page(service, emails=['new@buyer.ru'])
    fresh['links'] = ['https://buyer.ru/contacts']
    service.store.set_setting('search.page_limit', 2)

    def fetch(url, _):
        if url == fresh['url']:
            return fresh
        raise FetchError('Временная ошибка')

    service._page = fetch
    service._company(service.store.one('SELECT id FROM products')['id'],
                     SearchResult(fresh['url'], 'Покупатель', country='Россия'), False, lambda _: None)
    assert {row['email'] for row in company_emails(service.store, cid)} == {'old@buyer.ru', 'new@buyer.ru'}


@pytest.mark.parametrize('provider', ['', 'overpass'])
def test_unavailable_first_site_keeps_original_link(service, provider):
    pid = service.store.ensure_product('кофе')['id']
    service._page = Mock(side_effect=FetchError('Сайт недоступен'))
    metadata = {'provider': provider}
    if provider:
        metadata.update(source_url='https://www.openstreetmap.org/node/11', tags={'amenity': 'cafe'})
    service._company(pid, SearchResult('https://offline.ru/', 'Офлайн', country='Россия', metadata=metadata),
                     True, lambda _: None)
    assert service.store.one('SELECT url FROM companies')['url'] == 'https://offline.ru/'


@pytest.mark.parametrize('kind', [None, 17, False, {}, {'Organization': True}, [None, 17, {}]])
def test_malformed_jsonld_type_does_not_hide_contacts(kind):
    html = '<title>Company</title><script type="application/ld+json">' + json.dumps({'@type': kind})
    html += '</script><p>sales@buyer.ru</p>'
    page = parse_page(html, 'https://buyer.ru/')
    assert page['name'] == 'Company'
    assert page['emails'] == ['sales@buyer.ru']


def test_bad_jsonld_on_one_site_does_not_abort_remaining_queue(service):
    pid = service.store.ensure_product('кофе')['id']
    service.fetcher.fetch = lambda url: SimpleNamespace(url=url, text=(
        '<title>First</title><script type="application/ld+json">{"@type":null}</script><p>sales@first.ru</p>'
        if 'first' in url else '<title>Second</title><p>sales@second.ru</p>'))
    tasks = [{'phase': 'company', 'manual': True,
              'result': SearchResult(f'https://{host}.ru/', country='Россия').to_dict()}
             for host in ('first', 'second')]
    result = service._run('companies', pid, ['Россия'], tasks, {}, lambda _: None, None)
    assert result['status'] == 'complete'
    assert service.store.one('SELECT COUNT(*) AS n FROM companies')['n'] == 2
    assert service.store.one("SELECT COUNT(*) AS n FROM job_queue WHERE status='done'")['n'] == 2


@pytest.mark.parametrize('article_first', [True, False])
def test_atom_article_wins_over_image_enclosure(article_first):
    article = '<link rel="alternate" type="text/html" href="https://buyer.ru/article"/>'
    image = '<link rel="enclosure" type="image/jpeg" href="https://buyer.ru/image.jpg"/>'
    xml = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>News</title>'
    xml += article + image if article_first else image + article
    xml += '<link rel="self" href="https://buyer.ru/api/entry"/></entry></feed>'
    assert parse_feed(xml, 'https://buyer.ru/feed')[0].url == 'https://buyer.ru/article'


def test_atom_enclosure_alone_is_not_an_article():
    xml = '<feed><entry><title>News</title><link rel="enclosure" href="https://buyer.ru/image.jpg"/></entry></feed>'
    assert parse_feed(xml, 'https://buyer.ru/feed') == []


@pytest.mark.parametrize('media_type', ['application/xml', 'text/xml', 'application/rss+xml', 'application/atom+xml'])
def test_xml_download_negotiates_supported_media_types(monkeypatch, media_type):
    import sales_manager.research.safe_http as module

    class Connection:
        sock = None

        def __init__(self, *_args, **_kwargs):
            self.accept = ''

        def request(self, method, url, headers):
            self.accept = headers['Accept']

        def getresponse(self):
            pieces = iter([b'<feed/>', b''])
            return SimpleNamespace(status=200 if media_type in self.accept.split(',') else 406,
                                   getheaders=lambda: [('Content-Type', media_type)], read=lambda _: next(pieces))

        def close(self):
            pass

    monkeypatch.setattr(module, 'public_addresses', lambda *_: ['93.184.215.14'])
    monkeypatch.setattr(module.http.client, 'HTTPConnection', Connection)
    fetcher = SafeFetcher()
    monkeypatch.setattr(fetcher, '_check_robots', lambda _: None)
    assert fetcher.fetch_document('http://buyer.ru/feed.xml').text == '<feed/>'


@pytest.mark.parametrize('status,potential,a', [('FIT', 0, 1), ('NOT_FIT', 0, 0), ('INSUFFICIENT_EVIDENCE', 1, None)])
def test_recrawl_preserves_explicit_manual_decision(service, status, potential, a):
    cid = crawl(service, saved_page(service, text='Россия'))
    service.store.execute('UPDATE company_fit SET status=?,reason=?,fit_key=?,manual=1,a=?,potential=?',
                          (status, 'Решение пользователя: проверено', 'manual:accepted', a, potential))
    fresh = saved_page(service, text='Россия. Мы гостиница.', emails=['sales@buyer.ru'])
    crawl(service, fresh)
    fit = service.store.one('SELECT * FROM company_fit WHERE company_id=?', (cid,))
    assert (fit['status'], fit['potential'], fit['a']) == (status, potential, a)
    assert fit['fit_key'] == 'manual:accepted'
    assert fit['reason'] == 'Решение пользователя: проверено'
    assert fresh['source_id'] in json.loads(fit['source_ids'])


@pytest.mark.parametrize('changed,expected', [
    ('Новости. Мы комплектуем учебные аудитории для университетов.', 'FIT'),
    ('Мы комплектуем учебные аудитории для университетов. Офисные кресла не закупаем.', 'NOT_FIT'),
    ('Наши клиенты: Мы комплектуем учебные аудитории для университетов.', 'INSUFFICIENT_EVIDENCE'),
])
def test_semantic_relationship_survives_layout_changes_but_not_changed_provenance(service, changed, expected):
    from sales_manager.research.fit_rules import evaluate_fit

    quote = 'Мы комплектуем учебные аудитории для университетов.'
    product = service.store.ensure_product('офисное кресло')
    first = saved_page(service, text=quote, emails=['sales@buyer.ru'])
    service._page = lambda *_: first
    result = SearchResult(first['url'], 'Покупатель', country='Россия')
    service._company(product['id'], result, False, lambda _: None)
    assert evaluate_fit(product['name'], [first], {}, 'Россия')['status'] == 'INSUFFICIENT_EVIDENCE'
    reason = 'Связь: оснащение учебных аудиторий. Основание: «' + quote + '»'
    service.store.execute("UPDATE company_fit SET status='FIT',reason=?,ai_checked_key='semantic-v6:accepted'", (reason,))
    fresh = saved_page(service, text=changed, emails=['sales@buyer.ru'])
    service._page = lambda *_: fresh
    service._company(product['id'], result, False, lambda _: None)
    fit = service.store.one('SELECT * FROM company_fit')
    assert fit['status'] == expected
    if expected == 'FIT':
        assert fit['reason'] == reason
