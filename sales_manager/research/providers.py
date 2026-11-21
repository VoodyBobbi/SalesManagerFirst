"""Bounded source adapters. Search output is evidence, never invented demand."""
import atexit
import json
import math
import re
import ssl
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from dataclasses import asdict, dataclass, field
from urllib.parse import quote, urlencode, urlsplit

import httpx

from .normalization import COUNTRY_CODES, COUNTRY_QIDS, osm_filters
from .safe_http import USER_AGENT, FetchError

KEYED_PROVIDERS = {'tavily': 'Tavily', 'serper': 'Serper', 'exa': 'Exa',
                   'firecrawl': 'Firecrawl', 'jina': 'Jina Search'}
KEYLESS_PROVIDERS = {'overpass': 'OpenStreetMap / Overpass', 'gdelt': 'GDELT',
                     'wikidata': 'Wikidata', 'mediawiki': 'Wikipedia / MediaWiki', 'rss': 'RSS',
                     'catalog': 'Публичные каталоги', 'procurement': 'Публикации закупок',
                     'comtrade': 'UN Comtrade — импорт товаров'}
NO_SOURCES_MESSAGE = ('Нет доступных источников поиска. Включите хотя бы один открытый источник в настройках')
OVERPASS_URL = 'https://maps.mail.ru/osm/tools/overpass/api/interpreter'
OVERPASS_ENDPOINTS = (OVERPASS_URL, 'https://overpass-api.de/api/interpreter',
                      'https://overpass.private.coffee/api/interpreter')
OVERPASS_HOSTS = frozenset(urlsplit(url).hostname for url in OVERPASS_ENDPOINTS)

class ProviderError(RuntimeError):
    status = 'Недоступен'
    def __init__(self, message, status=None, retry_after=120):
        super().__init__(message)
        self.status = status or type(self).status
        self.retry_after = retry_after

class NoSourcesError(ProviderError):
    def __init__(self, reason=''):
        super().__init__(reason or NO_SOURCES_MESSAGE)

class MissingKeyError(ProviderError):
    status = 'Нет ключа'

class InvalidKeyError(ProviderError):
    status = 'Неверный ключ'

class QuotaError(ProviderError):
    status = 'Квота закончилась'

class TransientProviderError(ProviderError):
    """A request can be repeated after the provider cooldown."""

class RateLimitError(TransientProviderError):
    status = 'Временное ограничение'

class InvalidResponseError(ProviderError):
    pass

def retry_seconds(value, default=60):
    try:
        return max(1, float(value))
    except (TypeError, ValueError):
        try:
            return max(1, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return default

def check_response(status, data, headers=None):
    """Never log a provider body: it may echo credentials or the request."""
    headers = headers or {}
    body = json.dumps(data, ensure_ascii=False).casefold() if data else ''
    quota = any(word in body for word in ('quota_exceeded', 'insufficient credits', 'not enough credits',
                 'no credits remaining', 'credits exhausted', 'quota exhausted', 'usage limit exceeded',
                 'monthly limit', 'credit balance', 'run out of credits'))
    if status in (402, 432, 433) or (status >= 400 and quota):
        raise QuotaError('Бесплатный лимит исчерпан. Продолжаем поиск через другие источники.', retry_after=86400)
    if status == 401:
        raise InvalidKeyError('Ключ неверный или отозван. Замените его в настройках.', retry_after=86400)
    if status == 429:
        delay = retry_seconds(headers.get('retry-after') or headers.get('Retry-After'))
        raise RateLimitError('Временное ограничение запросов. Источник отложен; используем другие.', retry_after=delay)
    if status == 403:
        if any(word in body for word in ('invalid api key', 'invalid_api_key', 'unauthorized api key')):
            raise InvalidKeyError('Ключ неверный или отозван. Замените его в настройках.', retry_after=86400)
        raise ProviderError('Доступ запрещён. Проверьте права ключа и ограничения сервиса.')
    if status >= 500:
        raise TransientProviderError('Сервер временно недоступен. Найденное сохранено; используем другие источники.')
    if status >= 400:
        raise ProviderError('Источник отклонил запрос. Проверьте настройки подключения.')
    error = data.get('error') if isinstance(data, dict) else None
    if error:
        code = error.get('code', '') if isinstance(error, dict) else str(error)
        if code in ('ratelimited', 'maxlag'):
            raise RateLimitError('Источник временно ограничил запросы.', retry_after=60)
        raise InvalidResponseError('Источник вернул ошибку вместо результатов.')

def rows(data, key):
    value = data.get(key)
    if not isinstance(value, list):
        raise InvalidResponseError('Некорректный ответ: отсутствует список результатов.')
    return value

@dataclass
class SearchResult:
    url: str
    title: str = ''
    snippet: str = ''
    country: str = ''
    published_at: str = ''
    metadata: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)

