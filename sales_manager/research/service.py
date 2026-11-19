"""Research orchestration: bounded requests, persistent queues and real evidence."""
import hashlib
import json
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from .extraction import (domain_of, emails_in, is_directory, is_marketplace, legal_id_roles,
                         marketplace_seller_hint, parse_page, phone_normalize, external_links, sitemap_links)
from .normalization import (CATEGORIES, COUNTRIES, buyer_segments, clean_text, branch_identity,
                            company_identity, company_identity_key, matches_product, normalize_product,
                            organization_key, product_candidates)
from sales_manager.core.contacts import email_purpose
from .providers import (ExaProvider, FirecrawlProvider, GDELTProvider, OverpassProvider,
                        ProviderError, QuotaError, SearchResult, TransientProviderError, WikidataProvider, json_http,
                        TavilyProvider, SerperProvider, JinaProvider, MediaWikiProvider, RSSProvider,
                        KEYED_PROVIDERS, NoSourcesError, MissingKeyError, PublicPagesProvider,
                        ComtradeProvider, RateLimitError)
from .safe_http import FetchError, SafeFetcher, validate_url
from .quota import fetch_quota
from .registry import ADAPTERS, market_source_names
from .reporting import market_summary
from .access import credential_hash, verify_free_access, free_access_valid
from .market import (MARKET_PERIOD_QUERY, article_text, extract_observations, market_publication_possible,
                     market_ranking, measurement_basis, current_market_fact, market_fact_clause, SENTENCE_BOUNDARY)
from sales_manager.core.company_listing import visible_companies

def now():
    return datetime.now(timezone.utc).isoformat()

def _key(prefix, payload):
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return prefix + hashlib.sha256(raw.encode()).hexdigest()

class Paused(RuntimeError):
    pass


def current_demand_excerpt(product, text):
    """Keep reported current signals, excluding declines and speculative demand.

    A mixed or ambiguous clause is deliberately omitted rather than promoted
    into a positive trend. Publication evidence still remains labelled indirect.
    """
    demand = re.compile(r'спрос|продаж|потреблен|закуп|популяр|заказ', re.I)
    for sentence in re.split(SENTENCE_BOUNDARY, text, flags=re.I):
        if not demand.search(sentence):
            continue
        fragments = [sentence] if len(sentence) <= 1000 else [sentence[max(0, m.start() - 200):m.end() + 240] for m in demand.finditer(sentence)]
        for fragment in fragments:
            if not current_market_fact(fragment):
                continue
            fragment = market_fact_clause(fragment)
            if matches_product(product, fragment) or product in product_candidates(fragment):
                return ' '.join(fragment.split())[:650]
    return ''


