"""Opt-in real endpoint checks. No keys, mail, GigaChat or private database."""
import concurrent.futures
import json
import ssl
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
import truststore
from sales_manager.research.providers import (check_response, ProviderError, RSSProvider,
                                            ComtradeProvider, OVERPASS_URL)
from sales_manager.research.extraction import parse_feed
from sales_manager.research.safe_http import SafeFetcher, USER_AGENT


def probe(name):
    output = {'provider': name, 'checked_at': datetime.now(timezone.utc).isoformat(), 'ok': False}
    try:
        if name == 'rss':
            feeds = []
            for url in RSSProvider.DEFAULT_URLS:
                try:
                    result = SafeFetcher(timeout=12).fetch_document(url)
                    count = len(parse_feed(result.text, result.url))
                    feeds.append({'endpoint': result.url, 'http': result.status, 'ok': True, 'results': count})
                except Exception as exc:
                    feeds.append({'endpoint': url, 'ok': False, 'error': str(exc)[:180]})
            working = [feed for feed in feeds if feed['ok']]
            return dict(output, ok=bool(working), results=sum(feed['results'] for feed in working),
                        format='RSS/XML', status='Работает' if working else 'Недоступен', feeds=feeds)
        with httpx.Client(timeout=httpx.Timeout(22, connect=8), trust_env=False,
                verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
                headers={'User-Agent': USER_AGENT}, follow_redirects=False) as client:
            if name == 'firecrawl':
                response = client.post('https://api.firecrawl.dev/v2/search', json={
                    'query': 'кофе Россия', 'limit': 1, 'sources': ['web'], 'highlights': False, 'safe': True})
            elif name == 'gdelt':
                response = client.get('https://api.gdeltproject.org/api/v2/doc/doc', params={
                    'query': 'Russia demand', 'mode': 'ArtList', 'format': 'json', 'maxrecords': 5, 'timespan': '7d'})
            elif name == 'overpass':
                response = client.post(OVERPASS_URL, data={
                    'data': '[out:json][timeout:10];node(55.750,37.610,55.752,37.612)[shop];out tags 3;'})
            elif name == 'mediawiki':
                response = client.get('https://ru.wikipedia.org/w/api.php', params={
                    'action': 'query', 'generator': 'search', 'gsrsearch': 'кофе', 'gsrlimit': 1,
                    'prop': 'extracts|info|extlinks', 'exintro': 1, 'explaintext': 1,
                    'exchars': 300, 'inprop': 'url', 'ellimit': 10, 'format': 'json', 'maxlag': 5})
            elif name == 'comtrade':
                response = client.get(ComtradeProvider.ENDPOINT, params={
                    'reporterCode': 398, 'period': datetime.now(timezone.utc).year - 1,
                    'partnerCode': 0, 'cmdCode': '0901', 'flowCode': 'M', 'maxRecords': 1})
            else:
                response = client.get('https://www.wikidata.org/w/api.php', params={
                    'action': 'wbsearchentities', 'search': 'Россия', 'language': 'ru', 'format': 'json', 'limit': 1})
            output.update(http=response.status_code, endpoint=str(response.request.url).split('?')[0])
            try:
                data = response.json()
            except ValueError:
                check_response(response.status_code, {}, response.headers)
                return dict(output, status='Недоступен', error='Некорректный JSON')
            check_response(response.status_code, data, response.headers)
            if name == 'firecrawl':
                valid = isinstance(data.get('data'), dict) and isinstance(data['data'].get('web'), list) and data.get('success') is not False
                count = len(data.get('data', {}).get('web', []))
            elif name == 'overpass':
                valid = isinstance(data.get('elements'), list) and not data.get('remark')
                count = len(data.get('elements', []))
            elif name == 'gdelt':
                valid = isinstance(data.get('articles'), list)
                count = len(data.get('articles', []))
            elif name == 'mediawiki':
                valid = 'query' in data or 'batchcomplete' in data
                count = len(data.get('query', {}).get('pages', {}))
            elif name == 'comtrade':
                # Exercise the same adapter validation and measurement typing.
                parsed = ComtradeProvider(lambda *args, **kwargs: data).search('кофе', 'Казахстан', 1)
                valid = isinstance(data.get('data'), list) and not data.get('error')
                count = len(data['data'])
                output['measurements'] = [m for result in parsed for m in result.metadata['measurements']]
            else:
                valid = isinstance(data.get('search'), list)
                count = len(data.get('search', []))
            return dict(output, ok=valid, status='Работает' if valid else 'Недоступен',
                        results=count, format='JSON', error='' if valid else 'Некорректный формат ответа')
    except ProviderError as exc:
        return dict(output, status=exc.status, error=str(exc), retry_after=exc.retry_after)
    except Exception as exc:
        return dict(output, status='Недоступен', error=type(exc).__name__ + ': ' + str(exc)[:180])


if __name__ == '__main__':
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        result = list(pool.map(probe, ('firecrawl', 'gdelt', 'overpass', 'wikidata', 'mediawiki', 'rss', 'comtrade')))
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
    print(text)
