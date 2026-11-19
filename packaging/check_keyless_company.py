"""Bounded opt-in keyless company probe; never touches the user's database."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sales_manager.research.service import ResearchService
from sales_manager.research.providers import OverpassProvider, ProviderError
from sales_manager.storage.store import Store

class EmptySecrets:
    def get(self, name, default=''):
        return default

folder = Path(__file__).resolve().parents[1] / 'build' / 'keyless-company'
folder.mkdir(parents=True, exist_ok=True)
store = Store(folder / 'probe.sqlite3')
service = ResearchService(store, EmptySecrets())
service._limit = 4
store.set_setting('search.page_limit', 1)
output = {'provider': 'overpass', 'country': 'Армения', 'query': 'кофе', 'ok': False}
try:
    results = OverpassProvider(service._json).search('кофе', 'Армения', limit=3)
    output.update(ok=True, results=len(results), sources=[r.metadata.get('source_url') for r in results])
    product = store.ensure_product('кофе')
    selected = next((r for r in results if r.metadata.get('emails') or r.metadata.get('phones')), results[0] if results else None)
    if selected:
        service._company(product['id'], selected, False, lambda message: None)
        output['saved_companies'] = store.one('SELECT COUNT(*) n FROM companies')['n']
        output['saved_contacts'] = store.one('SELECT COUNT(*) n FROM company_contacts')['n']
        output['source_urls'] = [r['url'] for r in store.query('SELECT url FROM sources')]
    output['gigachat_requests'] = store.one("SELECT COUNT(*) n FROM usage_log WHERE provider='gigachat'")['n']
except Exception as exc:
    output.update(status=getattr(exc, 'status', 'Недоступен'), error=str(exc)[:300])
finally:
    store.close()
(folder / 'result.json').write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(output, ensure_ascii=True, indent=2))