_http_client = None
_http_client_lock = threading.Lock()


def _provider_client():
    global _http_client
    with _http_client_lock:
        if _http_client is not None:
            return _http_client
        try:
            import truststore
            context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except ImportError:
            context = ssl.create_default_context()
        _http_client = httpx.Client(timeout=httpx.Timeout(30, connect=10, pool=5),
                                   limits=httpx.Limits(max_connections=8, max_keepalive_connections=8),
                                   follow_redirects=False, verify=context, trust_env=False,
                                   headers={'User-Agent': USER_AGENT})
        return _http_client


@atexit.register
def close_provider_http():
    global _http_client
    with _http_client_lock:
        if _http_client is not None:
            _http_client.close()
            _http_client = None


def json_http(method, url, **kwargs):
    """Only fixed provider endpoints are passed here; page URLs use SafeFetcher."""
    try:
        with _provider_client().stream(method, url, **kwargs) as response:
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > 6_000_000:
                    raise ProviderError('Ответ источника превышает допустимый размер.')
            try:
                value = json.loads(data)
            except (ValueError, UnicodeError) as exc:
                check_response(response.status_code, {}, response.headers)
                raise InvalidResponseError('Источник вернул некорректный JSON; данные не добавлены.') from exc
            if response.status_code in (401, 403) and url.startswith('https://api.firecrawl.dev/') and not kwargs.get('headers', {}).get('Authorization'):
                raise MissingKeyError('Firecrawl не разрешает поиск без ключа с этого IP. Используйте другие открытые источники.')
            check_response(response.status_code, value, response.headers)
            if not isinstance(value, dict):
                raise InvalidResponseError('Неожиданный формат ответа источника.')
            return value
    except httpx.HTTPError as exc:
        raise TransientProviderError('Источник временно недоступен или не прошёл проверку TLS.') from exc

class ExaProvider:
    name = 'exa'

    def __init__(self, key, request=json_http):
        self.key, self.request = key, request

    def search(self, query, country, limit=10):
        if not self.key:
            raise MissingKeyError('Ключ Exa не задан. Добавьте его в настройках.')
        data = self.request('POST', 'https://api.exa.ai/search',
                            headers={'x-api-key': self.key},
                            json={'query': query, 'type': 'lite', 'numResults': min(limit, 10),
                                  'userLocation': COUNTRY_CODES.get(country, 'RU'),
                                  'contents': {'text': {'maxCharacters': 5000}}})
        return [SearchResult(r['url'], r.get('title', ''), r.get('text', '')[:5000], country,
                             r.get('publishedDate', ''), {'provider': self.name})
                for r in rows(data, 'results') if isinstance(r, dict) and r.get('url')]

class FirecrawlProvider:
    name = 'firecrawl'

    def __init__(self, key='', request=json_http):
        self.key, self.request = key, request

    def search(self, query, country, limit=10):
        kwargs = {'json': {'query': query, 'limit': min(limit, 10), 'sources': ['web'],
                           'country': COUNTRY_CODES.get(country, 'RU'), 'location': country,
                           'highlights': False, 'safe': True}}
        # An omitted header is important: Bearer with an empty token is invalid.
        if self.key:
            kwargs['headers'] = {'Authorization': 'Bearer ' + self.key}
        data = self.request('POST', 'https://api.firecrawl.dev/v2/search', **kwargs)
        if data.get('success') is False:
            raise ProviderError('Firecrawl не выполнил запрос.')
        results = data.get('data')
        if isinstance(results, dict):
            results = results.get('web')
        if not isinstance(results, list):
            raise InvalidResponseError('Firecrawl вернул некорректный список результатов.')
        return [SearchResult(r['url'], r.get('title', ''), r.get('description', '')[:5000], country,
                             r.get('date', ''), {'provider': self.name})
                for r in results if isinstance(r, dict) and r.get('url')]

