"""Verify a canonical paper against arXiv and return a PDF URL or outcome."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from enum import StrEnum

from pydantic import BaseModel

from comourat.clients.arxiv import ARXIV_PDF_URL_TEMPLATE, ArxivClient
from comourat.models import PaperRecord

_WHITESPACE_RUN = re.compile(r"\s+")


class ArxivPdfVerificationStatus(StrEnum):
    """Neutral result categories for arXiv PDF verification."""

    VERIFIED = "verified"
    NOT_FOUND = "not_found"
    TITLE_MISMATCH = "title_mismatch"
    API_ERROR = "api_error"


class ArxivPdfVerificationOutcome(BaseModel):
    """Verification result for one canonical paper."""

    paper_index: int
    status: ArxivPdfVerificationStatus
    pdf_url: str | None = None
    reason: str | None = None


class ArxivPdfVerificationCollection(BaseModel):
    """Verification outcomes in the same order as the input records."""

    outcomes: list[ArxivPdfVerificationOutcome]


def _normalize_title(title: str) -> str:
    return _WHITESPACE_RUN.sub(" ", title.strip()).casefold()


def _titles_match(claimed: str, arxiv_title: str, threshold: float) -> bool:
    return (
        SequenceMatcher(
            None, _normalize_title(claimed), _normalize_title(arxiv_title)
        ).ratio()
        >= threshold
    )


class ArxivPdfVerifierCore:
    """Find and verify a matching arXiv preprint for each paper record."""

    def __init__(
        self,
        arxiv_client: ArxivClient,
        title_similarity_threshold: float,
    ) -> None:
        self.arxiv_client = arxiv_client
        self.title_similarity_threshold = title_similarity_threshold

    def _verify_one(
        self, paper: PaperRecord, paper_index: int
    ) -> ArxivPdfVerificationOutcome:
        found = self.arxiv_client.search_by_title(paper.title)
        if found is None:
            return ArxivPdfVerificationOutcome(
                paper_index=paper_index,
                status=ArxivPdfVerificationStatus.NOT_FOUND,
                reason="not_on_arxiv",
            )
        arxiv_id, arxiv_title = found
        if not _titles_match(paper.title, arxiv_title, self.title_similarity_threshold):
            return ArxivPdfVerificationOutcome(
                paper_index=paper_index,
                status=ArxivPdfVerificationStatus.TITLE_MISMATCH,
                reason="arxiv_title_mismatch",
            )
        return ArxivPdfVerificationOutcome(
            paper_index=paper_index,
            status=ArxivPdfVerificationStatus.VERIFIED,
            pdf_url=ARXIV_PDF_URL_TEMPLATE.format(arxiv_id=arxiv_id),
        )

    def verify(self, papers: list[PaperRecord]) -> ArxivPdfVerificationCollection:
        """Verify each record independently; request errors do not abort the batch."""
        outcomes: list[ArxivPdfVerificationOutcome] = []
        for index, paper in enumerate(papers):
            try:
                outcome = self._verify_one(paper, index)
            except Exception:
                outcome = ArxivPdfVerificationOutcome(
                    paper_index=index,
                    status=ArxivPdfVerificationStatus.API_ERROR,
                    reason="api_error",
                )
            outcomes.append(outcome)
        return ArxivPdfVerificationCollection(outcomes=outcomes)
