"""Regressions discovered while exercising real article and queue paths."""
import json
from datetime import datetime, timezone
from unittest.mock import Mock

from sales_manager.research.providers import SearchResult, RSSProvider
from sales_manager.research.service import ResearchService, Paused
from sales_manager.storage import Store, now


def test_article_validation_does_not_inherit_unrelated_countries_from_navigation(tmp_path):
    store = Store(tmp_path / 'article.sqlite3')
    research = ResearchService(store, Mock(get=lambda *a: ''))
    stamp = datetime.now(timezone.utc).isoformat()
    article = f'В России за {stamp[:7]} продажи смартфонов составили 500 штук.'
    clutter = 'Новости США. Экономика Китая. ' + article
    sid = store.put_source('https://paper.test/story', text=clutter)
    research._page = lambda *a: dict(url='https://paper.test/story', source_id=sid, market_text=article)
    research._market_evidence(SearchResult('https://paper.test/story', 'Продажи смартфонов', '', 'Россия', stamp), lambda _: None)
    rows = research.market_ranking(['Россия'])['rows']
    assert len(rows) == 1 and rows[0]['name'] == 'смартфон'
    assert rows[0]['measurements'][0]['value'] == 500
    proof = store.one('SELECT text FROM sources WHERE id=?', (rows[0]['source_ids'][0],))['text']
    assert 'Китая' not in proof and 'США' not in proof
    assert store.one('SELECT text FROM sources WHERE id=?', (sid,))['text'] == clutter
    store.close()


def test_newly_known_country_updates_identity_without_losing_history(tmp_path):
    store = Store(tmp_path / 'identity.sqlite3')
    research = ResearchService(store, Mock(get=lambda *a: ''))
    first = research._save_company('Покупатель', 'buyer.com', 'https://buyer.com/', '', '1234567890', True)
    product = store.ensure_product('Кофе')
    oid = store.execute('INSERT INTO offers(product_id,company_id,subject,body,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                        (product['id'], first, 'Сохранено', 'Правка пользователя', now(), now()))
    second = research._save_company('Покупатель', 'buyer.com', 'https://buyer.com/', 'Россия', '1234567890', True)
    assert first == second
    assert store.one('SELECT COUNT(*) n FROM companies')['n'] == 1
    assert store.one('SELECT country FROM companies')['country'] == 'Россия'
    assert store.one('SELECT body FROM offers WHERE id=?', (oid,))['body'] == 'Правка пользователя'
    other = research._save_company('Другое юрлицо', 'buyer.com', 'https://buyer.com/', 'Россия', '5555555555', True)
    assert other != first
    store.close()


def test_successful_rss_response_counts_even_if_later_feed_is_interrupted(tmp_path, monkeypatch):
    store = Store(tmp_path / 'rss.sqlite3')
    research = ResearchService(store, Mock(get=lambda *a: ''))
    calls = []

    def document(url):
        calls.append(url)
        if len(calls) == 2:
            raise Paused('User pause')
        return dict(url=url, text='<rss><channel/></rss>')

    feed = RSSProvider(document, ['https://one.test/rss', 'https://two.test/rss'])
    feed.on_response = lambda: research._source_responded('rss')
    result = research._run('market', None, ['Россия'],
                          [dict(phase='search', provider='rss', country='Россия', query='*', discover=True, market=True)],
                          {'rss': feed}, lambda _: None, None)
    assert result['status'] == 'interrupted' and result['sources_used'] == 1
    assert result['pending'] == 1
    store.close()


def test_skipped_source_can_be_enabled_later_without_repeating_completed_work(tmp_path):
    store = Store(tmp_path / 'tasks.sqlite3')
    research = ResearchService(store, Mock(get=lambda *a: ''))
    run = store.execute('INSERT INTO job_runs(kind,status,countries,created_at,updated_at) VALUES(?,?,?,?,?)',
                        ('market', 'paused', '[]', now(), now()))
    task = dict(phase='search', provider='tavily', query='Спрос на кофе', country='Россия', market=True)
    research._enqueue(run, task)
    store.execute("UPDATE job_queue SET status='done',error='Пропущено: источник отключён'")
    research._enqueue(run, task)
    assert store.one('SELECT status FROM job_queue')['status'] == 'pending'
    store.execute("UPDATE job_queue SET status='done',error=''")
    research._enqueue(run, task)
    assert store.one('SELECT status FROM job_queue')['status'] == 'done'
    assert store.one('SELECT COUNT(*) n FROM job_queue')['n'] == 1
    store.close()