class GDELTProvider:
    name = 'gdelt'
    # DOC 2.0 searches machine-translated English articles. Russian query
    # words combined with AND yielded almost no matching publications.
    # https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/
    TOPICS = {
        'электроника': 'electronics OR smartphones OR laptops',
        'бытовая техника': 'appliances OR refrigerators OR "vacuum cleaners"',
        'электрика и освещение': 'lighting OR cables',
        'строительные материалы': 'cement OR bricks OR insulation',
        'отделка и ремонт': 'paint OR tiles OR laminate',
        'сантехника': 'plumbing OR pumps OR radiators',
        'инструменты': 'tools OR drills', 'металлоизделия': 'steel OR bearings',
        'промышленное оборудование': 'machinery OR compressors',
        'автотовары': 'tires OR "auto parts"', 'транспорт и склад': 'bicycles OR scooters',
        'мебель': 'furniture OR chairs OR mattresses',
        'дом и кухня': 'tableware OR bedding', 'одежда и текстиль': 'clothing OR footwear',
        'детские товары': 'toys OR strollers', 'спорт и отдых': 'sports OR fitness',
        'канцелярия и офис': 'stationery OR paper', 'упаковка': 'packaging OR cardboard',
        'клининг и гигиена': 'detergents OR hygiene',
        'косметика и уход': 'cosmetics OR perfume', 'украшения': 'jewelry OR watches',
        'продукты питания': 'food OR coffee OR milk',
        'сельское хозяйство': 'seeds OR greenhouses', 'зоотовары': '"pet food"',
        'торговое оборудование': '"retail equipment"',
        'общественное питание': '"kitchen equipment"',
        'полиграфия и творчество': 'books OR printing',
        'энергосбережение': '"solar panels" OR insulation',
    }
    GOODS = {'смартфон': 'smartphones', 'ноутбук': 'laptops', 'кофе': 'coffee',
             'офисное кресло': '"office chairs"', 'картонная коробка': '"cardboard boxes"',
             'бумажное полотенце': '"paper towels"'}

    def __init__(self, request=json_http):
        self.request = request

    @classmethod
    def topic_for(cls, query):
        from .normalization import clean_text, normalize_product, product_candidates
        original = clean_text(query)
        for category, words in cls.TOPICS.items():
            if original == category or original.startswith(category + ' '):
                return words
        if not original:
            return ''
        try:
            if topic := cls.GOODS.get(normalize_product(query)):
                return topic
        except ValueError:
            pass
        candidates = product_candidates(original)
        known = [word for product, word in cls.GOODS.items() if product in candidates]
        if len(known) == 1:
            return known[0]
        if re.fullmatch(r'[a-z0-9 -]{3,100}', original) and re.search(r'[a-z]{3}', original):
            return '"' + original + '"'
        return ''

    def search(self, query, country, limit=10):
        # The DOC index searches English translations, also for Russian outlets.
        topic = self.topic_for(query)
        if not topic:
            return []
        query = '(' + topic + ') (demand OR sales OR purchases OR orders) sourcelang:russian'
        data = self.request('GET', 'https://api.gdeltproject.org/api/v2/doc/doc',
                            params={'query': query, 'mode': 'ArtList', 'format': 'json',
                                    'timespan': '3months', 'maxrecords': min(limit, 30), 'sort': 'DateDesc'})
        return [SearchResult(r['url'], r.get('title', ''), r.get('title', ''), country,
                             r.get('seendate', ''), {'provider': self.name, 'source_country': r.get('sourcecountry', '')})
                for r in rows(data, 'articles') if isinstance(r, dict) and r.get('url')]

def overpass_elements(data):
    if data.get('remark'):
        raise TransientProviderError('Overpass прервал исследование: сервер перегружен или превышен лимит.')
    return rows(data, 'elements')