class ResearchService:
    def __init__(self, store, secrets):
        self.store, self.secrets = store, secrets
        self.fetcher = SafeFetcher()
        self._lock = threading.Lock()
        self._request_lock = threading.RLock()
        self._cancel = threading.Event()
        self._requests = 0
        self._limit = 100
        self._provider_clock = {}
        self._force_refresh = False
        self._responded = set()
        self._unavailable = set()
        self.fetcher.cancel_check = self._check_cancelled

    def _cache(self, key):
        row = self.store.one('SELECT value FROM cache WHERE key=? AND expires_at>?', (key, time.time()))
        return json.loads(row['value']) if row else None

    def _put_cache(self, key, value, ttl=86400):
        self.store.execute('INSERT INTO cache(key,value,expires_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,expires_at=excluded.expires_at',
                           (key, json.dumps(value, ensure_ascii=False), time.time() + ttl))

    def _check_cancelled(self):
        if self._cancel.is_set():
            raise Paused('Исследование прервано. Незавершённая работа сохранена.')

    def _checkpoint(self):
        with self._request_lock:
            self._check_cancelled()
            if self._requests >= self._limit:
                if getattr(self, '_active_run_id', None) is not None:
                    # The setting bounds a batch, not the whole finite research plan.
                    self._limit += self._batch_size
                else:
                    raise Paused(f'Достигнут лимит {self._limit} запросов.')

    def _take_request(self):
        with self._request_lock:
            self._checkpoint()
            self._requests += 1

    def _market_page_batch(self, run_id, rows, emit):
        """Read at most three distinct hosts; never race a host's robots delay."""
        selected, hosts = [], set()
        for row in rows:
            task = row.get('_payload') or json.loads(row['payload'])
            if task.get('phase') != 'market_page':
                continue
            host = (urlsplit(task['result']['url']).hostname or '').casefold()
            if host in hosts:
                continue
            hosts.add(host)
            selected.append((row, task))
            if len(selected) == 3:
                break
        if len(selected) < 2:
            return False
        for row, _ in selected:
            self.store.execute("UPDATE job_queue SET status='running',error='' WHERE id=?", (row['id'],))
        def read(item):
            row, task = item
            try:
                self._market_evidence(SearchResult(**task['result']), emit)
            except FetchError as exc:
                self.store.execute("UPDATE job_queue SET status='done',error=? WHERE id=?", (str(exc)[:500], row['id']))
            except Paused:
                self.store.execute("UPDATE job_queue SET status='pending' WHERE id=?", (row['id'],))
                raise
            except sqlite3.Error:
                # Storage failures affect the run, rather than one remote page.
                raise
            except Exception as exc:
                self.store.execute("UPDATE job_queue SET status='done',error=? WHERE id=?",
                                   (type(exc).__name__ + ': не удалось обработать страницу', row['id']))
                emit('Не удалось обработать одну публикацию. Продолжаем поиск.')
            else:
                self.store.execute("UPDATE job_queue SET status='done',error='' WHERE id=?", (row['id'],))
            finally:
                self.store.close()
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix='market-pages') as pool:
            # Consume every future even after one failure so no live thread is
            # left writing after the run has been reported as interrupted.
            futures = [pool.submit(read, item) for item in selected]
            failure = None
            for future in futures:
                try:
                    future.result()
                except BaseException as exc:
                    if failure is None or isinstance(exc, Paused):
                        failure = exc
            if failure:
                raise failure
        return [row['id'] for row, _ in selected]

    def _json(self, method, url, **kwargs):
        # Secrets are excluded from cache keys and audit events.
        auth = hashlib.sha256(json.dumps(kwargs.get('headers', {}), sort_keys=True).encode()).hexdigest()
        scoped_run = getattr(self, '_active_run_id', None) if urlsplit(url).hostname in ('api.tavily.com', 'api.exa.ai', 'google.serper.dev', 's.jina.ai', 'api.firecrawl.dev') else None
        key = _key('provider:v3:', [method, url, kwargs.get('json'), kwargs.get('params'), kwargs.get('data'), auth, scoped_run])
        cached = None if self._force_refresh else self._cache(key)
        if cached is not None:
            return cached
        self._checkpoint()
        host = urlsplit(url).hostname
        interval = {'api.firecrawl.dev': 6.1, 'maps.mail.ru': 2.0,
                    'api.gdeltproject.org': 5.1, 'www.wikidata.org': 1.0,
                    'comtradeapi.un.org': 1.1}.get(host, .3)
        wait = interval - (time.monotonic() - self._provider_clock.get(host, 0))
        if wait > 0 and self._cancel.wait(wait):
            raise Paused('Исследование приостановлено.')
        self._take_request()
        self._provider_clock[host] = time.monotonic()
        provider = {'api.exa.ai': 'exa', 'api.firecrawl.dev': 'firecrawl', 'api.tavily.com': 'tavily',
                    'google.serper.dev': 'serper', 's.jina.ai': 'jina', 'maps.mail.ru': 'overpass',
                    'api.gdeltproject.org': 'gdelt', 'www.wikidata.org': 'wikidata',
                    'ru.wikipedia.org': 'mediawiki', 'comtradeapi.un.org': 'comtrade'}.get(host, host)
        # Count real attempts, including failed requests; never infer provider balance.
        self.store.execute('INSERT INTO usage_log(provider,operation,credits,created_at) VALUES(?,?,?,?)',
                           (provider, 'research_request', 0, now()))
        result = json_http(method, url, **kwargs)
        self._put_cache(key, result)
        credits = (result.get('costDollars') or {}).get('total', 0)
        if not isinstance(credits, (int, float)):
            credits = 0
        if credits:
            self.store.execute('UPDATE usage_log SET credits=? WHERE id=(SELECT MAX(id) FROM usage_log WHERE provider=?)', (credits, provider))
        return result

    def _providers(self):
        providers = {}
        for name, spec in ADAPTERS.items():
            factory = globals().get(spec.factory.__name__, spec.factory)
            default = name not in ('catalog', 'procurement') and (not spec.keyed or name == 'firecrawl')
            if not self.store.get_setting(f'search.{name}_enabled', default):
                continue
            if spec.keyed:
                key = self.secrets.get(name + '_key')
                if (name == 'firecrawl' and not key) or self._free_allowed(name, key):
                    providers[name] = factory(key, self._json)
            elif name == 'rss':
                providers[name] = factory(self._document, self.store.get_setting('search.rss_urls', None))
                providers[name].on_response = lambda: self._source_responded('rss')
            elif name in ('catalog', 'procurement'):
                urls = self.store.get_setting('search.' + name + '_urls', [])
                if urls:
                    providers[name] = factory(name, urls, self._page)
            else:
                providers[name] = factory(self._json)
        return providers

    def _free_allowed(self, name, key):
        return isinstance(key, str) and bool(key) and free_access_valid(self.store.get_setting(f'search.{name}_free_access', {}), key)

    def billing_blocked_sources(self, *, keys=None):
        if keys is None:
            keys = {name: self.secrets.get(name + '_key') for name, spec in ADAPTERS.items() if spec.keyed}
        return [name for name, spec in ADAPTERS.items() if spec.keyed
                and self.store.get_setting(f'search.{name}_enabled', name == 'firecrawl')
                and keys.get(name) and not self._free_allowed(name, keys[name])]

    def _access_request(self, method, url, **kwargs):
        # Account inspection is credential-specific and must never use the web cache.
        self._checkpoint()
        self._take_request()
        return json_http(method, url, **kwargs)

    def _verify_access(self, name, key):
        saved = self.store.get_setting(f'search.{name}_free_access', {})
        if free_access_valid(saved, key):
            return saved
        try:
            record = verify_free_access(name, key, self._access_request)
        except ProviderError as exc:
            if key == self.secrets.get(name + '_key'):
                retry_access = isinstance(exc, TransientProviderError) and self._can_refresh_access(name, key)
                self.store.set_setting(f'search.{name}_free_access', {})
                self._record_status(name, exc, access_key=key, retry_access=retry_access)
            raise
        if key != self.secrets.get(name + '_key'):
            raise ProviderError('Ключ источника изменился. Повторите поиск с новым подключением.')
        self.store.set_setting(f'search.{name}_free_access', record)
        if not record['allowed']:
            self._unavailable.add(name)
            self._record_status(name, ProviderError(record['reason']), access_key=key)
        return record

    def _record_status(self, name, error=None, *, access_key=None, retry_access=False):
        key = self.secrets.get(name + '_key') if access_key is None and name in KEYED_PROVIDERS else access_key
        status = {'status': error.status if error else 'Работает',
                  'message': str(error) if error else 'Источник ответил; пустая выдача допустима.',
                  'retry_until': time.time() + error.retry_after if error else 0,
                  'quota': self.store.get_setting(f'search.{name}_free_access', {}).get('remaining'), 'checked_at': now(),
                  'key_hash': credential_hash(key) if key is not None else ''}
        if access_key is not None:
            status['access_verification'] = True
            status['retry_access'] = retry_access
        self.store.set_setting(f'search.{name}_status', status)
        return status

    def provider_status(self, name, *, keys=None):
        status = self.store.get_setting(f'search.{name}_status', {})
        if name in KEYED_PROVIDERS:
            key = self.secrets.get(name + '_key') if keys is None else keys.get(name, '')
            current_key = hashlib.sha256(key.encode()).hexdigest()
            proof = self.store.get_setting(f'search.{name}_free_access', {})
            # Authentication/rate-limit errors are more useful than the missing
            # billing proof that these same errors invalidated.
            if (status.get('key_hash') == current_key and status.get('status') not in ('Работает', 'Не проверен')
                    and status.get('checked_at')
                    and not (proof.get('allowed') is False and status.get('status') == 'Недоступен')):
                return status
            if status.get('key_hash') and status['key_hash'] != current_key and key:
                return {'status': 'Не проверен', 'message': 'Нажмите «Проверить».', 'retry_until': 0, 'quota': None}
            if key and self.store.get_setting(f'search.{name}_enabled', name == 'firecrawl') and not free_access_valid(proof, key):
                return {'status': 'Отключён: оплата не проверена', 'message':
                        proof.get('reason', 'Нажмите «Проверить»: бесплатный режим ещё не подтверждён.'),
                        'retry_until': 0, 'quota': None}
            if not key and name != 'firecrawl':
                return {'status': 'Нет ключа', 'message': 'Добавьте ключ в настройках.', 'retry_until': 0, 'quota': None}
            if status.get('key_hash') != current_key:
                return {'status': 'Не проверен', 'message': 'Нажмите «Проверить».', 'retry_until': 0, 'quota': None}
        return status or {'status': 'Не проверен', 'message': 'Нажмите «Проверить».', 'retry_until': 0, 'quota': None}

    def provider_statuses(self, names=None, *, keys=None):
        """Read each credential once, or use an already available UI snapshot."""
        names = list(ADAPTERS if names is None else names)
        if keys is None:
            keys = {name: self.secrets.get(name + '_key') for name in names if name in KEYED_PROVIDERS}
        return {name: self.provider_status(name, keys=keys) for name in names}

    def configured_sources(self, operation='companies'):
        """Configured adapters, including those temporarily cooling down."""
        return self.available_sources(operation, include_cooldown=True)

    def no_sources_error(self, operation):
        """Report account verification failures for enabled sources and current keys."""
        reasons = []
        for name, spec in ADAPTERS.items():
            if not spec.keyed or operation not in spec.capabilities:
                continue
            if not self.store.get_setting(f'search.{name}_enabled', name == 'firecrawl'):
                continue
            key = self.secrets.get(name + '_key')
            if not key:
                continue
            status = self.store.get_setting(f'search.{name}_status', {})
            if not status.get('access_verification') or status.get('key_hash') != hashlib.sha256(key.encode()).hexdigest():
                continue
            message = status.get('message')
            if message:
                if not message.casefold().startswith(name + ':'):
                    message = f'{name.title()}: {message}'
                reasons.append(message)
        return NoSourcesError('\n'.join(reasons))

    def available_sources(self, operation='companies', include_cooldown=False):
        names = list(self._providers())
        # The background operation can renew an expired proof safely using a
        # non-search account endpoint. Do not block it at the UI start gate.
        for name, spec in ADAPTERS.items():
            if not spec.keyed or not self.store.get_setting(f'search.{name}_enabled', name == 'firecrawl'):
                continue
            key = self.secrets.get(name + '_key')
            if name not in names and self._can_refresh_access(name, key):
                names.append(name)
        if operation == 'market':
            names = [n for n in names if n in market_source_names()]
        else:
            names = [n for n in names if n != 'comtrade']
        return names if include_cooldown else [n for n in names if self.provider_status(n).get('retry_until', 0) <= time.time()]

    def _can_refresh_access(self, name, key):
        """Recognize a previously verified key without granting search access."""
        if not isinstance(key, str) or not key:
            return False
        fingerprint = credential_hash(key)
        proof = self.store.get_setting(f'search.{name}_free_access', {})
        if (isinstance(proof, dict) and proof.get('allowed') is True
                and proof.get('provider') == name and proof.get('key_hash') == fingerprint):
            return True
        status = self.store.get_setting(f'search.{name}_status', {})
        if status.get('key_hash') != fingerprint or status.get('access_verification') is not True:
            return False
        if 'retry_access' in status:
            return status['retry_access'] is True
        # Older releases saved the failure and cooldown without recording
        # whether the account check could be repeated.
        return name == 'tavily' and (status.get('status') == RateLimitError.status or status.get('message') in (
            'Сервер временно недоступен. Найденное сохранено; используем другие источники.',
            'Источник временно недоступен или не прошёл проверку TLS.',
        ))

    def search_mode(self, *, keys=None):
        if keys is None:
            keys = {name: self.secrets.get(name + '_key') for name in KEYED_PROVIDERS}
        extended = any(self.store.get_setting(f'search.{name}_enabled', name == 'firecrawl')
                       and self._free_allowed(name, keys.get(name, ''))
                       for name in KEYED_PROVIDERS)
        return 'Режим поиска: расширенный' if extended else 'Режим поиска: базовый — без API-ключей'

    def _search(self, name, provider, query, country, limit=10, force=False):
        cache_country = '' if name == 'gdelt' else country
        key = _key('search:v1:', [name, clean_text(query), cache_country, limit,
                   hashlib.sha256(self.secrets.get(name + '_key').encode()).hexdigest()])
        cached = None if force or self._force_refresh else self._cache(key)
        if cached is not None:
            self._source_responded(name)
            results = [SearchResult(**result) for result in cached]
            if name == 'gdelt':
                for result in results:
                    result.country = country
            return results
        state = self.provider_status(name)
        if not force and state.get('retry_until', 0) > time.time():
            self._unavailable.add(name)
            raise ProviderError(state.get('message', 'Источник временно отложен.'),
                                state['status'], state['retry_until'] - time.time())
        if name in ADAPTERS and ADAPTERS[name].keyed and isinstance(getattr(provider, 'key', ''), str) and provider.key:
            access = self._verify_access(name, provider.key)
            if not access['allowed']:
                raise ProviderError(access['reason'])
        for attempt in range(2):
            try:
                results = provider.search(query, country, limit=limit)
                break
            except ProviderError as exc:
                self._record_status(name, exc)
                self._unavailable.add(name)
                # A short rate limit can be retried once; long waits stay queued.
                if attempt == 0 and isinstance(exc, RateLimitError) and 0 < exc.retry_after <= 2:
                    if self._cancel.wait(exc.retry_after):
                        raise Paused('Поиск приостановлен.')
                    continue
                raise
        self._source_responded(name)
        self._unavailable.discard(name)
        self._record_status(name)
        # Persist the response before scheduling its pages. Recovery can replay
        # this step after a crash without repeating an already successful query.
        self._put_cache(key, [result.to_dict() for result in results])
        return results

    @staticmethod
    def _web_route(providers):
        """One web search, with fallback only after empty/unavailable output."""
        return [name for name in ('tavily', 'serper', 'exa', 'firecrawl', 'jina') if name in providers]

    def test_provider(self, name):
        if not self._lock.acquire(blocking=False):
            raise ProviderError('Дождитесь паузы или завершения исследования перед проверкой подключения.')
        try:
            name = name.casefold().strip()
            self._cancel = threading.Event()
            self._force_refresh = True
            self._limit, self._requests = 5, 0
            if name == 'tavily' and not self.store.get_setting('search.tavily_enabled', False):
                raise ProviderError('Источник отключён: включите его в настройках.')
            key = self.secrets.get(name + '_key') if name in KEYED_PROVIDERS else ''
            if key:
                access = self._verify_access(name, key)
                if not access['allowed']:
                    raise ProviderError(access['reason'])
                if name == 'tavily':
                    self._record_status(name)
                    return 'Соединение проверено. Tavily подтвердил бесплатный доступ и остаток квоты; поисковый кредит не использован.'
            provider = self._providers().get(name)
            if not provider:
                if name in ('catalog', 'procurement'):
                    raise ProviderError('Укажите разрешённые публичные URL каталога или закупок и включите источник в настройках.')
                if name in KEYED_PROVIDERS and not self.secrets.get(name + '_key'):
                    error = MissingKeyError('Добавьте API-ключ в настройках и включите источник.')
                    self._record_status(name, error)
                    raise error
                raise ProviderError('Источник отключён: включите его в настройках.')
            if name == 'rss':
                # A connection check reads every configured feed. Keep that
                # diagnostic bounded by the actual feed list, not the generic
                # five-request budget intended for single-endpoint adapters.
                self._limit = max(self._limit, len(getattr(provider, 'urls', ())) + 1)
            query = 'кофе' if name in ('overpass', 'wikidata') else 'рынок кофе Россия'
            results = self._search(name, provider, query, 'Россия', limit=1, force=True)
            # Account endpoints report quota independently from local request counts.
            # An unavailable balance endpoint must not turn a successful search into
            # a false authentication failure or an invented zero balance.
            if name in ('tavily', 'firecrawl') and getattr(provider, 'key', ''):
                try:
                    quota = fetch_quota(name, provider.key, self._json)
                except ProviderError:
                    quota = None
                status = self.provider_status(name)
                status['quota'] = quota
                self.store.set_setting(f'search.{name}_status', status)
            suffix = ' Без ключа, бесплатная квота по IP.' if name == 'firecrawl' and not provider.key else ''
            return f'Соединение проверено. Источник вернул результатов: {len(results)}.{suffix}'
        finally:
            self._force_refresh = False
            self._lock.release()

    def _countries(self, countries):
        result = list(dict.fromkeys(c for c in countries if c in COUNTRIES))
        if not result:
            raise ValueError('Выберите хотя бы одну поддерживаемую страну.')
        return result

    def _refresh_access(self, cancel=None):
        # Refresh expired evidence in a background operation, never while painting UI.
        self._cancel = cancel or threading.Event()
        self._requests = 0
        self._limit = max(5, int(self.store.get_setting('search.request_limit', 100)))
        for name, spec in ADAPTERS.items():
            if not spec.keyed or not self.store.get_setting(f'search.{name}_enabled', name == 'firecrawl'):
                continue
            key = self.secrets.get(name + '_key')
            if (self._can_refresh_access(name, key) and not self._free_allowed(name, key)
                    and self.provider_status(name, keys={name: key}).get('retry_until', 0) <= time.time()):
                try:
                    self._verify_access(name, key)
                except ProviderError:
                    # The recorded diagnostic remains available if no other source can run.
                    pass

    def research_market(self, countries, emit=lambda message: None, cancel=None, run_id=None):
        countries = self._countries(countries)
        self._refresh_access(cancel)
        providers = self._providers()
        if not any(name in market_source_names() for name in providers) and not self._saved_run('market', None, countries):
            raise self.no_sources_error('market')
        tasks = []
        eligible = {name: provider for name, provider in providers.items() if name in market_source_names()}
        if 'rss' in eligible:
            tasks.append({'phase': 'search', 'provider': 'rss', 'query': '*:market-v2',
                          'country': countries[0], 'market': True, 'discover': True})
        for country in countries:
            if 'procurement' in eligible:
                tasks.append({'phase': 'search', 'provider': 'procurement', 'query': 'закупки товаров',
                              'country': country, 'market': True})
        # Independent source tasks let a newly authorized API join a saved run.
        # Alternate categories/countries rather than exhausting a single country.
        for category, goods in CATEGORIES.items():
            for country in countries:
                for name in eligible:
                    if name in ('rss', 'procurement'):
                        continue
                    if name in ('gdelt', 'tavily') and country != countries[0]:
                        continue
                    geography = ('(' + ' OR '.join('"' + item + '"' for item in countries) + ')'
                                 if name == 'tavily' else country)
                    query = (f'{category} спрос новости' if name == 'gdelt' else
                             f'{category} {" ".join(goods[:2])} продажи спрос статистика {geography} {MARKET_PERIOD_QUERY}')
                    tasks.append({'phase': 'search', 'provider': name, 'query': query,
                                  'country': '' if name in ('gdelt', 'tavily') else country, 'market': True,
                                  'selected_countries': countries})
        emit('Ищем актуальные товары и факты спроса.')
        return self._run('market', None, countries, tasks, eligible, emit, cancel, run_id=run_id)

    def resume_market(self, run_id, countries, emit=lambda message: None, cancel=None):
        return self.research_market(countries, emit, cancel, run_id=run_id)

    def find_companies(self, product_id, countries, emit=lambda message: None, cancel=None, manual_url=None):
        countries = self._countries(countries)
        product = self.store.one('SELECT * FROM products WHERE id=?', (product_id,))
        if not product:
            raise ValueError('Сначала выберите или добавьте товар.')
        # Preserve the selected model in buyer search queries. The category
        # rules still classify this name through normalize_product as needed.
        product_name = product['name']
        self._refresh_access(cancel)
        providers = self._providers()
        tasks = []
        if manual_url:
            url = validate_url(manual_url.strip())
            tasks.append({'phase': 'company', 'result': SearchResult(url, country=countries[0]).to_dict(), 'manual': True})
        else:
            if not any(name != 'comtrade' for name in providers) and not self._saved_run('companies', product_id, countries):
                raise self.no_sources_error('companies')
            route = self._web_route(providers)
            for country in countries:
                for provider in ('overpass', 'wikidata', 'mediawiki', 'rss', 'catalog', 'procurement'):
                    if provider in providers:
                        tasks.append({'phase': 'search', 'provider': provider, 'query': product_name, 'country': country})
                for segment in buyer_segments(product_name):
                    for provider in route:
                        tasks.append({'phase': 'search', 'provider': provider,
                                      'query': f'{product_name} {segment} официальный сайт контакты {country}', 'country': country})
                if 'gdelt' in providers:
                    tasks.append({'phase': 'search', 'provider': 'gdelt', 'query': f'{product_name} компании {country}', 'country': country})
        kind = 'manual:' + _key('', manual_url)[:12] if manual_url else 'companies'
        return self._run(kind, product_id, countries, tasks, providers, emit, cancel)

    def _saved_run(self, kind, product_id, countries):
        return self.store.one("SELECT id FROM job_runs WHERE kind=? AND product_id IS ? AND countries=? AND status IN ('paused','interrupted','failed','running','complete_partial') AND (status!='complete_partial' OR EXISTS(SELECT 1 FROM job_queue q WHERE q.run_id=job_runs.id AND q.status!='done')) ORDER BY id DESC LIMIT 1",
                              (kind, product_id, json.dumps(countries, ensure_ascii=False)))

    def resume_companies(self, run_id, product_id, countries, emit=lambda message: None, cancel=None):
        countries = self._countries(countries)
        signature = json.dumps(countries, ensure_ascii=False)
        run = self.store.one("SELECT * FROM job_runs WHERE id=? AND product_id=? AND countries=? AND (kind='companies' OR kind LIKE 'manual:%') AND status IN ('paused','interrupted','failed','running','complete_partial')", (run_id, product_id, signature))
        if not run:
            raise ValueError('Сохранённое исследование не найдено или уже завершено.')
        self._refresh_access(cancel)
        return self._run(run['kind'], product_id, countries, [], self._providers(), emit, cancel, run_id=run_id)

    def _market_followups(self, run_id, countries, providers, blocked):
        """One bounded, evidence-driven sweep of underrepresented categories."""
        marker = 'research.followups.' + str(run_id)
        if self.store.get_setting(marker, False):
            return False
        rows = market_ranking(self.store, countries, limit=10)['rows']
        if len(rows) >= 10:
            self.store.set_setting(marker, True)
            return False
        represented = {normalize_product(row['name']) for row in rows}
        sources = [name for name in providers if name not in blocked
                   and name in self._web_route(providers) + ['gdelt']]
        added = False
        for category, goods in CATEGORIES.items():
            missing = [good for good in goods if good not in represented]
            if not missing or any(good in represented for good in goods):
                continue
            for country in countries:
                for source in sources:
                    if source == 'tavily' and country != countries[0]:
                        continue
                    if source == 'gdelt':
                        if country != countries[0]:
                            continue
                        # Use supported product translations rather than another
                        # category query that maps to the same upstream request.
                        for good in [item for item in missing if item in GDELTProvider.GOODS][:2]:
                            self._enqueue(run_id, {'phase': 'search', 'provider': source,
                                'query': good, 'country': '', 'market': True, 'followup': True,
                                'selected_countries': countries})
                            added = True
                        continue
                    geography = ('(' + ' OR '.join('"' + item + '"' for item in countries) + ')'
                                 if source == 'tavily' else country)
                    self._enqueue(run_id, {'phase': 'search', 'provider': source,
                        'query': f'{" ".join(missing[:2])} рост спроса продажи исследование {geography} {MARKET_PERIOD_QUERY}',
                        'country': '' if source == 'tavily' else country, 'market': True, 'followup': True,
                        'selected_countries': countries})
                    added = True
        # Commit marker after enqueueing. A crash before it is harmless because
        # the durable task identity prevents duplicates.
        self.store.set_setting(marker, True)
        return added

    def _run(self, kind, product_id, countries, tasks, providers, emit, cancel, run_id=None):
        if not self._lock.acquire(blocking=False):
            raise ProviderError('Предыдущее исследование ещё выполняется.')
        try:
            self._cancel = cancel or threading.Event()
            self._requests = 0
            self._responded = set()
            self._unavailable = set()
            self._attempted_sources = set()
            self._batch_size = max(1, min(int(self.store.get_setting('search.request_limit', 100)), 2000))
            self._limit = self._batch_size
            signature = json.dumps(countries, ensure_ascii=False)
            if run_id is not None:
                existing = self.store.one("SELECT * FROM job_runs WHERE id=? AND kind=? AND product_id IS ? AND countries=? AND status IN ('paused','interrupted','failed','running','complete_partial')", (run_id, kind, product_id, signature))
                if not existing:
                    raise ValueError('Сохранённое исследование не найдено или уже завершено.')
            else:
                existing = self._saved_run(kind, product_id, countries)
            run_id = existing['id'] if existing else self.store.execute('INSERT INTO job_runs(kind,product_id,status,countries,created_at,updated_at) VALUES(?,?,?,?,?,?)', (kind, product_id, 'running', signature, now(), now()))
            self._active_run_id = run_id
            self._active_kind = kind
            self._responded.update(row['provider'] for row in self.store.query(
                'SELECT provider FROM job_source_responses WHERE run_id=?', (run_id,)))
            self.store.execute("UPDATE job_runs SET status='running',reason='',message='',updated_at=? WHERE id=?", (now(), run_id))
            self.store.execute("UPDATE job_queue SET status='pending' WHERE run_id=? AND status='running'", (run_id,))
            # Retry failed/retired source work once when this saved run is
            # resumed, while continuing to block it within an individual pass.
            with self.store.db as db:
                db.executemany("UPDATE job_queue SET status='pending',error='' WHERE run_id=? AND status='done' "
                               "AND json_extract(payload,'$.provider')=? AND "
                               "(json_extract(payload,'$.retry_on_resume')=1 OR error='Источник исключён из текущего исследования')",
                               [(run_id, name) for name in providers])
            original_emit = emit
            def emit(message):
                self.store.execute('INSERT INTO job_diagnostics(run_id,provider,message,created_at) VALUES(?,?,?,?)',
                                   (run_id, '', str(message)[:2000], now()))
                original_emit('Исследуем рынок. Ищем востребованные товары.' if kind == 'market' else
                              'Изучаем сайты и собираем контакты.' if any(word in str(message) for word in ('контакт', 'Публикация', 'Проверены ссылки')) else
                              'Ищем компании, которым может подойти товар.')
            for task in tasks:
                self._enqueue(run_id, task)
            blocked = set()
            status, reason = 'complete', ''
            pending, loaded_id = [], 0
            try:
                while True:
                    self._checkpoint()
                    # Payloads are decoded once; only newly enqueued work is
                    # fetched again. Existing pending tasks stay in memory.
                    incoming = self.store.query("SELECT * FROM job_queue WHERE run_id=? AND status='pending' AND id>? ORDER BY id", (run_id, loaded_id))
                    for row in incoming:
                        row['_payload'] = json.loads(row['payload'])
                        loaded_id = max(loaded_id, row['id'])
                    pending.extend(incoming)
                    pending.sort(key=lambda row: (row['_payload'].get('phase') not in ('company', 'market_page'), row['id']))
                    # Retire unavailable source work in this research. Failed tasks
                    # remain in history, never masquerading as successful responses.
                    retired = [row for row in pending if row['_payload'].get('provider') in blocked]
                    if retired:
                        with self.store.db as db:
                            db.executemany("UPDATE job_queue SET status='done',error='Источник исключён из текущего исследования' WHERE id=?",
                                           [(row['id'],) for row in retired])
                    rows = [row for row in pending if row['_payload'].get('provider') not in blocked]
                    pending = rows
                    if not rows:
                        if kind == 'market' and self._market_followups(run_id, countries, providers, blocked):
                            continue
                        break
                    todo = rows[0]
                    # Round-robin coverage: each adapter gets an initial turn before
                    # a large feed's page queue consumes subsequent batches.
                    for candidate in rows:
                        data = candidate['_payload']
                        if data.get('phase') == 'search' and data.get('provider') not in self._attempted_sources:
                            todo = candidate
                            break
                    payload = todo['_payload']
                    if payload.get('phase') == 'market_page':
                        completed = self._market_page_batch(run_id, rows, emit)
                        if completed:
                            pending = [row for row in pending if row['id'] not in completed]
                            continue
                    pending = [row for row in pending if row['id'] != todo['id']]
                    if payload.get('phase') == 'search':
                        self._attempted_sources.add(payload.get('provider'))
                    self.store.execute("UPDATE job_queue SET status='running',error='' WHERE id=?", (todo['id'],))
                    try:
                        source = payload.get('provider') if payload.get('phase') == 'search' else None
                        if ((source and source != 'web' and source not in providers)
                                or (source == 'web' and not self._web_route(providers))):
                            self._unavailable.add(source)
                            blocked.add(source)
                            self.store.execute("UPDATE job_queue SET status='done',error='Пропущено: источник отключён или его бесплатный доступ не подтверждён' WHERE id=?", (todo['id'],))
                            continue
                        self._handle(run_id, product_id, payload, providers, emit, blocked)
                    except ProviderError as exc:
                        source = payload.get('provider')
                        if source and source != 'web':
                            blocked.add(source)
                            self._unavailable.add(source)
                        payload['failures'] = int(payload.get('failures', 0)) + 1
                        payload['retry_on_resume'] = True
                        self.store.execute("UPDATE job_queue SET status='done',error=?,payload=? WHERE id=?",
                            (str(exc)[:500], json.dumps(payload, ensure_ascii=False), todo['id']))
                        emit(f'{source or "Источник"}: {exc}')
                    except FetchError as exc:
                        self.store.execute("UPDATE job_queue SET status='done',error=? WHERE id=?", (str(exc)[:500], todo['id']))
                        emit('Страница недоступна: ' + str(exc))
                    except Paused:
                        self.store.execute("UPDATE job_queue SET status='pending',payload=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), todo['id']))
                        raise
                    except Exception as exc:
                        if (isinstance(exc, sqlite3.Error)
                                or payload.get('phase') not in ('company', 'market_page', 'discovery')):
                            raise
                        # Isolate unexpected company/page parsing failures to
                        # this item. Scheduling failures must remain resumable;
                        # database failures must remain fatal.
                        self.store.execute("UPDATE job_queue SET status='done',error=? WHERE id=?",
                                           (type(exc).__name__ + ': не удалось обработать элемент поиска', todo['id']))
                        emit('Не удалось обработать один результат. Продолжаем поиск.')
                    else:
                        self.store.execute("UPDATE job_queue SET status='done',payload=?,error=? WHERE id=?",
                            (json.dumps(payload, ensure_ascii=False), 'Часть RSS-лент недоступна' if payload.get('partial') else '', todo['id']))
            except Paused as exc:
                status, reason = 'interrupted', 'user' if self._cancel.is_set() else 'interrupted'
                self.store.execute("UPDATE job_queue SET status='pending' WHERE run_id=? AND status='running'", (run_id,))
                emit(str(exc))
            except BaseException as exc:
                self.store.execute("UPDATE job_queue SET status='pending' WHERE run_id=? AND status='running'", (run_id,))
                self.store.execute('INSERT INTO job_diagnostics(run_id,provider,message,created_at) VALUES(?,?,?,?)',
                                   (run_id, '', type(exc).__name__ + ': непредвиденная ошибка; работа сохранена.', now()))
                self.store.execute("UPDATE job_runs SET status='failed',reason='error',message='Работа прервана. Прогресс сохранён.',updated_at=? WHERE id=?", (now(), run_id))
                raise
            count = len(market_ranking(self.store, countries)['rows']) if product_id is None else len(visible_companies(self.store, product_id))
            if status == 'complete' and (not self._responded or (kind == 'market' and count < 10)):
                reason = 'sources_exhausted'
            noun = 'товаров' if kind == 'market' else 'компаний'
            total = len(market_source_names()) if kind == 'market' else sum('companies' in spec.capabilities for spec in ADAPTERS.values())
            message = (f'Поиск завершён. {"Доступные источники исчерпаны. " if reason == "sources_exhausted" else ""}Найдено {noun}: {count}. Использовано источников: {len(self._responded)}/{total}.'
                       if status == 'complete' else 'Работа прервана. Незавершённое исследование сохранено для восстановления.')
            self.store.execute('UPDATE job_runs SET status=?,reason=?,message=?,updated_at=? WHERE id=?',
                               (status, reason, message, now(), run_id))
            response = {'run_id': run_id, 'status': status, 'count': count, 'requests': self._requests,
                        'reason': reason, 'successful_sources': sorted(self._responded),
                        'unavailable_sources': sorted(self._unavailable), 'message': message,
                        'sources_used': len(self._responded), 'sources_total': total,
                        'billing_blocked_sources': self.billing_blocked_sources()}
            if kind == 'market':
                response.update(market_summary(self.store, run_id, countries, status))
                self.store.execute('UPDATE job_runs SET status=?,message=? WHERE id=?',
                                   (response['status'], response['message'], run_id))
            return response
        finally:
            self._active_run_id = None
            self._lock.release()

    def _source_responded(self, name):
        self._responded.add(name)
        run_id = getattr(self, '_active_run_id', None)
        if run_id is not None:
            self.store.execute('INSERT OR IGNORE INTO job_source_responses(run_id,provider,first_success_at) VALUES(?,?,?)',
                               (run_id, name, now()))

    def _enqueue(self, run_id, payload):
        phase = payload['phase']
        existing = self.store.one(
                "SELECT id,status,error FROM job_queue WHERE run_id=? AND json_extract(payload,'$.phase')='search' "
                "AND json_extract(payload,'$.provider')=? AND json_extract(payload,'$.query')=? "
                "AND json_extract(payload,'$.country')=? AND COALESCE(json_extract(payload,'$.market'),0)=? LIMIT 1",
                (run_id, payload['provider'], payload['query'], payload['country'], int(payload.get('market', False)))) if phase == 'search' else None
        if existing:
            if existing['status'] == 'done' and (existing['error'] or '').startswith('Пропущено:'):
                # Only current available tasks are offered by the planner. A
                # previously disabled source may now be verified and enabled.
                self.store.execute("UPDATE job_queue SET status='pending',error='',payload=? WHERE id=?",
                                   (json.dumps(payload, ensure_ascii=False), existing['id']))
            return
        identity = payload if phase != 'company' else (payload.get('result', {}).get('metadata', {}).get('source_url') or payload.get('result', {}).get('url'))
        if phase == 'market_page':
            result = payload['result']
            identity = [result['url'], '' if result.get('metadata', {}).get('provider') == 'gdelt' else result.get('country', '')]
        task_key = phase + ':' + _key('', identity)
        self.store.execute('INSERT OR IGNORE INTO job_queue(run_id,task_key,payload,status) VALUES(?,?,?,?)',
                           (run_id, task_key, json.dumps(payload, ensure_ascii=False), 'pending'))

    def _handle(self, run_id, product_id, task, providers, emit, blocked=None):
        if task['phase'] == 'search':
            blocked = blocked if blocked is not None else set()
            if task['provider'] == 'web':
                route = task.get('route', self._web_route(providers))
                route = list(dict.fromkeys(route + self._web_route(providers)))
                task['route'] = route
                index = int(task.get('next_provider', 0))
                route_keys = {name: hashlib.sha256(self.secrets.get(name + '_key').encode()).hexdigest()
                              for name in route if name in KEYED_PROVIDERS}
                old_keys = task.get('route_keys', {})
                if old_keys and any(old_keys.get(name) != value for name, value in route_keys.items()):
                    # A replacement key can make an earlier failed source
                    # usable again; do not strand it behind the saved cursor.
                    index = 0
                task['route_keys'] = route_keys
                # An exhausted route is retried only on explicit resume, not in this run.
                if index >= len(route):
                    index = 0
                results = [SearchResult(**row) for row in task.get('route_results', [])]
                saw_response = bool(task.get('successful_providers'))
                for position in range(index, len(route)):
                    name = route[position]
                    task['next_provider'] = position
                    if name in blocked:
                        task['next_provider'] = position + 1
                        continue
                    provider = providers.get(name)
                    if provider is None:
                        task['next_provider'] = position + 1
                        continue
                    emit(f'{task["country"]}: запрос к {name}…')
                    try:
                        results = self._search(name, provider, task['query'], task['country'], limit=10)
                    except ProviderError as exc:
                        blocked.add(name)
                        task['next_provider'] = position + 1
                        emit(f'{name}: {exc}')
                        continue
                    saw_response = True
                    task['selected_provider'] = name
                    task['successful_providers'] = list(dict.fromkeys(task.get('successful_providers', []) + [name]))
                    # Old saved web routes also collect every compatible source.
                    # Persist intermediate hits in the task on interruption.
                    previous = [SearchResult(**row) for row in task.get('route_results', [])]
                    results = previous + results
                    task['route_results'] = [row.to_dict() for row in results]
                    task['next_provider'] = position + 1
                if not results and not saw_response:
                    raise ProviderError('Доступные веб-источники временно недоступны. Прогресс сохранён.')
            else:
                provider = providers.get(task['provider'])
                if provider is None:
                    raise ProviderError('Источник отключён в настройках; подключите его для продолжения.')
                emit(f'{task["country"]}: запрос к {task["provider"]}…')
                if task.get('discover') and task['provider'] == 'rss':
                    try:
                        results = provider.discover(task['country'], limit=1000)
                    except ProviderError as exc:
                        self._record_status('rss', exc)
                        self._unavailable.add('rss')
                        raise
                    self._source_responded('rss')
                    self._record_status('rss')
                    task['partial'] = bool(provider.last_errors)
                    task['failed_feeds'] = list(provider.last_errors)
                    if task['partial']:
                        self._unavailable.add('rss:partial')
                        emit(f'RSS: недоступно лент — {len(provider.last_errors)}. Продолжаем по остальным.')
                    # Prefer actual demand reports; articles can identify goods in their body.
                    results = [r for r in results if self._market_candidate(r)]
                    results.sort(key=lambda r: (
                        bool(product_candidates(r.title + ' ' + r.snippet)) * 2 +
                        bool(re.search(r'спрос|продаж|закуп|заказ|покуп|просмотр|интерес', r.title + ' ' + r.snippet, re.I)) * 8 +
                        bool(re.search(r'вырос|увелич|приобрели|продано', r.title + ' ' + r.snippet, re.I))), reverse=True)
                    groups = {}
                    for result in results:
                        group = tuple(product_candidates(result.title + ' ' + result.snippet)) or ('other',)
                        groups.setdefault(group, []).append(result)
                    from itertools import zip_longest
                    results = [item for batch in zip_longest(*groups.values()) for item in batch if item is not None][:300]
                else:
                    results = self._search(task['provider'], provider, task['query'], task['country'], limit=10)
            for result in results:
                if not result.url.startswith(('https://', 'http://')):
                    continue
                if task.get('market'):
                    if task.get('selected_countries'):
                        result.metadata = {**result.metadata, 'selected_countries': task['selected_countries']}
                    if task['provider'] == 'comtrade':
                        self._market_evidence(result, emit)
                    else:
                        self._enqueue(run_id, {'phase': 'market_page', 'result': result.to_dict(), 'category': task['query']})
                else:
                    if result.metadata.get('provider') in ('gdelt', 'rss', 'mediawiki', 'catalog', 'procurement'):
                        self._enqueue(run_id, {'phase': 'discovery', 'url': result.url,
                            'result': result.to_dict(), 'country': task['country'], 'query': task['query']})
                    else:
                        self._enqueue(run_id, {'phase': 'company', 'result': result.to_dict(), 'manual': False})
            emit(f'{task.get("selected_provider", task["provider"])}: найдено ссылок — {len(results)}.')
        elif task['phase'] == 'market_page':
            self._market_evidence(SearchResult(**task['result']), emit)
        elif task['phase'] == 'company':
            result = SearchResult(**task['result'])
            # One site needs one crawl even when found by several country
            # queries. A resumed URL task uses the same persisted scope.
            run = self.store.one('SELECT countries FROM job_runs WHERE id=?', (run_id,))
            if run:
                result.metadata = {**result.metadata, 'company_countries': json.loads(run['countries'])}
            if is_marketplace(result.url) and not result.metadata.get('source_url'):
                seller = marketplace_seller_hint(result.title, result.snippet, result.url)
                if seller:
                    geography = result.country or 'Россия'
                    query = f'{seller} официальный сайт {geography}'
                    for provider in ('tavily', 'firecrawl', 'mediawiki'):
                        if provider in self._unavailable:
                            continue
                        self._enqueue(run_id, {'phase': 'search', 'provider': provider, 'query': query,
                                               'country': result.country, 'market': False,
                                               'marketplace_hint': result.url, 'seller': seller})
                        break
                    emit(f'Маркетплейс: подсказка продавца «{seller}». Ищем независимый официальный источник.')
                else:
                    emit('Маркетплейс: продавец не назван однозначно; страница не используется для оценки.')
                return
            self._company(product_id, result, task.get('manual', False), emit)
        elif task['phase'] == 'discovery':
            result = SearchResult(**task['result']) if task.get('result') else None
            candidates = result.metadata.get('discovery_urls', []) if result else []
            source_id = None
            try:
                page = self._page(task['url'], task['country'])
                source_id = page['source_id']
                candidates = list(dict.fromkeys(candidates + page.get('external_links', [])))
            except FetchError as exc:
                emit('Публикация недоступна: ' + str(exc))
            for url in candidates[:5]:
                try:
                    url = validate_url(url)
                except FetchError:
                    continue
                if is_directory(url):
                    continue
                self._enqueue(run_id, {'phase': 'company', 'result': SearchResult(url,
                    country=task['country'], metadata={'discovered_from': task['url'],
                    'discovery_source_id': source_id}).to_dict(), 'manual': False})
            emit(f'Проверены ссылки публикации: кандидатов — {min(len(candidates), 5)}. Официальность и применимость проверяются отдельно.')

    def _document(self, url, source='rss'):
        url = validate_url(url)
        key = _key('document:v1:', url)
        cached = None if self._force_refresh else self._cache(key)
        if cached is not None:
            if cached.get('error'):
                raise FetchError(cached['error'])
            return cached
        self._checkpoint()
        self._take_request()
        self.store.execute('INSERT INTO usage_log(provider,operation,credits,created_at) VALUES(?,?,?,?)',
                           (source, 'public_document', 0, now()))
        try:
            raw = self.fetcher.fetch_document(url)
        except FetchError as exc:
            self._put_cache(key, {'error': str(exc)}, ttl=1800)
            raise
        value = {'url': raw.url, 'text': raw.text}
        self._put_cache(key, value, ttl=1800)
        return value

    def _page(self, url, country):
        url = validate_url(url)
        key = _key('page:v9:', [url, country])
        cached = None if self._force_refresh else self._cache(key)
        if cached is not None:
            if cached.get('error'):
                raise FetchError(cached['error'])
            return cached
        self._checkpoint()
        self._take_request()
        try:
            raw = self.fetcher.fetch(url)
            page = parse_page(raw.text, raw.url, country)
            page['market_text'] = article_text(raw.text)
            page['external_links'] = external_links(raw.text, raw.url)
            page['source_id'] = self.store.put_source(raw.url, page['title'], page['text'])
        except FetchError as exc:
            self._put_cache(key, {'error': str(exc)}, ttl=3600)
            raise
        self._put_cache(key, page, ttl=86400)
        return page

    def _market_evidence(self, result, emit):
        text = result.title + '. ' + result.snippet
        source_id = None
        if not self._market_candidate(result):
            return
        if not result.metadata.get('official_statistics'):
            try:
                page = self._page(result.url, result.country)
                text = result.title + '. ' + (page.get('market_text') or result.snippet)
                # Market validation must use the same article-only evidence as
                # extraction. Site menus mention unrelated countries/products.
                source_id = self.store.put_source(page.get('url', result.url), result.title, text, result.published_at)
            except FetchError:
                pass
        candidates = product_candidates(text)
        structured_product = result.metadata.get('product')
        if structured_product and matches_product(normalize_product(structured_product), text):
            candidates = [normalize_product(structured_product)]
        if not candidates:
            return
        for product in candidates:
            observations = extract_observations(product, text, result.country, result.published_at, result.metadata,
                                                title=result.title)
            selected = result.metadata.get('selected_countries')
            if selected:
                observations = [item for item in observations if item['country'] in selected]
            if not observations:
                continue
            if not source_id:
                source_id = self.store.put_source(result.url, result.title, text[:100000], result.published_at)
            elif result.published_at:
                self.store.execute("UPDATE sources SET published_at=? WHERE id=? AND published_at=''", (result.published_at, source_id))
            normalized = normalize_product(product)
            existing = self.store.one('SELECT * FROM products WHERE normalized=?', (normalized,))
            # Old versions could use the generic identity for a model-specific
            # workspace. Preserve that workspace and create a separate market
            # product instead of silently repurposing or splitting it.
            if existing and branch_identity(existing['name']) != normalized:
                normalized = 'market:' + normalized
            row = self.store.ensure_product(product, normalized=normalized)
            for item in observations:
                self.store.execute('''INSERT OR IGNORE INTO market_observations
                    (product_id,source_id,metric,value,unit,period,country,published_at,excerpt,measured,event_key,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''', (row['id'], source_id, item['metric'], item['value'], item['unit'],
                    item['period'], item['country'], item['published_at'], item['excerpt'], int(item['measured']), item['event_key'], now()))
                observation = self.store.one('SELECT id FROM market_observations WHERE product_id=? AND event_key=?',
                                             (row['id'], item['event_key']))
                self.store.execute('''INSERT OR IGNORE INTO market_observation_sources
                    (observation_id,source_id,excerpt,published_at) VALUES(?,?,?,?)''',
                    (observation['id'], source_id, item['excerpt'], item['published_at']))
                description = measurement_basis(item) if item['measured'] else 'Публичный сигнал: ' + item['excerpt']
                # Keep source links for the existing source dialog; score no longer ranks demand.
                self.store.execute('''INSERT OR IGNORE INTO market_signals
                    (product_id,source_id,country,kind,description,score,created_at) VALUES(?,?,?,?,?,?,?)''',
                    (row['id'], source_id, item['country'], 'measured_' + item['metric'] if item['measured'] else 'indirect_publication',
                     description[:1000], 0, now()))
        emit('Сохранены данные рынка: ' + ', '.join(candidates[:6]) + '.')

    @staticmethod
    def _market_candidate(result):
        if not market_publication_possible(result.published_at, datetime.now(timezone.utc)):
            return False
        summary = result.title + ' ' + result.snippet
        return bool(result.metadata.get('official_statistics') or product_candidates(summary) or
                    re.search(r'спрос|продаж|закуп|заказ|покуп|потреблен', summary, re.I))

    def market_ranking(self, countries, limit=10):
        return market_ranking(self.store, countries, limit)

    def _same_ai_evidence(self, product_name, existing, pages, metadata, country):
        """Keep accepted AI reasoning only while its cited evidence is still current."""
        if not existing or existing['status'] != 'FIT' or not existing['ai_checked_key']:
            return False
        match = re.search(r' Основание: «(.+?)»\s*$', existing['reason'] or '', re.S)
        if not match or len(match[1]) < 15:
            return False
        quote = clean_text(match[1])
        try:
            ids = json.loads(existing['source_ids'])
        except (TypeError, ValueError):
            return False
        if not isinstance(ids, list):
            return False
        cited_urls = set()
        for sid in ids:
            source = self.store.one('SELECT url,text FROM sources WHERE id=?', (sid,)) if type(sid) is int else None
            if source and quote in clean_text(source['text']):
                cited_urls.add(source['url'])
        if not any(page.get('url') in cited_urls and quote in clean_text(page.get('text', '')) for page in pages):
            return False
        # Recheck provenance and current denials without requiring the semantic
        # relationship to fit the same limited rules that originally deferred it.
        from .fit_rules import semantic_evidence_allowed
        return semantic_evidence_allowed(product_name, pages, quote)

    def _company(self, product_id, result, manual, emit):
        product = self.store.one('SELECT * FROM products WHERE id=?', (product_id,))
        meta = dict(result.metadata)
        pages, source_ids, contacts = [], [], []
        errors, rejected_urls = [], set()
        rejected_redirect = None
        if meta.get('discovery_source_id'):
            source_ids.append(meta['discovery_source_id'])
        if meta.get('source_url'):
            sid = self.store.put_source(meta['source_url'], result.title, result.snippet)
            source_ids.append(sid)
            meta['structured_source_id'] = sid
            meta['structured_text'] = result.snippet
            for value in meta.get('emails', []):
                contacts.extend(('email', email, sid) for email in emails_in(value))
            for value in meta.get('phones', []):
                normalized = phone_normalize(value, result.country)
                if normalized:
                    contacts.append(('phone', normalized, sid))
        # Preserve the supplied public site even when its first fetch fails, so
        # the user can still open it manually from the company card.
        official_url = result.url if not is_directory(result.url) else ''
        if not is_directory(result.url):
            try:
                first = self._page(result.url, result.country)
                site_domain = domain_of(first['url'])
                expected_name = company_identity(result.title)
                different_structured_site = (meta.get('source_url') and site_domain != domain_of(result.url)
                                             and (not expected_name or expected_name != company_identity(first['name'])))
                if is_directory(first['url']) or different_structured_site:
                    rejected_urls.add(first['url'])
                    rejected_redirect = (result.url, first['url'])
                    official_url = ''
                    raise FetchError('Адрес перенаправляет на справочник или сайт другой организации.')
                pages.append(first)
                official_url = first['url']
                page_limit = max(1, min(int(self.store.get_setting('search.page_limit', 8)), 20))
                todo = first['links']
                root = urlsplit(first['url'])
                root_url = urlunsplit((root.scheme, root.netloc, '/', '', ''))
                if root_url != first['url']:
                    todo = [root_url] + todo
                # Only consult sitemap when direct page links cannot fill the bounded crawl.
                if len(todo) < page_limit - 1:
                    try:
                        document = self._document(urlunsplit((root.scheme, root.netloc, '/sitemap.xml', '', '')), source='website')
                        todo = list(dict.fromkeys(todo + sitemap_links(document['text'], first['url'])))
                    except FetchError:
                        pass
                seen = {first['url']}
                for url in todo:
                    if len(pages) >= page_limit:
                        break
                    if url in seen:
                        continue
                    seen.add(url)
                    try:
                        page = self._page(url, result.country)
                        if domain_of(page['url']) != site_domain:
                            rejected_urls.add(page['url'])
                            errors.append('Страница сайта перенаправляет на другую организацию.')
                            continue
                        pages.append(page)
                    except FetchError as exc:
                        errors.append(str(exc))
            except FetchError as exc:
                errors.append(str(exc))
        elif not meta.get('source_url'):
            errors.append('Ссылка ведёт на справочник или публикацию; официальный сайт компании не установлен.')
        name = result.title if meta.get('source_url') else (pages[0]['name'] if pages else result.title)
        name = (name or domain_of(result.url))[:180]
        if meta.get('provider') == 'overpass':
            branch_address = ', '.join(str(meta.get('tags', {}).get(k, '')) for k in ('addr:city', 'addr:street', 'addr:housenumber') if meta.get('tags', {}).get(k))
            if branch_address:
                name = (name + ' — ' + branch_address)[:180]
        from .fit_rules import evaluate_fit
        decision = evaluate_fit(product['name'], pages, meta, result.country)
        status, reason = decision['status'], decision['reason']
        profile = decision['profile']
        potential = int(decision['potential'])
        country = profile['country'] or result.country
        for page in pages:
            sid = page['source_id']
            source_ids.append(sid)
            contacts.extend(('email', value, sid) for value in page['emails'])
            phones = page['phones']
            if country != result.country and 'phone_values' in page:
                phones = sorted({phone for raw in page['phone_values'] if (phone := phone_normalize(raw, country))})
            contacts.extend(('phone', value, sid) for value in phones)
        own_legal_ids, external_legal_ids = set(), set()
        if country == 'Россия':
            own_legal_ids = {value for page in pages for value in page.get('legal_ids', (page.get('legal_id'),)) if value}
            external_legal_ids = {value for page in pages for value in page.get('external_legal_ids', ())} - own_legal_ids
        legal_id = next(iter(own_legal_ids)) if len(own_legal_ids) == 1 else ''
        if is_directory(result.url) and not meta.get('source_url'):
            status, potential = 'INSUFFICIENT_EVIDENCE', 0
            reason = 'Нужна проверка: требуется сайт самой организации.'
        if errors and not pages and not profile['activities']:
            reason += ' ' + ' '.join(dict.fromkeys(errors))[:250]
        if not contacts:
            reason += ' При первичном обходе email и телефон не найдены.'
        if not source_ids:
            source_ids.append(self.store.put_source(result.url, name, 'Исследование не выполнено. ' + ' '.join(errors)))
        candidate_domain = domain_of(official_url or result.url) if not is_directory(official_url or result.url) else ''
        branch_name = ''
        address = ''
        region = ''
        if meta.get('provider') == 'overpass':
            tags = meta.get('tags', {})
            address = ', '.join(str(tags.get(k, '')) for k in ('addr:city', 'addr:street', 'addr:housenumber') if tags.get(k))
            region = str(tags.get('addr:city') or tags.get('addr:region') or '')
            branch_name = name
        parent_name = name
        org_key = organization_key(parent_name, candidate_domain)
        identity = company_identity_key(parent_name, domain=candidate_domain, url=official_url,
                                        address=address, branch_name=branch_name,
                                        source_url=meta.get('source_url', ''), country=profile['country'],
                                        legal_id=legal_id)
        company_id = self._save_company(name, candidate_domain, official_url, profile['country'], legal_id, manual,
                                        meta.get('source_url', ''), failed_fetch=bool(errors and not pages),
                                        external_legal_ids=external_legal_ids, rejected_redirect=rejected_redirect,
                                        branch_name=branch_name, address=address, region=region,
                                        org_key=org_key, identity=identity)
        self._persist_fit(product_id, company_id, product, pages, meta, country, status, reason, potential,
                          profile, source_ids, contacts, manual, errors, rejected_urls, name, emit)
        # Confirmed local branches keep separate cards, contacts and fit history.
        for branch_page in pages:
            for branch in branch_page.get('branches') or []:
                branch_address = branch.get('address') or ''
                branch_region = branch.get('region') or ''
                label = branch.get('branch_name') or branch.get('name') or branch_address
                if not (branch_address or branch_region or label):
                    continue
                display = (f'{parent_name} — {label}')[:180]
                branch_identity_key = company_identity_key(
                    parent_name, domain=candidate_domain, url=official_url, address=branch_address,
                    branch_name=label, parent=parent_name, country=profile['country'], legal_id='')
                branch_id = self._save_company(
                    display, candidate_domain, official_url, profile['country'], '', manual,
                    '', branch_name=label, address=branch_address, region=branch_region,
                    org_key=org_key, identity=branch_identity_key)
                branch_contacts = [('email', value, None) for value in branch.get('emails', [])]
                branch_contacts.extend(('phone', value, None) for value in branch.get('phones', []))
                branch_text = '\n'.join(filter(None, (
                    branch.get('description') or '',
                    'Адрес филиала: ' + branch_address if branch_address else '',
                    'Регион филиала: ' + branch_region if branch_region else '',
                    'Контакты: ' + ', '.join(branch.get('emails', []) + branch.get('phones', []))
                    if branch.get('emails') or branch.get('phones') else '',
                )))
                branch_source_url = branch_page.get('url') or official_url
                branch_source_id = self.store.put_source(branch_source_url, display, branch_text)
                for index, (kind, value, _) in enumerate(branch_contacts):
                    branch_contacts[index] = (kind, value, branch_source_id)
                branch_pages = [{'text': branch_text, 'source_id': branch_source_id,
                                 'url': branch_source_url, 'emails': branch.get('emails', []),
                                 'phones': branch.get('phones', [])}]
                from .fit_rules import evaluate_fit
                # Parent-level OSM tags or website activity do not prove what
                # this branch does. Each branch must carry its own evidence.
                branch_decision = evaluate_fit(product['name'], branch_pages, {}, result.country)
                self._persist_fit(product_id, branch_id, product, branch_pages, {}, result.country,
                                  branch_decision['status'], branch_decision['reason'],
                                  int(branch_decision['potential']), branch_decision['profile'],
                                  [branch_source_id], branch_contacts, manual, [], set(), display, emit)

    def _persist_fit(self, product_id, company_id, product, pages, meta, country, status, reason, potential,
                     profile, source_ids, contacts, manual, errors, rejected_urls, name, emit):
        fit_key = hashlib.sha256(json.dumps({'product': normalize_product(product['name']),
            'profile': profile, 'sources': sorted(set(source_ids))}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        existing = self.store.one('SELECT * FROM company_fit WHERE product_id=? AND company_id=?', (product_id, company_id))
        ai_checked = existing['ai_checked_key'] if existing else ''
        manual_decision = bool(existing and (existing['fit_key'] or '').startswith('manual:'))
        # Reuse an accepted semantic decision for identical inputs. A transport
        # failure alone also must not erase previously saved positive facts.
        stable_ai = (status != 'NOT_FIT' and existing and existing['fit_key'] != fit_key
                     and self._same_ai_evidence(product['name'], existing, pages, meta, country))
        if manual_decision:
            status, reason, potential = existing['status'], existing['reason'], existing['potential']
            fit_key = existing['fit_key']
            source_ids = list(source_ids) + json.loads(existing['source_ids'])
        elif stable_ai:
            status, reason, potential = existing['status'], existing['reason'], existing['potential']
            profile = json.loads(existing['activity_profile'] or '{}')
            source_ids = list(source_ids) + json.loads(existing['source_ids'])
        elif status != 'NOT_FIT' and existing and (existing['fit_key'] == fit_key or
                (errors and not rejected_urls and not pages and not profile['activities'] and
                 (existing['status'] == 'FIT' or (existing['status'] == 'INSUFFICIENT_EVIDENCE' and existing['potential'])))):
            status, reason, potential = existing['status'], existing['reason'], existing['potential']
            profile = json.loads(existing['activity_profile'] or '{}')
            fit_key = existing['fit_key']
            source_ids = json.loads(existing['source_ids']) if errors and not pages else list(source_ids) + json.loads(existing['source_ids'])
        if existing and existing['status'] == 'FIT' and existing['ai_checked_key'] and status != 'FIT':
            self.store.execute('INSERT INTO audit_events(event,details,created_at) VALUES(?,?,?)',
                               ('fit_evidence_changed', json.dumps({'product_id': product_id, 'company_id': company_id,
                                   'previous_reason': existing['reason'], 'previous_source_ids': json.loads(existing['source_ids']),
                                   'new_status': status}, ensure_ascii=False), now()))
        self.store.execute('INSERT INTO company_fit(product_id,company_id,status,reason,source_ids,manual,a,b,c,checked_at,potential,activity_profile,fit_key,ai_checked_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(product_id,company_id) DO UPDATE SET status=excluded.status,reason=excluded.reason,source_ids=excluded.source_ids,manual=MAX(company_fit.manual,excluded.manual),a=excluded.a,b=excluded.b,c=excluded.c,checked_at=excluded.checked_at,potential=excluded.potential,activity_profile=excluded.activity_profile,fit_key=excluded.fit_key,ai_checked_key=excluded.ai_checked_key',
                           (product_id, company_id, status, reason[:2500], json.dumps(sorted(set(source_ids))), int(manual), existing['a'] if manual_decision else (1 if status == 'FIT' else None), None, 1 if contacts else None, now(), potential, json.dumps(profile, ensure_ascii=False), fit_key, ai_checked))
        for kind, value, sid in contacts:
            scope = 'hq' if kind == 'email' and email_purpose(value)[1] == 'Общий' else ''
            self.store.execute('INSERT INTO company_contacts(company_id,kind,value,source_id,checked_at,scope) VALUES(?,?,?,?,?,?) '
                               'ON CONFLICT(company_id,kind,value) DO UPDATE SET '
                               'source_id=CASE WHEN company_contacts.source_id IS NULL THEN NULL ELSE excluded.source_id END,'
                               'checked_at=excluded.checked_at,scope=CASE WHEN excluded.scope<>\'\' THEN excluded.scope ELSE company_contacts.scope END',
                               (company_id, kind, value, sid, now(), scope))
        developer_emails = {(page['url'], email) for page in pages for email in page.get('developer_emails', ())}
        if rejected_urls or developer_emails or (pages and not errors):
            current_contacts = {(kind, value.casefold()) for kind, value, _ in contacts}
            current_urls = {page['url'] for page in pages} if not errors else set()
            for old in self.store.query("SELECT c.*,s.url AS source_url FROM company_contacts c "
                                        "JOIN sources s ON s.id=c.source_id WHERE c.company_id=?", (company_id,)):
                value = old['value'].casefold()
                lost_email = old['kind'] == 'email' and (old['source_url'] in current_urls
                                                        or (old['source_url'], value) in developer_emails)
                rejected_source = old['source_url'] in rejected_urls
                if (lost_email or rejected_source) and (old['kind'], value) not in current_contacts:
                    with self.store.db as db:
                        db.execute('INSERT INTO audit_events(event,details,created_at) VALUES(?,?,?)',
                                   ('contact_retired', json.dumps(dict(old), ensure_ascii=False), now()))
                        db.execute('DELETE FROM company_contacts WHERE id=?', (old['id'],))
        if not manual_decision and status == 'INSUFFICIENT_EVIDENCE' and potential and contacts and profile.get('activities') and self.secrets.get('gigachat_key'):
            if not getattr(self, 'ai', None):
                from sales_manager.ai import AIService
                self.ai = AIService(self.store, self.secrets)
            try:
                self.ai.assess_fit(product_id, company_id)
                revised = self.store.one('SELECT status,reason FROM company_fit WHERE product_id=? AND company_id=?', (product_id, company_id))
                status, reason = revised['status'], revised['reason']
            except (ValueError, RuntimeError):
                emit(f'{name}: нужна проверка, кандидат сохранён.')
        emit(f'{name}: ' + ('подходит, контакты сохранены' if status == 'FIT' and contacts else reason[:180]))

    def _fit(self, product_name, text, pages, metadata, country):
        from .fit_rules import evaluate_fit
        decision = evaluate_fit(product_name, pages, metadata, country)
        return decision['status'], decision['reason']

    def _save_company(self, name, domain, url, country, legal_id, manual, source_url='', failed_fetch=False,
                      external_legal_ids=(), rejected_redirect=None, branch_name='', address='', region='',
                      org_key='', identity=''):
        def unambiguous(rows):
            # With no country in the new evidence, reuse only a unique identity;
            # identical names/domains can legitimately exist in several states.
            unique = {row['id']: row for row in rows}
            return next(iter(unique.values())) if len(unique) == 1 else None

        same = None
        identity_key = identity or company_identity_key(
            name, domain=domain, url=url, address=address, branch_name=branch_name,
            source_url=source_url, country=country, legal_id=legal_id)
        organization_value = org_key or organization_key(name, domain)

        def compatible(row):
            if legal_id and row['legal_id'] and legal_id != row['legal_id'] and row['legal_id'] not in external_legal_ids:
                return False
            if address and row['address'] and clean_text(row['address']) != clean_text(address):
                return False
            if (branch_name or row['branch_name']) and clean_text(row['branch_name']) != clean_text(branch_name):
                return False
            return True

        # Stable identity first. Raw INN is not a cross-site merge key.
        if identity_key:
            same = unambiguous(row for row in self.store.query(
                'SELECT * FROM companies WHERE identity_key=? AND (country=? OR length(country)=0 OR ?=\'\')',
                (identity_key, country, country)) if compatible(row))
        if source_url and not same:
            # A structured provider's stable entity/branch ID is stronger than its brand name.
            same = unambiguous(row for row in self.store.query(
                'SELECT DISTINCT c.* FROM companies c JOIN company_fit f ON f.company_id=c.id '
                'JOIN json_each(f.source_ids) j JOIN sources s ON s.id=j.value '
                'WHERE s.url=? AND (c.country=? OR length(c.country)=0 OR ?=\'\')',
                (source_url, country, country)) if compatible(row))
        if (not same and legal_id and not source_url.startswith('https://www.openstreetmap.org/')):
            # Reuse a card only when this INN already belongs to that card's own
            # confirmed sources, or the official domain is unchanged.
            candidates = self.store.query(
                'SELECT * FROM companies WHERE legal_id=? AND (country=? OR length(country)=0 OR ?=\'\')',
                (legal_id, country, country))
            same = unambiguous(row for row in candidates
                               if compatible(row) and (row['domain'] == domain
                                                       or self._confirmed_company_legal_id(row, legal_id)))
        if not same and url and (failed_fetch or not source_url) and not source_url.startswith('https://www.openstreetmap.org/'):
            # A site's title can change on a successful crawl. Prefer the same
            # legal entity on that URL, then a unique compatible card.
            url_rows = [row for row in self.store.query(
                'SELECT * FROM companies WHERE url=? AND (country=? OR length(country)=0 OR ?=\'\')'
                ' AND (branch_name=? OR (branch_name=\'\' AND ?=\'\'))',
                (url, country, country, branch_name, branch_name)) if compatible(row)]
            if legal_id:
                same = unambiguous(row for row in url_rows if row['legal_id'] == legal_id)
            if not same:
                same = unambiguous(row for row in url_rows
                                   if company_identity(row['name']) == company_identity(name))
            if not same:
                candidate = unambiguous(url_rows)
                if candidate:
                    structured_identity = self.store.one(
                        "SELECT 1 FROM company_fit f "
                        "JOIN json_each(CASE WHEN json_valid(f.source_ids) THEN f.source_ids ELSE '[]' END) j "
                        "JOIN sources s ON s.id=j.value "
                        "WHERE f.company_id=? AND (s.url LIKE 'https://www.openstreetmap.org/%' "
                        "OR s.url LIKE 'https://www.wikidata.org/wiki/Q%') LIMIT 1", (candidate['id'],))
                    if not structured_identity:
                        same = candidate
        if not same and rejected_redirect and domain and not source_url:
            # Find a past card whose website was this URL or whose old evidence
            # was the exact directory page reached through this very domain.
            requested, rejected = rejected_redirect
            same = unambiguous(self.store.query(
                "SELECT c.* FROM companies c WHERE c.domain=? AND (c.country=? OR c.country='' OR ?='') "
                "AND (c.url=? OR (c.url='' AND EXISTS (SELECT 1 FROM company_fit f "
                "JOIN json_each(CASE WHEN json_valid(f.source_ids) THEN f.source_ids ELSE '[]' END) j "
                "JOIN sources s ON s.id=j.value WHERE f.company_id=c.id AND s.url=?)))",
                (domain, country, country, requested, rejected)))
        if not same and domain and not source_url.startswith('https://www.openstreetmap.org/'):
            candidates = []
            for row in self.store.query('SELECT * FROM companies WHERE domain=? AND (country=? OR length(country)=0 OR ?=\'\')', (domain, country, country)):
                if (legal_id and row['legal_id'] and legal_id != row['legal_id']
                        and row['legal_id'] not in external_legal_ids):
                    continue
                if branch_name or row['branch_name']:
                    if clean_text(row['branch_name']) != clean_text(branch_name):
                        continue
                    if address and row['address'] and clean_text(row['address']) != clean_text(address):
                        continue
                # Same domain alone cannot merge different legal entities or branches.
                if company_identity(row['name']) == company_identity(name):
                    candidates.append(row)
            same = unambiguous(candidates)
        if same:
            replace_legal_id = same['legal_id'] in external_legal_ids
            with self.store.db as db:
                db.execute('UPDATE companies SET name=CASE WHEN manual=1 OR ?=\'\' OR ?=\'\' THEN name ELSE ? END,'
                           'country=CASE WHEN length(country)=0 THEN ? ELSE country END,manual=MAX(manual,?),'
                           'legal_id=CASE WHEN legal_id=\'\' OR ? THEN ? ELSE legal_id END,'
                           'url=CASE WHEN url=\'\' THEN ? ELSE url END,domain=CASE WHEN domain=\'\' THEN ? ELSE domain END,'
                           'branch_name=CASE WHEN branch_name=\'\' THEN ? ELSE branch_name END,'
                           'address=CASE WHEN address=\'\' THEN ? ELSE address END,'
                           'region=CASE WHEN region=\'\' THEN ? ELSE region END,'
                           'organization_key=CASE WHEN organization_key=\'\' THEN ? ELSE organization_key END,'
                           'identity_key=CASE WHEN identity_key=\'\' THEN ? ELSE identity_key END WHERE id=?',
                           (url, name, name, country, int(manual), replace_legal_id, legal_id, url, domain,
                            branch_name, address, region, organization_value, identity_key, same['id']))
                if replace_legal_id:
                    db.execute('INSERT INTO audit_events(event,details,created_at) VALUES(?,?,?)',
                               ('company_legal_id_retired', json.dumps({'company_id': same['id'],
                                   'previous_legal_id': same['legal_id']}), now()))
            return same['id']
        try:
            return self.store.execute(
                'INSERT INTO companies(name,domain,url,country,legal_id,manual,created_at,branch_name,address,region,organization_key,identity_key) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (name, domain, url, country, legal_id, int(manual), now(), branch_name, address, region,
                 organization_value, identity_key))
        except sqlite3.IntegrityError:
            existing = self.store.one('SELECT * FROM companies WHERE identity_key=?', (identity_key,))
            if existing and compatible(existing):
                return existing['id']
            # Same site/name without a separating legal id: keep a distinct card.
            fallback = identity_key + '|#' + hashlib.sha256(
                f'{name}|{domain}|{url}|{country}|{legal_id}|{branch_name}|{address}'.encode()).hexdigest()[:12]
            return self.store.execute(
                'INSERT INTO companies(name,domain,url,country,legal_id,manual,created_at,branch_name,address,region,organization_key,identity_key) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (name, domain, url, country, legal_id, int(manual), now(), branch_name, address, region,
                 organization_value, fallback))

    def _confirmed_company_legal_id(self, company, legal_id):
        sources = self.store.query(
            "SELECT DISTINCT s.url,s.text FROM company_fit f "
            "JOIN json_each(CASE WHEN json_valid(f.source_ids) THEN f.source_ids ELSE '[]' END) j "
            "JOIN sources s ON s.id=j.value WHERE f.company_id=?", (company['id'],))
        confirmed = False
        for source in sources:
            if domain_of(source['url']) != company['domain']:
                continue
            own, other = legal_id_roles(source['text'])
            if legal_id in other:
                return False
            if own == {legal_id}:
                confirmed = True
        return confirmed

