"""Semantic Scholar Graph API client."""

from __future__ import annotations

import logging
import random
import time
from datetime import date
from typing import Any
from urllib.parse import quote

import requests

from comourat.models import (
    AuthorRecord,
    PaperIdentity,
    PaperPage,
    PaperRecord,
)
from comourat.proxy import validate_proxy_url

logger = logging.getLogger(__name__)

BASE_URL = "https://api.semanticscholar.org/graph/v1"
FIELDS = "title,authors,abstract,publicationDate,citationCount,externalIds"
# The provider accepts at most 1000 IDs in one author-batch request.
AUTHOR_BATCH_SIZE = 1000


class SemanticScholarClient:
    """Plain client for Semantic Scholar's Graph API."""

    def __init__(
        self,
        api_key: str | None = None,
        api_url: str = BASE_URL,
        proxy: str | None = None,
        timeout_seconds: float = 30.0,
        max_retries: int = 4,
        backoff_seconds: float = 2.0,
        regular_delay_seconds: float = 4.0,
        jitter_seconds: float = 1.0,
        page_size: int = 100,
        citation_sort: str = "provider",
        citation_scan_budget: int | None = None,
        http_client: Any | None = None,
    ) -> None:
        self.api_key = api_key
        self.api_url = api_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.regular_delay_seconds = regular_delay_seconds
        self.jitter_seconds = jitter_seconds
        self.page_size = page_size
        if citation_sort not in {"provider", "citation_count"}:
            raise ValueError("citation_sort must be 'provider' or 'citation_count'")
        if citation_scan_budget is not None and citation_scan_budget < 1:
            raise ValueError("citation_scan_budget must be positive when set")
        self.citation_sort = citation_sort
        self.citation_scan_budget = citation_scan_budget
        self._session = http_client or requests.Session()
        if hasattr(self._session, "headers"):
            self._session.headers.update({"User-Agent": "mourat/0.1"})
        if proxy and hasattr(self._session, "proxies"):
            validated = validate_proxy_url(proxy)
            self._session.proxies.update({"http": validated, "https": validated})

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = dict(params or {})
        params.setdefault("fields", FIELDS)
        return self._request("GET", path, params=params)

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        headers = {"x-api-key": self.api_key} if self.api_key else {}
        url = f"{self.api_url}/{quote(path.lstrip('/'), safe='/:')}"
        for attempt in range(self.max_retries + 1):
            request = self._session.get if method == "GET" else self._session.post
            kwargs: dict[str, Any] = {
                "params": dict(params or {}),
                "headers": headers,
                "timeout": self.timeout_seconds,
            }
            if json_body is not None:
                kwargs["json"] = json_body
            response = request(url, **kwargs)
            if response.status_code == 200:
                if self.regular_delay_seconds:
                    time.sleep(self.regular_delay_seconds)
                return response.json()
            if response.status_code not in (429,) and response.status_code < 500:
                response.raise_for_status()
            if attempt >= self.max_retries:
                response.raise_for_status()
            retry_after = response.headers.get("Retry-After")
            if retry_after is not None:
                try:
                    delay = max(float(retry_after), 0.0)
                except ValueError:
                    delay = self.backoff_seconds * (2**attempt)
            else:
                delay = self.backoff_seconds * (2**attempt)
            delay += random.uniform(0.0, self.jitter_seconds)
            logger.warning(
                "Semantic Scholar request failed with %s; retrying in %.1fs",
                response.status_code,
                delay,
            )
            if delay:
                time.sleep(delay)
        raise RuntimeError("unreachable")

    @staticmethod
    def _identity(payload: dict[str, Any]) -> PaperIdentity:
        ids = payload.get("externalIds") or {}
        return PaperIdentity.from_values(arxiv_id=ids.get("ArXiv"), doi=ids.get("DOI"))

    @classmethod
    def _record(cls, payload: dict[str, Any]) -> PaperRecord:
        publication_date = payload.get("publicationDate")
        return PaperRecord(
            identity=cls._identity(payload),
            title=payload.get("title") or "",
            authors=[
                a.get("name", "") for a in payload.get("authors") or [] if a.get("name")
            ],
            abstract=payload.get("abstract") or "",
            publication_date=(
                date.fromisoformat(publication_date) if publication_date else None
            ),
            citation_count=payload.get("citationCount"),
            raw_influence=(
                {"citation_count": payload["citationCount"]}
                if isinstance(payload.get("citationCount"), (int, float))
                else {}
            ),
        )

    def _resolve(self, key: str) -> PaperRecord | None:
        payload = self._get(f"paper/{key}")
        return self._record(payload) if payload.get("title") else None

    def resolve_by_arxiv_id(self, arxiv_id: str) -> PaperRecord | None:
        return self._resolve(f"ARXIV:{arxiv_id}")

    def resolve_by_doi(self, doi: str) -> PaperRecord | None:
        return self._resolve(f"DOI:{doi}")

    def search_papers(self, query: str, continuation: Any = None) -> PaperPage:
        params = {"query": query, "limit": self.page_size}
        if continuation is not None:
            params["offset"] = continuation
        payload = self._get("paper/search", params)
        return PaperPage(
            papers=[self._record(p) for p in payload.get("data") or []],
            continuation=payload.get("next"),
        )

    def get_authors(self, author_ids: list[str]) -> list[AuthorRecord]:
        """Return available author names and affiliations in input chunk order."""
        records: list[AuthorRecord] = []
        for start in range(0, len(author_ids), AUTHOR_BATCH_SIZE):
            batch = author_ids[start : start + AUTHOR_BATCH_SIZE]
            payload = self._request(
                "POST",
                "author/batch",
                params={"fields": "name,affiliations"},
                json_body={"ids": batch},
            )
            for author in payload or []:
                if not isinstance(author, dict):
                    continue
                author_id = author.get("authorId")
                if not author_id:
                    continue
                records.append(
                    AuthorRecord(
                        author_id=author_id,
                        name=author.get("name") or "",
                        affiliations=author.get("affiliations") or [],
                    )
                )
        return records

    def _provider_id(self, identity: PaperIdentity) -> str:
        if identity.arxiv_id:
            payload = self._get(
                f"paper/ARXIV:{identity.arxiv_id}", {"fields": "paperId"}
            )
        elif identity.doi:
            payload = self._get(f"paper/DOI:{identity.doi}", {"fields": "paperId"})
        else:
            raise ValueError("paper identity must contain arxiv_id or doi")
        return payload["paperId"]

    def get_citations(
        self, identity: PaperIdentity, continuation: Any = None
    ) -> PaperPage:
        paper_id = self._provider_id(identity)
        params = {"limit": self.page_size}
        if continuation is not None:
            params["offset"] = continuation
        payload = self._get(f"paper/{paper_id}/citations", params)
        papers = [
            self._record(x["citingPaper"])
            for x in payload.get("data") or []
            if x.get("citingPaper")
        ]
        if self.citation_sort == "citation_count":
            scan_budget = self.citation_scan_budget or self.page_size
            papers = papers[:scan_budget]
            next_offset = payload.get("next")
            while len(papers) < scan_budget and next_offset is not None:
                remaining = scan_budget - len(papers)
                page_params = {
                    "limit": min(self.page_size, remaining),
                    "offset": next_offset,
                }
                page_payload = self._get(f"paper/{paper_id}/citations", page_params)
                page = [
                    self._record(x["citingPaper"])
                    for x in page_payload.get("data") or []
                    if x.get("citingPaper")
                ]
                if not page:
                    break
                papers.extend(page)
                next_offset = page_payload.get("next")
            papers.sort(key=lambda paper: paper.citation_count or 0, reverse=True)
            return PaperPage(papers=papers, continuation=None)
        return PaperPage(papers=papers, continuation=payload.get("next"))

    def get_references(
        self, identity: PaperIdentity, limit: int | None = None
    ) -> list[PaperRecord]:
        paper_id = self._provider_id(identity)
        out: list[PaperRecord] = []
        offset: int | None = None
        while limit is None or len(out) < limit:
            request_limit = (
                self.page_size
                if limit is None
                else min(self.page_size, limit - len(out))
            )
            params: dict[str, Any] = {"limit": request_limit}
            if offset is not None:
                params["offset"] = offset
            payload = self._get(f"paper/{paper_id}/references", params)
            page = [
                self._record(x["citedPaper"])
                for x in payload.get("data") or []
                if x.get("citedPaper")
            ]
            out.extend(page)
            next_offset = payload.get("next")
            if next_offset is None or not page:
                break
            offset = next_offset
        return out if limit is None else out[:limit]