class OverpassProvider:
    name = 'overpass'

    def __init__(self, request=json_http, *, retry_at=None):
        self.request = request
        self._retry_at = {} if retry_at is None else retry_at

    def search(self, query, country, limit=10):
        filters = osm_filters(query)
        if not filters:
            return []
        code = COUNTRY_CODES.get(country)
        if not code:
            return []
        choices = ''.join(f'nwr["{k}"="{v}"]["name"](area.a);' for k, v in filters)
        statement = (f'[out:json][timeout:20][maxsize:134217728];'
                     f'area["ISO3166-1"="{code}"]["admin_level"="2"]->.a;'
                     f'({choices});out tags {min(limit, 30)};')
        error = None
        for endpoint in OVERPASS_ENDPOINTS:
            if self._retry_at.get(endpoint, 0) > time.monotonic():
                continue
            try:
                data = self.request('POST', endpoint, data={'data': statement})
                elements = overpass_elements(data)
                break
            except ProviderError as exc:
                error = exc
                self._retry_at[endpoint] = time.monotonic() + max(2, exc.retry_after)
        else:
            if error is not None:
                raise error
            raise RateLimitError('Зеркала Overpass временно недоступны.')
        results = []
        for row in elements:
            tags = row.get('tags', {})
            if not tags.get('name'):
                continue
            source_url = f'https://www.openstreetmap.org/{row["type"]}/{row["id"]}'
            website = tags.get('contact:website') or tags.get('website') or ''
            if website and not website.startswith(('http://', 'https://')):
                website = 'https://' + website
            emails = [tags[k] for k in ('contact:email', 'email') if tags.get(k)]
            phones = [tags[k] for k in ('contact:phone', 'phone') if tags.get(k)]
            tag_text = '; '.join(f'{k}: {v}' for k, v in tags.items())
            results.append(SearchResult(website or source_url, tags['name'], tag_text[:8000], country,
                                       metadata={'provider': self.name, 'source_url': source_url,
                                                 'tags': tags, 'emails': emails, 'phones': phones,
                                                 'website': website, 'segment_verified': True}))
        return results

class WikidataProvider:
    name = 'wikidata'

    def __init__(self, request=json_http):
        self.request = request

    def search(self, query, country, limit=10):
        qid = COUNTRY_QIDS.get(country)
        term = re.sub(r'["{}:|]', ' ', query).strip()[:150]
        if not qid or not term:
            return []
        # Search statements, then read the actual official-website/country claims.
        data = self.request('GET', 'https://www.wikidata.org/w/api.php',
                            params={'action': 'query', 'list': 'search', 'srnamespace': 0,
                                    'srsearch': f'{term} haswbstatement:P17={qid} haswbstatement:P856',
                                    'srlimit': min(limit, 10), 'format': 'json'})
        if data.get('error'):
            raise ProviderError('Wikidata не выполнил поиск.')
        ids = [r.get('title', '') for r in rows(data.get('query', {}), 'search') if re.fullmatch(r'Q\d+', r.get('title', ''))]
        if not ids:
            return []
        records = self.request('GET', 'https://www.wikidata.org/w/api.php',
                               params={'action': 'wbgetentities', 'ids': '|'.join(ids),
                                       'props': 'labels|descriptions|claims', 'languages': 'ru|en', 'format': 'json'})
        result = []
        if not isinstance(records.get('entities'), dict):
            raise InvalidResponseError('Wikidata вернула некорректные сведения об организациях.')
        for entity in records['entities'].values():
            claims = entity.get('claims', {})
            values = lambda prop: [x.get('mainsnak', {}).get('datavalue', {}).get('value') for x in claims.get(prop, []) if x.get('rank') != 'deprecated']
            countries = values('P17')
            if not any(isinstance(c, dict) and c.get('id') == qid for c in countries):
                continue
            sites = [x for x in values('P856') if isinstance(x, str) and x.startswith(('http://', 'https://'))]
            if not sites:
                continue
            labels, descs = entity.get('labels', {}), entity.get('descriptions', {})
            label = (labels.get('ru') or labels.get('en') or {}).get('value', entity['id'])
            description = (descs.get('ru') or descs.get('en') or {}).get('value', '')
            result.append(SearchResult(sites[0], label, description, country, metadata={
                'provider': self.name, 'source_url': 'https://www.wikidata.org/wiki/' + entity['id'],
                'website': sites[0], 'entity_id': entity['id']}))
        return result


