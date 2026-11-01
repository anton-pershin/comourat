"""Clients for external paper and post APIs (plain, non-framework helpers)."""

from comourat.clients.arxiv import ArxivClient
from comourat.clients.hn_algolia import HNAlgoliaClient
from comourat.clients.openalex import OpenAlexClient
from comourat.clients.paper_graph import PaperGraphClient
from comourat.clients.semantic_scholar import SemanticScholarClient

__all__ = [
    "ArxivClient",
    "HNAlgoliaClient",
    "OpenAlexClient",
    "PaperGraphClient",
    "SemanticScholarClient",
]
