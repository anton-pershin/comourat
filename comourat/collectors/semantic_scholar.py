import datetime
import json
import logging
import math
import random
import time
from typing import Any, Callable, Literal, TypeAlias, TypeVar

import httpx

from comourat.models import (
    PaperCandidate,
    PaperIdentity,
    PaperInfo,
    PaperInfoCollection,
    SemanticScholarBulkSearchResult,
    SemanticScholarQueryResult,
)

logger = logging.getLogger(__name__)

SemanticScholarSearchMode: TypeAlias = Literal[
    "newest", "most_relevant", "most_influential", "publication_window"
]

BULK_CANDIDATE_FIELDS = "paperId,title,url,abstract,publicationDate,authors,externalIds"

PaperT = TypeVar("PaperT")


class SemanticScholarPaperCollectorCore:
    def __init__(
        self,
        http_client: httpx.Client,
        api_url: str,
        mode: SemanticScholarSearchMode,
        start_date: str | None = None,  # YYYY-MM-DD
        end_date: str | None = None,  # YYYY-MM-DD
        max_results: int | None = None,
        strict_keyword_query: str | None = None,
        *,
        queries: list[str] | None = None,
        max_results_per_query: int | None = None,
        api_key: str | None = None,
        timeout_seconds: float = 30.0,
        max_retries: int = 4,
        backoff_seconds: float = 2.0,
        regular_delay_seconds: float = 4.0,
        jitter_seconds: float = 1.0,
    ) -> None:
        self.http_client = http_client
        self.api_url = api_url

        self.start_date: datetime.date | None = None
        if start_date is not None:
            self.start_date = datetime.date.fromisoformat(start_date)

        self.end_date: datetime.date | None = None
        if end_date is not None:
            self.end_date = datetime.date.fromisoformat(end_date)

        if mode != "publication_window":
            if max_results is None:
                raise ValueError("max_results is required for legacy modes")
            if strict_keyword_query is None:
                raise ValueError("strict_keyword_query is required for legacy modes")
        self.max_results = max_results if max_results is not None else 0
        self.strict_keyword_query = strict_keyword_query or ""
        self.queries = [query.strip() for query in queries or []]
        self.max_results_per_query = max_results_per_query
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.regular_delay_seconds = regular_delay_seconds
        self.jitter_seconds = jitter_seconds

        if mode == "publication_window":
            self._validate_publication_window_configuration(queries)

        self.mode = mode
        self.mode_to_handler: dict[
            str,
            Callable[[], PaperInfoCollection | SemanticScholarBulkSearchResult],
        ] = {
            "newest": self._handle_newest_mode,
            "most_relevant": self._handle_most_relevant_mode,
            "most_influential": self._handle_most_influential_mode,
            "publication_window": self._handle_publication_window_mode,
        }

    def _validate_publication_window_configuration(
        self, queries: list[str] | None
    ) -> None:
        if not queries or any(not query.strip() for query in queries):
            raise ValueError("queries must contain at least one non-empty query")
        if self.start_date is None or self.end_date is None:
            raise ValueError("publication_window mode requires start_date and end_date")
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        if self.max_results_per_query is not None and self.max_results_per_query < 1:
            raise ValueError("max_results_per_query must be positive when set")
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative")

    def collect(self) -> PaperInfoCollection | SemanticScholarBulkSearchResult:
        return self.mode_to_handler[self.mode]()

    def _handle_newest_mode(self) -> PaperInfoCollection:
        request_data = {
            "query": self.strict_keyword_query,
            "sort": "publicationDate:desc",
        }

        return self._get_papers_via_bulk_search(request_data)

    def _handle_most_relevant_mode(self) -> PaperInfoCollection:
        output = PaperInfoCollection(papers=[])

        n_papers_processed = 0
        request_data: dict[str, Any] = {
            "query": self.strict_keyword_query,
        }
        next_offset: int | None = None

        while n_papers_processed < self.max_results:
            if next_offset is not None:
                request_data["offset"] = next_offset

            papers, r_data = self._run_one_paper_request_via_api(
                full_api_url=self.api_url + "/paper/search",
                request_data=request_data,
                n_papers_processed=n_papers_processed,
            )
            output.papers.extend(papers)
            n_papers_processed = len(output.papers)

            if r_data["next"] is None:  # present only if we can fetch more results
                break

            next_offset = r_data["next"]

        return output

    def _handle_most_influential_mode(self) -> PaperInfoCollection:
        request_data = {
            "query": self.strict_keyword_query,
            "sort": "citationCount:desc",
        }

        return self._get_papers_via_bulk_search(request_data)

    def _handle_publication_window_mode(self) -> SemanticScholarBulkSearchResult:
        output = SemanticScholarBulkSearchResult(papers=[], query_results=[])
        for query in self.queries:
            papers, cap_reached = self._get_candidates_via_bulk_search(query)
            output.papers.extend(papers)
            output.query_results.append(
                SemanticScholarQueryResult(
                    query=query,
                    candidate_count=len(papers),
                    cap_reached=cap_reached,
                )
            )
            if cap_reached:
                logger.warning(
                    "Semantic Scholar query reached max_results_per_query=%d: %s",
                    self.max_results_per_query,
                    query,
                )
        return output

    def _prepare_bulk_search_request(
        self, request_data: dict[str, Any], fields: str
    ) -> None:
        request_data["fields"] = fields
        publication_date_range = (
            self.start_date.isoformat() if self.start_date is not None else ""
        )
        publication_date_range += ":"
        publication_date_range += (
            self.end_date.isoformat() if self.end_date is not None else ""
        )
        if publication_date_range != ":":
            request_data["publicationDateOrYear"] = publication_date_range

    def _collect_via_bulk_search(
        self,
        request_data: dict[str, Any],
        max_results: int | None,
        request_one_page: Callable[
            [str, dict[str, Any], int], tuple[list[PaperT], dict[str, Any]]
        ],
    ) -> tuple[list[PaperT], bool]:
        output: list[PaperT] = []
        token: str | None = None
        cap_reached = False

        while max_results is None or len(output) < max_results:
            if token is not None:
                request_data["token"] = token

            papers, r_data = request_one_page(
                self.api_url + "/paper/search/bulk",
                request_data,
                len(output),
            )
            output.extend(papers)
            if max_results is not None and len(output) >= max_results:
                cap_reached = True
                break

            if r_data["token"] is None:
                break
            token = r_data["token"]

        return output, cap_reached

    def _get_papers_via_bulk_search(
        self,
        request_data: dict[str, Any],
    ) -> PaperInfoCollection:
        self._prepare_bulk_search_request(
            request_data, "title,url,abstract,citationCount,publicationDate,authors"
        )
        papers, _ = self._collect_via_bulk_search(
            request_data,
            self.max_results,
            self._run_one_paper_request_via_api,
        )
        return PaperInfoCollection(papers=papers)

    def _get_candidates_via_bulk_search(
        self, query: str
    ) -> tuple[list[PaperCandidate], bool]:
        request_data: dict[str, Any] = {
            "query": query,
            "sort": "publicationDate:desc",
        }
        self._prepare_bulk_search_request(request_data, BULK_CANDIDATE_FIELDS)
        return self._collect_via_bulk_search(
            request_data,
            self.max_results_per_query,
            self._run_one_candidate_request_via_api,
        )

    def _run_one_paper_request_via_api(
        self,
        full_api_url: str,
        request_data: dict[str, Any],
        n_papers_processed: int,
    ) -> tuple[list[PaperInfo], dict[str, Any]]:
        received = False
        while not received:
            r: httpx.Response = self.http_client.get(
                full_api_url,
                params=request_data,
            )
            r_data = json.loads(r.text)
            if "code" not in r_data:
                received = True
            else:
                error_code = int(r_data["code"])
                sleep_s = 5
                logger.warning(
                    "Got error code %d: '%s'. Will retry in %d seconds",
                    error_code,
                    r_data["message"],
                    sleep_s,
                )
                time.sleep(sleep_s)

        papers: list[PaperInfo] = []

        for ss_paper_data in r_data["data"]:
            if self._mandatory_fields_absent(ss_paper_data):
                continue

            publication_date: datetime.date | None = None
            if ss_paper_data["publicationDate"] is not None:
                publication_date = datetime.date.fromisoformat(
                    ss_paper_data["publicationDate"]
                )

            papers.append(
                PaperInfo(
                    title=ss_paper_data["title"],
                    link=ss_paper_data["url"],
                    abstract=ss_paper_data["abstract"],
                    citation_count=ss_paper_data["citationCount"],
                    authors=[
                        author_data["name"] for author_data in ss_paper_data["authors"]
                    ],
                    author_ids=[
                        author_data.get("authorId")
                        for author_data in ss_paper_data["authors"]
                    ],
                    publication_date=publication_date,
                )
            )
            n_papers_processed += 1
            if n_papers_processed >= self.max_results:
                break

        return papers, r_data

    def _run_one_candidate_request_via_api(
        self,
        full_api_url: str,
        request_data: dict[str, Any],
        n_papers_processed: int,
    ) -> tuple[list[PaperCandidate], dict[str, Any]]:
        r_data = self._get_with_bounded_retries(full_api_url, request_data)
        papers: list[PaperCandidate] = []
        for ss_paper_data in r_data.get("data") or []:
            candidate = self._candidate_from_bulk_data(ss_paper_data)
            if candidate is None:
                continue
            papers.append(candidate)
            n_papers_processed += 1
            if (
                self.max_results_per_query is not None
                and n_papers_processed >= self.max_results_per_query
            ):
                break
        return papers, r_data

    def _get_with_bounded_retries(
        self, full_api_url: str, request_data: dict[str, Any]
    ) -> dict[str, Any]:
        headers = {"x-api-key": self.api_key} if self.api_key else {}
        for attempt in range(self.max_retries + 1):
            try:
                response = self.http_client.get(
                    full_api_url,
                    params=dict(request_data),
                    headers=headers,
                    timeout=self.timeout_seconds,
                )
            except httpx.RequestError as exc:
                if attempt >= self.max_retries:
                    raise
                delay = self._retry_delay(attempt)
                logger.warning(
                    "Semantic Scholar transport failed with %s; retrying in %.1fs",
                    type(exc).__name__,
                    delay,
                )
                if delay:
                    time.sleep(delay)
                continue

            if response.status_code == 200:
                if self.regular_delay_seconds:
                    time.sleep(self.regular_delay_seconds)
                return response.json()
            if response.status_code != 429 and response.status_code < 500:
                response.raise_for_status()
            if attempt >= self.max_retries:
                response.raise_for_status()

            delay = self._retry_delay(attempt, response.headers.get("Retry-After"))
            logger.warning(
                "Semantic Scholar bulk search failed with %s; retrying in %.1fs",
                response.status_code,
                delay,
            )
            if delay:
                time.sleep(delay)
        raise RuntimeError("unreachable")

    def _retry_delay(self, attempt: int, retry_after: str | None = None) -> float:
        delay = self.backoff_seconds * (2**attempt)
        if retry_after is not None:
            try:
                retry_after_delay = float(retry_after)
            except ValueError:
                pass
            else:
                if math.isfinite(retry_after_delay) and retry_after_delay >= 0:
                    delay = retry_after_delay
        return delay + random.uniform(0.0, self.jitter_seconds)

    def _candidate_from_bulk_data(
        self, ss_paper_data: dict[str, Any]
    ) -> PaperCandidate | None:
        title = ss_paper_data.get("title")
        if not title:
            return None
        raw_publication_date = ss_paper_data.get("publicationDate")
        if not raw_publication_date:
            return None
        try:
            publication_date = datetime.date.fromisoformat(raw_publication_date)
        except (TypeError, ValueError):
            return None
        if (
            self.start_date is None
            or self.end_date is None
            or not self.start_date <= publication_date <= self.end_date
        ):
            return None

        external_ids = ss_paper_data.get("externalIds") or {}
        identity = PaperIdentity.from_values(
            arxiv_id=external_ids.get("ArXiv"),
            doi=external_ids.get("DOI"),
        )
        url = ss_paper_data.get("url")
        return PaperCandidate(
            title=title,
            authors=[
                author["name"]
                for author in ss_paper_data.get("authors") or []
                if author.get("name")
            ],
            description=ss_paper_data.get("abstract") or "",
            urls_seen=[url] if url else [],
            arxiv_id=identity.arxiv_id,
            doi=identity.doi,
            semantic_scholar_id=ss_paper_data.get("paperId"),
            publication_date=publication_date.isoformat(),
            provenance=["semantic_scholar"],
        )

    @staticmethod
    def _mandatory_fields_absent(ss_paper_data: dict[str, Any]) -> bool:
        mandatory_fields = ["title", "url", "abstract"]
        return any(ss_paper_data[f] is None for f in mandatory_fields)
