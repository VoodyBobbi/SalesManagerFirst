"""Real web research with persistent checkpoints and evidence."""
from .normalization import COUNTRIES, normalize_product
from .service import ResearchService

__all__ = ['COUNTRIES', 'normalize_product', 'ResearchService']
