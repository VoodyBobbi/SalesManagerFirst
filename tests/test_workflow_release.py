"""One connected product-to-message workflow, with transport boundaries faked."""
import json
import socket
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from sales_manager.ai import AIService
from sales_manager.core.contacts import company_emails
from sales_manager.mail import MailService
from sales_manager.research.providers import SearchResult
from sales_manager.research.service import ResearchService
from sales_manager.storage import Store, now


@pytest.mark.parametrize('selection,fit_method', [('market', 'ai'), ('custom', 'manual')])
def test_market_to_confirmed_send_with_changed_company_email(tmp_path, monkeypatch, selection, fit_method):
    def network_forbidden(*_args, **_kwargs):
        raise AssertionError('This workflow must not use real network or accounts')

    monkeypatch.setattr(socket, 'create_connection', network_forbidden)
    monkeypatch.setattr(socket.socket, 'connect', network_forbidden)
    monkeypatch.setattr(socket, 'getaddrinfo', network_forbidden)
    store = Store(tmp_path / 'workflow.sqlite3')
    secrets = SimpleNamespace(get=lambda key, default='': {
        'gigachat_key': 'synthetic-credential', 'mail_password': 'synthetic-app-password'}.get(key, default))
    research = ResearchService(store, secrets)
    store.set_setting('search.page_limit', 1)
    stamp = datetime.now(timezone.utc).isoformat()
    period = stamp[:7]
    goods = ['офисное кресло', 'смартфон', 'ноутбук', 'монитор', 'принтер',
             'телевизор', 'холодильник', 'пылесос', 'кофемашина', 'компьютер']
    reports, pages = [], {}
    for index, name in enumerate(goods):
        url = f'https://news{index}.ru/report'
        text = f'В России за {period} продано {20000 - index * 1000} шт. товара {name}.'
        reports.append(SearchResult(url, f'Продажи: {name}', text, 'Россия', stamp,
                                    {'provider': 'rss', 'product': name}))
        pages[url] = '<article><h1>Отчёт рынка</h1><p>' + text + '</p></article>'
    rss = SimpleNamespace(discover=lambda *_args, **_kwargs: reports, last_errors=[])
    monkeypatch.setattr(research, '_providers', lambda: {'rss': rss})
    monkeypatch.setattr(research.fetcher, 'fetch', lambda url: SimpleNamespace(url=url, text=pages[url]))

    completion_calls = []
    quote = 'Мы комплектуем учебные аудитории для университетов.'

    def completion(request):
        if request.url.path.endswith('/oauth'):
            return httpx.Response(200, json={'access_token': 'synthetic-token', 'expires_at': int(time.time()) + 1800})
        assert request.url.path.endswith('/chat/completions')
        payload = json.loads(request.content)
        card = json.loads(payload['messages'][-1]['content'])
        source_ids = [source['id'] for source in card['sources']]
        is_fit = 'evidence_quote' in payload['response_format']['schema']['properties']
        completion_calls.append('fit' if is_fit else 'offer')
        content = ({'status': 'FIT', 'reason': 'Гипотеза: товар может использоваться при оснащении учебных аудиторий.',
                    'source_ids': source_ids, 'evidence_quote': quote}
                   if is_fit else
                   {'subject': 'Оснащение учебных аудиторий',
                    'body': 'Предлагаем обсудить товар ' + card['product'] + ' для оснащения учебных аудиторий. '
                            'Гипотеза: такое предложение может соответствовать вашей деятельности. Уточним применимость?',
                    'reason': 'Гипотеза: оснащение учебных аудиторий.', 'source_ids': source_ids})
        return httpx.Response(200, json={
            'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(content, ensure_ascii=False)}}],
            'usage': {'prompt_tokens': 200, 'completion_tokens': 100}})

    client = httpx.Client(transport=httpx.MockTransport(completion))
    ai = AIService(store, secrets, client)
    try:
        assert research.research_market(['Россия'])['status'] == 'complete'
        ranking = research.market_ranking(['Россия'])['rows']
        assert len(ranking) == 10
        assert {row['name'] for row in ranking} == set(goods)
        assert all(row['source_ids'] for row in ranking)
        product = (next(row for row in ranking if row['name'] == 'офисное кресло') if selection == 'market'
                   else store.ensure_product('Стол для учебной аудитории'))
        pid = product['id']

        url = 'https://buyer.ru/'
        pages[url] = '<title>Учебные решения</title><p>' + quote + '</p><p>old@buyer.ru</p>'
        assert research.find_companies(pid, ['Россия'], manual_url=url)['status'] == 'complete'
        company = store.one('SELECT * FROM companies')
        cid = company['id']
        assert company['url'] == url
        assert store.one('SELECT status FROM company_fit')['status'] == 'INSUFFICIENT_EVIDENCE'
        if fit_method == 'ai':
            assert ai.assess_fit(pid, cid)['status'] == 'FIT'
        else:
            store.set_company_fit_manual(pid, cid, 'FIT', 'Проверено: оснащение учебных аудиторий.')

        store.set_setting('sender.signature', 'С уважением, Аргон')
        offer = ai.generate_offer(pid, cid)
        assert offer['approved'] == 0
        assert offer['body'].endswith('С уважением, Аргон')
        assert json.loads(offer['source_ids'])
        store.execute('INSERT INTO mail_accounts(id,email,provider,imap_host,smtp_host,connected) VALUES(1,?,?,?,?,1)',
                      ('owner@sender.ru', 'manual', 'imap.sender.ru', 'smtp.sender.ru'))
        store.execute('INSERT INTO permissions(company_id,email,basis,evidence,created_at) VALUES(?,?,?,?,?)',
                      (cid, 'old@buyer.ru', 'Согласие', 'Согласие прежнего адресата', now()))
        # These writes represent the user's original recipient selection and confirmation.
        store.execute("UPDATE offers SET recipient='old@buyer.ru',approved=1 WHERE id=?", (offer['id'],))
        smtp = Mock()
        smtp.send_message.return_value = {}
        mail = MailService(store, secrets)
        monkeypatch.setattr(mail, '_smtp', Mock(return_value=smtp))
        monkeypatch.setattr(mail, '_append_sent', Mock())

        pages[url] = '<title>Учебные решения</title><p>' + quote + '</p><p>new@buyer.ru</p>'
        research._force_refresh = True
        assert research.find_companies(pid, ['Россия'], manual_url=url)['status'] == 'complete'
        assert store.one('SELECT status FROM company_fit')['status'] == 'FIT'
        assert [row['email'] for row in company_emails(store, cid)] == ['new@buyer.ru']
        assert store.one('SELECT recipient FROM offers')['recipient'] == 'old@buyer.ru'
        with pytest.raises(ValueError, match='действующий email'):
            mail.send_offer(offer['id'])
        mail._smtp.assert_not_called()

        # The user explicitly selects the replacement; it needs its own approval and permission.
        store.execute("UPDATE offers SET recipient='new@buyer.ru',approved=0 WHERE id=?", (offer['id'],))
        with pytest.raises(ValueError, match='подтвердите'):
            mail.send_offer(offer['id'], recipient='new@buyer.ru')
        store.execute('UPDATE offers SET approved=1 WHERE id=?', (offer['id'],))
        with pytest.raises(ValueError, match='основание'):
            mail.send_offer(offer['id'], recipient='new@buyer.ru')
        store.execute('INSERT INTO permissions(company_id,email,basis,evidence,created_at) VALUES(?,?,?,?,?)',
                      (cid, 'new@buyer.ru', 'Согласие', 'Согласие нового адресата', now()))
        expected = dict(subject=offer['subject'], body=offer['body'], recipient='new@buyer.ru', sender='owner@sender.ru')
        assert mail.send_offer(offer['id'], recipient='new@buyer.ru', expected=expected) == 'Отправлено'
        assert mail.send_offer(offer['id'], recipient='new@buyer.ru', expected=expected) == 'Уже отправлено'
        smtp.send_message.assert_called_once()
        sent = smtp.send_message.call_args
        assert sent.kwargs['to_addrs'] == ['new@buyer.ru']
        assert sent.args[0].get_content().replace('\r\n', '\n').rstrip('\n') == offer['body']
        snapshot = store.one("SELECT * FROM mail_messages WHERE direction='outgoing'")
        assert (snapshot['product_id'], snapshot['company_id'], snapshot['recipient'], snapshot['body']) == (
            pid, cid, 'new@buyer.ru', offer['body'])
        assert store.one('SELECT status FROM outbound_attempts')['status'] == 'sent'
        assert store.one("SELECT email FROM permissions WHERE email='old@buyer.ru'")
        assert completion_calls == (['fit', 'offer'] if fit_method == 'ai' else ['offer'])
    finally:
        client.close()
        store.close()