class TavilyProvider:
    name = 'tavily'
    def __init__(self, key, request=json_http):
        self.key, self.request = key, request

    def search(self, query, country, limit=10):
        if not self.key:
            raise MissingKeyError('Добавьте API-ключ Tavily в настройках.')
        data = self.request('POST', 'https://api.tavily.com/search',
            headers={'Authorization': 'Bearer ' + self.key},
            json={'query': query, 'search_depth': 'basic', 'auto_parameters': False,
                  'max_results': max(1, min(limit, 10)), 'include_answer': False,
                  'include_raw_content': False, 'include_images': False, 'include_usage': True})
        return [SearchResult(r['url'], r.get('title', ''), str(r.get('content', ''))[:5000], country,
                             r.get('published_date', ''), {'provider': self.name})
                for r in rows(data, 'results') if isinstance(r, dict) and r.get('url')]


class SerperProvider:
    name = 'serper'
    def __init__(self, key, request=json_http):
        self.key, self.request = key, request

    def search(self, query, country, limit=10):
        if not self.key:
            raise MissingKeyError('Добавьте API-ключ Serper в настройках.')
        data = self.request('POST', 'https://google.serper.dev/search',
            headers={'X-API-KEY': self.key}, json={'q': query,
                'gl': COUNTRY_CODES.get(country, 'RU').lower(), 'hl': 'ru', 'num': max(1, min(limit, 10))})
        return [SearchResult(r['link'], r.get('title', ''), str(r.get('snippet', ''))[:5000], country,
                             r.get('date', ''), {'provider': self.name})
                for r in rows(data, 'organic') if isinstance(r, dict) and r.get('link')]


class JinaProvider:
    name = 'jina'
    def __init__(self, key, request=json_http):
        self.key, self.request = key, request

    def search(self, query, country, limit=10):
        if not self.key:
            raise MissingKeyError('Jina Search требует API-ключ. Добавьте его в настройках.')
        data = self.request('GET', 'https://s.jina.ai/' + quote(query, safe=''),
            headers={'Authorization': 'Bearer ' + self.key, 'Accept': 'application/json',
                     'X-Respond-With': 'no-content', 'X-Retain-Images': 'none'},
            params={'num': max(1, min(limit, 10))})
        return [SearchResult(r['url'], r.get('title', ''), str(r.get('description', ''))[:5000], country,
                             metadata={'provider': self.name})
                for r in rows(data, 'data')[:limit] if isinstance(r, dict) and r.get('url')]


class MediaWikiProvider:
    name = 'mediawiki'
    def __init__(self, request=json_http):
        self.request = request

    def search(self, query, country, limit=10):
        # Batch extracts and links; no article HTML scraping, no Wikidata inference.
        data = self.request('GET', 'https://ru.wikipedia.org/w/api.php', params={
            'action': 'query', 'generator': 'search', 'gsrsearch': query,
            'gsrnamespace': 0, 'gsrlimit': max(1, min(limit, 10)),
            'prop': 'extracts|info|extlinks', 'exintro': 1, 'explaintext': 1,
            'exchars': 3000, 'inprop': 'url', 'ellimit': 30, 'format': 'json', 'maxlag': 5})
        pages = data.get('query', {}).get('pages', {})
        if not isinstance(pages, dict):
            raise InvalidResponseError('Wikipedia вернула некорректные страницы.')
        result = []
        for page in pages.values():
            if not isinstance(page, dict) or not page.get('fullurl'):
                continue
            # External references are leads only, never asserted to be official sites.
            external = [r.get('*', '') for r in page.get('extlinks', []) if isinstance(r, dict)]
            result.append(SearchResult(page['fullurl'], page.get('title', ''), page.get('extract', '')[:3000],
                country, metadata={'provider': self.name, 'discovery_urls': external[:20]}))
        return result


