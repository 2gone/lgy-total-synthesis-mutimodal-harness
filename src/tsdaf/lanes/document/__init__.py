"""Direct document-lane loading and canonicalization API."""

from .canonicalizer import canonicalize_paper_package
from .package_loader import LoadedPaperPackage, PackageLoadError, load_paper_package
from .search_index import InMemorySearchIndex, SearchHit, SearchIndex

__all__ = [
    "InMemorySearchIndex",
    "LoadedPaperPackage",
    "PackageLoadError",
    "SearchHit",
    "SearchIndex",
    "canonicalize_paper_package",
    "load_paper_package",
]
