"""Paper graph identity and record models: re-exported from comourat.models.

The canonical definitions live in `comourat.models` (single source of truth
for the neutral model set). This module exists so the historical import path
`comourat.clients.paper_graph` keeps working for callers that treat these
types as client-layer types.
"""

from comourat.models import (
    PaperGraphClient,
    PaperIdentity,
    PaperPage,
    PaperRecord,
    normalize_arxiv_id,
    normalize_doi,
)

__all__ = [
    "PaperGraphClient",
    "PaperIdentity",
    "PaperPage",
    "PaperRecord",
    "normalize_arxiv_id",
    "normalize_doi",
]
