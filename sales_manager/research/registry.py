"""Adapter capabilities: one registry drives routing and source totals."""
from dataclasses import dataclass

from .providers import (TavilyProvider, SerperProvider, ExaProvider, FirecrawlProvider,
                        JinaProvider, GDELTProvider, RSSProvider, PublicPagesProvider,
                        OverpassProvider, WikidataProvider, MediaWikiProvider, ComtradeProvider)


@dataclass(frozen=True)
class AdapterSpec:
    factory: type
    capabilities: frozenset[str]
    keyed: bool = False


ADAPTERS = {
    'tavily': AdapterSpec(TavilyProvider, frozenset({'market', 'companies'}), True),
    'serper': AdapterSpec(SerperProvider, frozenset({'market', 'companies'}), True),
    'exa': AdapterSpec(ExaProvider, frozenset({'market', 'companies'}), True),
    'firecrawl': AdapterSpec(FirecrawlProvider, frozenset({'market', 'companies'}), True),
    'jina': AdapterSpec(JinaProvider, frozenset({'market', 'companies'}), True),
    'gdelt': AdapterSpec(GDELTProvider, frozenset({'market', 'companies'})),
    'rss': AdapterSpec(RSSProvider, frozenset({'market', 'companies'})),
    'procurement': AdapterSpec(PublicPagesProvider, frozenset({'market', 'companies'})),
    'overpass': AdapterSpec(OverpassProvider, frozenset({'companies'})),
    'wikidata': AdapterSpec(WikidataProvider, frozenset({'companies'})),
    'mediawiki': AdapterSpec(MediaWikiProvider, frozenset({'companies'})),
    'catalog': AdapterSpec(PublicPagesProvider, frozenset({'companies'})),
    'comtrade': AdapterSpec(ComtradeProvider, frozenset({'imports'})),
}


def market_source_names():
    return tuple(name for name, adapter in ADAPTERS.items() if 'market' in adapter.capabilities)