class RSSProvider:
    name = 'rss'
    # Feeds explicitly published for aggregation on /rss/ or /info/rss.
    # Source links are retained with every item; no full article is republished.
    RELEASE_140_URLS = ['https://www.retail.ru/rss/news/',
                    'https://www.retail.ru/rss/articles/',
                    'https://www.cnews.ru/inc/rss/news_top.xml',
                    'https://www.vedomosti.ru/rss/rubric/business/retail',
                    'https://www.vedomosti.ru/rss/rubric/business/industry',
                    'https://www.vedomosti.ru/rss/rubric/business/agriculture',
                    'https://www.bfm.ru/news.rss',
                    'https://www.interfax.ru/rss.asp']
    # Published aggregation interfaces: https://www.retail.ru/rss/ and
    # https://www.vedomosti.ru/info/rss. A feed remains part of RSS, not a
    # separate search engine in the source counter.
    DEFAULT_URLS = RELEASE_140_URLS + [
        'https://www.retail.ru/rss/press_releases/',
        'https://www.vedomosti.ru/rss/rubric/technology',
        'https://www.vedomosti.ru/rss/rubric/auto']
    RELEASE_139_URLS = ['https://www.retail.ru/rss/news/',
                        'https://www.retail.ru/rss/articles/',
                        'https://www.cnews.ru/inc/rss/news_top.xml',
                        'https://www.vedomosti.ru/rss/rubric/business/retail',
                        'https://www.vedomosti.ru/rss/rubric/business/industry']
    LEGACY_DEFAULT_URLS = ['https://www.cbr.ru/rss/RssPress']
    PREVIOUS_DEFAULT_URLS = ['https://www.retail.ru/rss/news/',
                             'https://www.retail.ru/rss/tovar_na_polku/']

    @classmethod
    def effective_urls(cls, urls=None):
        """Replace the previous application default, preserving custom feed lists."""
        return list(cls.DEFAULT_URLS if urls is None or urls in
                    (cls.LEGACY_DEFAULT_URLS, cls.PREVIOUS_DEFAULT_URLS,
                     cls.RELEASE_139_URLS, cls.RELEASE_140_URLS) else urls)

    def __init__(self, fetch, urls=None):
        self.fetch = fetch
        self.urls = self.effective_urls(urls)
        self.last_errors = {}
        self.last_responded = 0
        self.last_attempted = 0
        self._items = None

    def _load_items(self):
        """Read each configured feed once during a research pass, including failures."""
        from itertools import zip_longest
        from .extraction import parse_feed
        if self._items is not None:
            return self._items
        groups = []
        self.last_errors = {}
        self.last_responded = 0
        urls = list(dict.fromkeys(self.urls))[:16]
        self.last_attempted = len(urls)
        for url in urls:
            try:
                document = self.fetch(url)
                items = parse_feed(document['text'], document['url'])
            except (FetchError, ProviderError) as exc:
                self.last_errors[url] = str(exc)
                continue
            self.last_responded += 1
            callback = getattr(self, 'on_response', None)
            if callable(callback):
                callback()
            groups.append(items)
        # Interleave feeds so a large first feed cannot hide all other publishers.
        self._items = []
        seen = set()
        for batch in zip_longest(*groups):
            for item in batch:
                if item is None or item.url in seen:
                    continue
                seen.add(item.url)
                item.metadata['source_type'] = 'industry_publication'
                self._items.append(item)
        return self._items

    def discover(self, country, limit=200):
        """Discover publications broadly; demand is assessed from their article text."""
        items = self._load_items()
        if not self.last_responded:
            raise ProviderError('RSS недоступны. Проверьте адреса лент и разрешение чтения; используем другие источники.')
        if limit is not None:
            items = items[:max(0, int(limit))]
        # Country here is a search context, never proof of an article's geography.
        return [SearchResult(item.url, item.title, item.snippet, country,
                             item.published_at, dict(item.metadata)) for item in items]

    def search(self, query, country, limit=10):
        from .normalization import CATEGORIES, clean_text, product_candidates, normalize_product
        wanted = set(product_candidates(query))
        # A category query must also find its actual products in short RSS entries.
        category = clean_text(query)
        for label, products in CATEGORIES.items():
            if category == label or (len(category) >= 5 and category in label):
                wanted.update(products)
        normalized_wanted = {normalize_product(product) for product in wanted}
        words = [w[:max(4, len(w)-2)] for w in re.findall(r'[а-яёa-z]{4,}', query.casefold())
                 if w not in ('россия', 'контакты', 'компании', 'официальный', 'спрос', 'рынок', 'новости', 'отчет', 'сайт')]
        output = []
        for item in self.discover(country, limit=None):
            text = (item.title + ' ' + item.snippet).casefold()
            relevant = (not normalized_wanted.isdisjoint(product_candidates(text)) if normalized_wanted
                        else any(word in text for word in words))
            if relevant:
                output.append(item)
                if len(output) >= limit:
                    break
        return output[:limit]


