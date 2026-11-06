"""Neutral data models for comourat: posts, papers, and paper graph records.

These models carry only collection- and enrichment-relevant data. Influence
score fields hold in-flight scores written by comourat's assessors; relevance
scores and research-context fields belong to the consuming application.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Protocol

from pydantic import BaseModel, Field

# -- Paper graph models (moved from mourat.clients.paper_graph) --

_ARXIV_DOI_RE = re.compile(r"^10\.48550/arxiv\.(.+)$", re.IGNORECASE)
_ARXIV_ID_RE = re.compile(
    r"^(?:arxiv:)?([0-9]{4}\.\d{4,5})(?:v[0-9]+)?$", re.IGNORECASE
)


class PaperIdentity(BaseModel):
    """Canonical paper identity; provider IDs are deliberately absent."""

    arxiv_id: str | None = None
    doi: str | None = None

    @classmethod
    def from_values(
        cls, arxiv_id: str | None = None, doi: str | None = None
    ) -> "PaperIdentity":
        normalized_arxiv = normalize_arxiv_id(arxiv_id)
        normalized_doi = normalize_doi(doi)
        if normalized_doi is not None:
            match = _ARXIV_DOI_RE.match(normalized_doi)
            if match:
                normalized_arxiv = normalized_arxiv or normalize_arxiv_id(
                    match.group(1)
                )
                normalized_doi = None
        return cls(arxiv_id=normalized_arxiv, doi=normalized_doi)


def normalize_arxiv_id(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    match = _ARXIV_ID_RE.match(value)
    return match.group(1) if match else value


def normalize_doi(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    value = re.sub(r"^https?://doi\.org/", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^doi:", "", value, flags=re.IGNORECASE)
    return value or None


class PaperRecord(BaseModel):
    """Provider-neutral paper record returned by a graph client."""

    identity: PaperIdentity = Field(default_factory=PaperIdentity)
    title: str
    authors: list[str] = Field(default_factory=list)
    abstract: str = ""
    publication_date: date | None = None
    citation_count: int | None = None
    raw_influence: dict[str, float | int] = Field(default_factory=dict)


class PaperPage(BaseModel):
    """A page of records; continuation is opaque to the caller."""

    papers: list[PaperRecord] = Field(default_factory=list)
    continuation: Any = None


class PaperGraphClient(Protocol):
    """Operations required by seed resolution and expansion."""

    def resolve_by_arxiv_id(self, arxiv_id: str) -> PaperRecord | None: ...

    def resolve_by_doi(self, doi: str) -> PaperRecord | None: ...

    def search_papers(self, query: str, continuation: Any = None) -> PaperPage: ...

    def get_citations(
        self, identity: PaperIdentity, continuation: Any = None
    ) -> PaperPage: ...

    def get_references(
        self, identity: PaperIdentity, limit: int | None = None
    ) -> list[PaperRecord]: ...


# -- Post models --


class RedditPostInfo(BaseModel):
    """A single Reddit post."""

    subreddit: str
    submission_id: str
    title: str
    author: str
    date: str  # ISO format datetime string
    url: str
    text: str = ""
    score: int = 0
    influence_score: int | None = None


class RedditPostCollection(BaseModel):
    """Collection of Reddit posts."""

    posts: list[RedditPostInfo]


class HNPostInfo(BaseModel):
    """A single HackerNews story."""

    hn_id: str
    title: str
    author: str
    date: str  # ISO format datetime string
    url: str
    text: str = ""  # story_text when present, else empty
    points: int = 0
    num_comments: int = 0
    influence_score: int | None = None
    description: str = ""  # compact summary produced by the summarizer


class HNPostCollection(BaseModel):
    """Collection of HackerNews posts."""

    posts: list[HNPostInfo]


# -- Paper candidate models --


class PaperCandidate(BaseModel):
    """A paper candidate with collection metadata used by library components.

    `provenance` records the methods that found the candidate. `announce_type`
    preserves the arXiv feed type, and `affiliations` stores fetched author
    affiliations when available. These fields are optional so other collection
    sources can leave them unset or empty.
    """

    title: str = Field(description="Title of the paper as discovered")
    authors: list[str] = Field(description="Authors of the paper, as found")
    description: str = Field(default="", description="Short description of the paper")
    urls_seen: list[str] = Field(
        default_factory=list, description="URLs encountered while discovering the paper"
    )
    arxiv_id: str | None = None
    doi: str | None = None
    publication_date: str | None = Field(
        default=None,
        description=(
            "ISO publication date when the producer supplies one deterministically"
        ),
    )
    influence_score: int | None = Field(
        default=None,
        description="In-flight influence score written by the influence assessors",
    )
    provenance: list[str] = Field(
        default_factory=list,
        description="Collection or discovery methods that produced this candidate",
    )
    announce_type: str | None = Field(
        default=None,
        description="arXiv feed announce type, when supplied by the source",
    )
    affiliations: dict[str, list[str]] | None = Field(
        default=None,
        description="Fetched author affiliations, or None when unavailable",
    )


class PaperCandidateCollection(BaseModel):
    """Collection of discovered paper candidates."""

    papers: list[PaperCandidate]


class PaperInfo(BaseModel):
    title: str = Field(description="Title of the paper")
    link: str = Field(description="Arxiv URL of the paper")
    abstract: str = Field(description="Abstract of the paper")
    citation_count: int | None = Field(description="Number of citations", default=None)
    authors: list[str] | None = Field(description="Authors of the paper", default=None)
    publication_date: date | None = Field(
        description="Date of publication", default=None
    )


class PaperInfoCollection(BaseModel):
    """Collection of paper infos from a source search."""

    papers: list[PaperInfo]


class EnrichmentResult(BaseModel):
    """LLM enrichment agent output: additional context points."""

    additional_context: list[str] = Field(
        description=(
            "Short self-contained context points with facts obtained from the web "
            "that are not already evident in the item"
        )
    )
