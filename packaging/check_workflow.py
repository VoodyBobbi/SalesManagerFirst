"""Opt-in live manual-URL research in an isolated diagnostic database."""
import json
import sys
import threading
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sales_manager.storage import Store
from sales_manager.research import ResearchService, normalize_product


class EmptySecrets:
    def get(self, key, default=''):
        return default


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', default='build/live-workflow')
    parser.add_argument('--product', default='Бумага')
    parser.add_argument('--url', default='https://example.org')
    parser.add_argument('--country', default='Россия')
    parser.add_argument('--market', action='store_true')
    parser.add_argument('--discover', action='store_true')
    parser.add_argument('--request-limit', type=int, default=12)
    args = parser.parse_args()
    root = Path(args.directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    store = Store(root / 'research.sqlite3')
    secrets = EmptySecrets()
    product = store.ensure_product(args.product, normalize_product(args.product))
    service = ResearchService(store, secrets)
    store.set_setting('search.request_limit', args.request_limit)
    store.set_setting('search.page_limit', 2)
    if args.market:
        result = service.research_market([args.country], print, threading.Event())
        result['ranking'] = service.market_ranking([args.country])
    else:
        result = service.find_companies(product['id'], [args.country], print, threading.Event(), manual_url=None if args.discover else args.url)
    result['cards'] = store.query('SELECT c.name,c.url,c.country,f.status,f.reason,f.potential,f.activity_profile,f.manual FROM companies c JOIN company_fit f ON f.company_id=c.id WHERE f.product_id=?', (product['id'],))
    result['contacts'] = store.one('SELECT COUNT(*) n FROM company_contacts')['n']
    result['sources'] = store.one('SELECT COUNT(*) n FROM sources')['n']
    result['ai_usage'] = store.one("SELECT COUNT(*) n FROM usage_log WHERE provider='gigachat'")['n']
    (root / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