class ComtradeProvider:
    """Small no-key official import statistics, not retail sales.

    Public preview contract: https://comtradeplus.un.org/en/api-docs.
    Commodity codes follow UNSD HS: 0901 coffee, 0902 tea, 851713 smartphones.
    Only an exact known goods group is queried; no broad code is relabelled as
    an arbitrary user-entered product. Each query makes at most one API call.
    """
    name = 'comtrade'
    ENDPOINT = 'https://comtradeapi.un.org/public/v1/preview/C/A/HS'
    REPORTERS = {'Россия': 643, 'Беларусь': 112, 'Казахстан': 398, 'Армения': 51,
                 'Азербайджан': 31, 'Кыргызстан': 417, 'Молдова': 498,
                 'Таджикистан': 762, 'Туркменистан': 795, 'Узбекистан': 860}
    GOODS = {'смартфон': '851713', 'кофе': '0901', 'чай': '0902'}

    def __init__(self, request=json_http, year=None):
        self.request = request
        self.year = str(year if year is not None else datetime.now(timezone.utc).year - 1)

    @classmethod
    def commodity(cls, query):
        from .normalization import normalize_product
        canonical = normalize_product(query)
        if canonical in cls.GOODS:
            return canonical, cls.GOODS[canonical]
        return None

    @staticmethod
    def _positive(value):
        return (type(value) in (int, float) and math.isfinite(value) and value > 0)

    def search(self, query, country, limit=10):
        goods = self.commodity(query)
        reporter = self.REPORTERS.get(country)
        if not goods or reporter is None:
            return []
        product, code = goods
        params = {'reporterCode': reporter, 'period': self.year, 'partnerCode': 0,
                  'cmdCode': code, 'flowCode': 'M', 'maxRecords': min(max(limit, 1), 10)}
        data = self.request('GET', self.ENDPOINT, params=params)
        if data.get('error'):
            raise InvalidResponseError('UN Comtrade вернул ошибку вместо статистики.')
        output = []
        source_url = self.ENDPOINT + '?' + urlencode(params)
        for row in rows(data, 'data'):
            if not isinstance(row, dict):
                raise InvalidResponseError('UN Comtrade вернул некорректную запись статистики.')
            if (str(row.get('reporterCode')) != str(reporter)
                    or str(row.get('period')) != self.year
                    or str(row.get('cmdCode')) != code
                    or row.get('flowCode') != 'M'
                    or str(row.get('partnerCode')) != '0'):
                continue
            measurements = []
            for field, unit in (('primaryValue', 'USD'), ('netWgt', 'kg')):
                value = row.get(field)
                if not self._positive(value):
                    continue
                # Do not substitute arbitrary quantity units or call imports sales.
                excerpt = f'Импорт товара «{product}» в {country} за {self.year}: {value} {unit}.'
                measurements.append({'product': product, 'metric': 'imports', 'value': value,
                    'unit': unit, 'period': self.year, 'country': country, 'published_at': '',
                    'source_url': source_url, 'excerpt': excerpt})
            if measurements:
                snippet = ' '.join(m['excerpt'] for m in measurements)
                output.append(SearchResult(source_url, f'UN Comtrade: импорт {product}, {country}, {self.year}',
                    snippet, country, metadata={'provider': self.name, 'product': product,
                        'official_statistics': True, 'measurements': measurements,
                        'classification': row.get('classificationCode', 'HS'), 'commodity_code': code}))
        return output[:limit]


class PublicPagesProvider:
    """User-configured allowed catalog/procurement pages; no invented APIs."""
    def __init__(self, name, urls, fetch):
        self.name, self.urls, self.fetch = name, urls, fetch

    def search(self, query, country, limit=10):
        if not self.urls:
            raise ProviderError('Добавьте разрешённые публичные URL этого источника в настройках.')
        results, responded = [], False
        errors = []
        for url in self.urls[:3]:
            try:
                page = self.fetch(url, country)
            except FetchError as exc:
                errors.append(str(exc))
                continue
            responded = True
            results.append(SearchResult(page['url'], page['title'], page['text'][:5000], country,
                metadata={'provider': self.name, 'discovery_urls': page.get('external_links', []),
                          'publication': True}))
        if not responded:
            raise ProviderError('Публичные страницы недоступны или запрещают чтение. Проверьте URL источника.')
        return results[:limit]
