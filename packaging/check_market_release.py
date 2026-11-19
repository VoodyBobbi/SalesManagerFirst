"""Explicit live market check, isolated from the user's database and credentials.

Run from the project root. Results and restartable state stay in build/.
This is deliberately not part of pytest and never connects a personal account.
"""
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sales_manager.research.normalization import COUNTRIES
from sales_manager.research.service import ResearchService
from sales_manager.storage import Store


class NoCredentials:
    def get(self, name, default=''):
        return default


def main():
    directory = ROOT / 'build' / 'live-market-release'
    if len(sys.argv) == 2 and sys.argv[1].isalnum():
        directory = directory / sys.argv[1]
    directory.mkdir(parents=True, exist_ok=True)
    store = Store(directory / 'market.sqlite3')
    store.set_setting('search.request_limit', 100)
    service = ResearchService(store, NoCredentials())
    cancel = threading.Event()
    timer = threading.Timer(180, cancel.set)
    timer.daemon = True
    started = time.monotonic()
    printed = [0.0]

    def progress(message):
        if time.monotonic() - printed[0] >= 15:
            printed[0] = time.monotonic()
            print(f'{time.monotonic() - started:.0f}s: {message}', flush=True)

    timer.start()
    try:
        result = service.research_market(COUNTRIES, progress, cancel)
        ranking = service.market_ranking(COUNTRIES)
        result.update(elapsed_seconds=round(time.monotonic() - started, 2),
                      scope='live public sources only; no personal credentials; 100-request batches, automatic continuation; 180s per pass',
                      rows=ranking['rows'], caption=ranking['caption'],
                      pending_sources=store.query("SELECT json_extract(payload,'$.provider') provider,COUNT(*) tasks FROM job_queue WHERE run_id=? AND status!='done' GROUP BY provider", (result['run_id'],)),
                      source_statuses={name: service.provider_status(name) for name in ('rss', 'gdelt', 'firecrawl')})
        (directory / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({key: result[key] for key in ('status', 'count', 'sources_used', 'sources_total', 'requests', 'message', 'elapsed_seconds')}, ensure_ascii=False), flush=True)
        print(json.dumps([row['name'] for row in ranking['rows']], ensure_ascii=False), flush=True)
    finally:
        timer.cancel()
        store.close()


if __name__ == '__main__':
    main()
