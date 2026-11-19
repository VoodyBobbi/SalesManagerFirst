"""Opt-in real RSS market check, isolated from user data, accounts and credentials."""
import argparse
import hashlib
import json
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sales_manager.research import ResearchService
from sales_manager.research.market import market_evidence
from sales_manager.research.providers import KEYED_PROVIDERS, KEYLESS_PROVIDERS, RSSProvider
from sales_manager.research.safe_http import FetchError, FetchResult
from sales_manager.storage import Store


class EmptySecrets:
    def get(self, key, default=''):
        return default


def check(directory, request_limit=90, timeout=240, replay_from=None):
    destination = Path(directory).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    snapshots = destination / 'responses'
    snapshots.mkdir(exist_ok=True)
    store = Store(destination / 'research.sqlite3')
    for name in {*KEYED_PROVIDERS, *KEYLESS_PROVIDERS}:
        store.set_setting('search.' + name + '_enabled', name == 'rss')
    store.set_setting('search.rss_urls', RSSProvider.DEFAULT_URLS)
    store.set_setting('search.free_confirmed', False)
    store.set_setting('search.request_limit', request_limit)
    service = ResearchService(store, EmptySecrets())
    recorded = {}
    replay_root = Path(replay_from).resolve() if replay_from else None
    if replay_root:
        replay_report = json.loads((replay_root / 'result.json').read_text(encoding='utf-8'))
        recorded = {item['url']: item for item in replay_report['transport']}

    def replay(url):
        entry = recorded.get(url)
        if not entry or 'snapshot' not in entry:
            raise FetchError('Нет сохранённого ответа: ' + url)
        content_type = entry.get('content_type', '')
        charset = re.search(r'charset=["\']?([\w.-]+)', content_type, re.I)
        return FetchResult(entry.get('resolved_url', url), entry['status'],
                           (replay_root / 'responses' / entry['snapshot']).read_bytes(),
                           content_type, charset[1] if charset else 'utf-8')
    requests = []
    progress = []
    before_usage = store.one('SELECT COUNT(*) n FROM usage_log')['n']

    def capture(method):
        def fetch(url):
            request = {'url': url, 'started_at': datetime.now(timezone.utc).isoformat()}
            started = time.monotonic()
            try:
                response = method(url)
                body = response.content
                name = hashlib.sha256(response.url.encode()).hexdigest() + '.txt'
                (snapshots / name).write_bytes(body)
                request.update(status=response.status, resolved_url=response.url,
                               content_type=response.content_type, bytes=len(body), snapshot=name)
                if replay_root:
                    request['replayed'] = True
                return response
            except Exception as exc:
                request['error'] = type(exc).__name__ + ': ' + str(exc)
                raise
            finally:
                request['seconds'] = round(time.monotonic() - started, 3)
                requests.append(request)
        return fetch

    # Capture only real transport responses; the application still enforces robots,
    # validated public DNS, TLS, byte limits, caching and its persistent task queue.
    service.fetcher.fetch = capture(replay if replay_root else service.fetcher.fetch)
    service.fetcher.fetch_document = capture(replay if replay_root else service.fetcher.fetch_document)
    cancel = threading.Event()
    timer = threading.Timer(timeout, cancel.set)
    started = time.monotonic()
    timer.start()
    result = {}

    def emit(message):
        progress.append(str(message))
        print(message, flush=True)

    try:
        result = service.research_market(['Россия'], emit, cancel)
    except Exception as exc:
        result = {'status': 'error', 'error': type(exc).__name__ + ': ' + str(exc)}
        emit(result['error'])
    finally:
        timer.cancel()
        ranking = service.market_ranking(['Россия'])
        result.update(
            checked_at=datetime.now(timezone.utc).isoformat(),
            seconds=round(time.monotonic() - started, 3),
            country='Россия', providers=['rss'], feed_urls=RSSProvider.DEFAULT_URLS,
            mode='reprocessed_saved_responses' if replay_root else 'live_network',
            replay_source=str(replay_root) if replay_root else None,
            network_attempts=0 if replay_root else len(requests),
            request_limit=request_limit, diagnostic_timeout=timeout,
            new_usage_records=store.one('SELECT COUNT(*) n FROM usage_log')['n'] - before_usage,
            ai_usage=store.one("SELECT COUNT(*) n FROM usage_log WHERE provider='gigachat'")['n'],
            ranking=ranking,
            facts={row['name']: [{key: source.get(key) for key in
                                  ('url', 'title', 'published_at', 'excerpt', 'fact')}
                                 for source in market_evidence(store, row['id'], ['Россия'])]
                   for row in ranking['rows']},
            observations=store.query('SELECT product_id,metric,value,unit,period,country,published_at,excerpt FROM market_observations'),
            queue=store.query('SELECT status,COUNT(*) n FROM job_queue GROUP BY status'),
            transport=requests, progress=progress,
        )
        (destination / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({key: result[key] for key in ('status', 'seconds', 'new_usage_records', 'ai_usage')}, ensure_ascii=False), flush=True)
        print(json.dumps([{'name': row['name'], 'basis': row['basis'], 'sources': row['count']}
                          for row in ranking['rows']], ensure_ascii=False, indent=2), flush=True)
        print('Report: ' + str(destination / 'result.json'), flush=True)
        store.close()
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', default='build/live-market-v13')
    parser.add_argument('--request-limit', type=int, default=90)
    parser.add_argument('--timeout', type=int, default=240)
    parser.add_argument('--replay-from', help='Reprocess captured responses without any network requests')
    arguments = parser.parse_args()
    check(arguments.directory, arguments.request_limit, arguments.timeout, arguments.replay_from)
