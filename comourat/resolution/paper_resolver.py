"""Resolve neutral paper candidates to canonical paper records."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from enum import StrEnum

from pydantic import BaseModel

from comourat.models import PaperCandidate, PaperGraphClient, PaperRecord

_ARXIV_ID_IN_URL = re.compile(
    r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.\d{4,5})(?:v[0-9]+)?/?",
    re.IGNORECASE,
)
_WHITESPACE_RUN = re.compile(r"\s+")


class PaperResolutionStatus(StrEnum):
    """Outcome of resolving one candidate."""

    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"


class PaperResolution(BaseModel):
    """Canonical record or reason why a candidate could not be resolved."""

    candidate_index: int
    candidate: PaperCandidate
    status: PaperResolutionStatus
    record: PaperRecord | None = None
    reason: str | None = None


class PaperResolutionCollection(BaseModel):
    """Resolution outcomes in the same order as the input candidates."""

    resolutions: list[PaperResolution]


def extract_arxiv_id(urls: list[str]) -> str | None:
    """Return the first arXiv ID found in an abstract or PDF URL."""
    for url in urls:
        match = _ARXIV_ID_IN_URL.search(url)
        if match:
            return match.group(1)
    return None


def _normalize_title(title: str) -> str:
    return _WHITESPACE_RUN.sub(" ", title.strip()).casefold()


def _titles_match(claimed: str, resolved: str, threshold: float) -> bool:
    return (
        SequenceMatcher(
            None, _normalize_title(claimed), _normalize_title(resolved)
        ).ratio()
        >= threshold
    )


class PaperResolverCore:
    """Resolve candidates through a paper graph client.

    For each candidate, try an arXiv ID (explicit or found in its URLs), then
    a DOI, then title search. Accept a provider record only when its title
    meets the configured similarity threshold. Provider failures become
    explicit unresolved outcomes so a caller can decide how to handle them.
    """

    def __init__(
        self,
        paper_graph_client: PaperGraphClient,
        title_similarity_threshold: float = 0.9,
    ) -> None:
        self.client = paper_graph_client
        self.title_similarity_threshold = title_similarity_threshold

    def _resolve_candidate(
        self, candidate: PaperCandidate
    ) -> tuple[PaperRecord | None, str | None]:
        arxiv_id = candidate.arxiv_id or extract_arxiv_id(candidate.urls_seen)
        record = self.client.resolve_by_arxiv_id(arxiv_id) if arxiv_id else None
        if record is None and candidate.doi:
            record = self.client.resolve_by_doi(candidate.doi)
        if record is None:
            page = self.client.search_papers(candidate.title)
            record = page.papers[0] if page.papers else None
        if record is None:
            return None, "no_paper_found"
        if not _titles_match(
            candidate.title, record.title, self.title_similarity_threshold
        ):
            return None, "provider_title_mismatch"
        return record, None

    def resolve(self, candidates: list[PaperCandidate]) -> PaperResolutionCollection:
        """Resolve each candidate; preserve input order and report failures."""
        outcomes: list[PaperResolution] = []
        for index, candidate in enumerate(candidates):
            try:
                record, reason = self._resolve_candidate(candidate)
            except Exception:
                record, reason = None, "api_error"
            if record is None:
                outcomes.append(
                    PaperResolution(
                        candidate_index=index,
                        candidate=candidate,
                        status=PaperResolutionStatus.UNRESOLVED,
                        reason=reason,
                    )
                )
            else:
                outcomes.append(
                    PaperResolution(
                        candidate_index=index,
                        candidate=candidate,
                        status=PaperResolutionStatus.RESOLVED,
                        record=record,
                    )
                )
        return PaperResolutionCollection(resolutions=outcomes)
