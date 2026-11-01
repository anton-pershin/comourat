"""OpenAlex client: title search, citation-graph queries and single-work lookup.

Plain (non-`Function`) client called from within pipeline components. Owns the
request policy per NFR2/NFR4: identifying User-Agent, `select=` field lists,
the `cursor=*` sentinel on the first request of any paged call (without it
OpenAlex silently returns page 1 with no error), and bounded retry with backoff
on 429/5xx.
"""

import logging
import time
from datetime import date
from typing import Any
from urllib.parse import quote

import requests

from comourat.models import PaperIdentity, PaperPage, PaperRecord

logger = logging.getLogger(__name__)

OPENALEX_API_BASE_URL = "https://api.openalex.org/works"

# Fields requested on every work lookup/search: everything resolution and
# influence assessment need, nothing more (NFR2: request only needed fields).
DEFAULT_SELECT_FIELDS = [
    "id",
    "doi",
    "title",
    "display_name",
    "abstract_inverted_index",
    "authorships",
    "publication_date",
    "publication_year",
    "cited_by_count",
    "fwci",
    "primary_location",
    "locations",
    "best_oa_location",
    "referenced_works",
]


def _abstract_from_inverted_index(inv: dict[str, list[int]] | None) -> str:
    if not inv:
        return ""
    positions = [
        (position, word) for word, indexes in inv.items() for position in indexes
    ]
    return " ".join(word for _, word in sorted(positions))


class OpenAlexClient:
    """Client for the unauthenticated OpenAlex works API.

    Args mirror the future Hydra group file keys one-to-one. `http_client` is
    injectable for tests; by default a module-level `requests.Session` with the
    configured identifying User-Agent is used. `api_key` (optional) is sent as
    the `api_key` query parameter on every request for the authenticated,
    higher-rate-limit tier; `None` keeps the unauthenticated public tier.
    """

    def __init__(
        self,
        user_agent: str,
        select_fields: list[str] | None = None,
        max_retries: int = 4,
        regular_delay_seconds: float = 2.0,
        backoff_seconds: float = 2.0,
        timeout_seconds: float = 30.0,
        per_page: int = 25,
        api_key: str | None = None,
        http_client: Any | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.select_fields = select_fields or list(DEFAULT_SELECT_FIELDS)
        self.max_retries = max_retries
        self.regular_delay_seconds = regular_delay_seconds
        self.backoff_seconds = backoff_seconds
        self.timeout_seconds = timeout_seconds
        self.per_page = per_page
        self.api_key = api_key
        self._identity_to_work_id: dict[tuple[str | None, str | None], str] = {}
        self._session: requests.Session | None = None
        if http_client is None:
            self._session = requests.Session()
            self._session.headers.update({"User-Agent": self.user_agent})
        else:
            self._http_client = http_client

    def _get(self, url: str, params: dict[str, Any]) -> requests.Response:
        """GET with bounded retry and backoff on 429/5xx (NFR2).

        Transport errors (`Timeout`, `ConnectionError`) are as transient as
        a 5xx and retried the same way, bounded by `max_retries`.
        """
        attempt = 0
        while True:
            if self.api_key:
                params = {**params, "api_key": self.api_key}
            try:
                if self._session is not None:
                    response = self._session.get(
                        url, params=params, timeout=self.timeout_seconds
                    )
                else:
                    response = self._http_client.get(
                        url, params=params, timeout=self.timeout_seconds
                    )
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt >= self.max_retries:
                    raise
                attempt += 1
                delay = self.backoff_seconds * (2 ** (attempt - 1))
                logger.warning(
                    "OpenAlex request failed (%s); retry %d/%d in %.1fs: %s",
                    type(exc).__name__,
                    attempt,
                    self.max_retries,
                    delay,
                    url,
                )
                time.sleep(delay)
                continue
            if response.status_code == 200:
                time.sleep(self.regular_delay_seconds)
                return response
            retryable = response.status_code == 429 or response.status_code >= 500
            if not retryable or attempt >= self.max_retries:
                response.raise_for_status()
                return response
            attempt += 1
            delay = self.backoff_seconds * (2 ** (attempt - 1))
            logger.warning(
                "OpenAlex request failed with %d; retry %d/%d in %.1fs: %s",
                response.status_code,
                attempt,
                self.max_retries,
                delay,
                url,
            )
            time.sleep(delay)

    def search_works_by_title(
        self, title: str, cursor: str | None = "*"
    ) -> dict[str, Any]:
        """Search works by title text; returns the raw parsed JSON envelope.

        The first call uses the `cursor=*` sentinel (NFR3); callers advance
        paging by passing `meta.next_cursor` back as `cursor`.
        """
        params: dict[str, Any] = {
            "search": title,
            "select": ",".join(self.select_fields),
            "per-page": self.per_page,
            "cursor": cursor,
        }
        response = self._get(OPENALEX_API_BASE_URL, params)
        return response.json()

    def search_works_citing(
        self, work_id: str, cursor: str | None = "*"
    ) -> dict[str, Any]:
        """Return works that cite `work_id` (forward citation-graph expansion).

        Cursor-paged like the title search: the first call uses the `cursor=*`
        sentinel, callers advance via `meta.next_cursor`. Relevance-ranked by
        the API default; no citation-count sort is ever sent (spec 11 FR3).
        """
        bare_id = work_id.rsplit("/", 1)[-1]
        params: dict[str, Any] = {
            "filter": f"cites:{bare_id}",
            "select": ",".join(self.select_fields),
            "per-page": self.per_page,
            "cursor": cursor,
        }
        response = self._get(OPENALEX_API_BASE_URL, params)
        return response.json()

    def get_work_references(self, work_id: str) -> list[str]:
        """Return the OpenAlex ids of works `work_id` cites (backward expansion).

        Reads the record's own `referenced_works` field. An empty or missing
        list is returned as `[]` (normal for preprint-only records), never an
        error.
        """
        record = self.get_work_by_id(work_id)
        references = record.get("referenced_works") or []
        return list(references)

    def get_work_by_id(self, work_id: str) -> dict[str, Any]:
        """Fetch a single work by its OpenAlex id or URL form."""
        bare_id = work_id.rsplit("/", 1)[-1]
        params: dict[str, Any] = {"select": ",".join(self.select_fields)}
        response = self._get(f"{OPENALEX_API_BASE_URL}/{bare_id}", params)
        return response.json()

    @staticmethod
    def _record(work: dict[str, Any]) -> PaperRecord:
        doi = work.get("doi")
        arxiv_id = None
        for location in work.get("locations") or []:
            landing = location.get("landing_page_url") or ""
            if "arxiv.org/abs/" in landing:
                arxiv_id = landing.rsplit("/", 1)[-1].split("v", 1)[0]
                break
        authors = [
            entry.get("author", {}).get("display_name", "")
            for entry in work.get("authorships", [])
        ]
        abstract = _abstract_from_inverted_index(work.get("abstract_inverted_index"))
        return PaperRecord(
            identity=PaperIdentity.from_values(arxiv_id=arxiv_id, doi=doi),
            title=work.get("display_name") or work.get("title") or "",
            authors=[name for name in authors if name],
            publication_date=(
                date.fromisoformat(work["publication_date"])
                if work.get("publication_date")
                else None
            ),
            citation_count=work.get("cited_by_count"),
            raw_influence={
                "fwci": work["fwci"]
                for key in ["fwci"]
                if isinstance(work.get(key), (int, float))
            },
        )

    def _resolve_by_openalex_work(self, work: dict[str, Any]) -> PaperRecord:
        record = self._record(work)
        work_id = work.get("id")
        if work_id:
            key = (record.identity.arxiv_id, record.identity.doi)
            self._identity_to_work_id[key] = work_id
        return record

    def _lookup_identity(self, identity: PaperIdentity) -> PaperRecord | None:
        key = (identity.arxiv_id, identity.doi)
        cached = self._identity_to_work_id.get(key)
        if cached:
            return self._resolve_by_openalex_work(self.get_work_by_id(cached))
        lookup_doi = identity.doi or (
            f"10.48550/arXiv.{identity.arxiv_id}" if identity.arxiv_id else None
        )
        if lookup_doi is None:
            raise ValueError("paper identity must contain arxiv_id or doi")
        try:
            response = self._get(
                OPENALEX_API_BASE_URL,
                {
                    "filter": f"doi:{lookup_doi}",
                    "per-page": 1,
                    "select": ",".join(self.select_fields),
                },
            )
            envelope = response.json()
            results = envelope.get("results") or []
            if not results:
                return None
            work = results[0]
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return None
            raise
        return self._resolve_by_openalex_work(work)

    def resolve_by_arxiv_id(self, arxiv_id: str) -> PaperRecord | None:
        return self._lookup_identity(PaperIdentity.from_values(arxiv_id=arxiv_id))

    def resolve_by_doi(self, doi: str) -> PaperRecord | None:
        return self._lookup_identity(PaperIdentity.from_values(doi=doi))

    def search_papers(self, query: str, continuation: Any = None) -> PaperPage:
        envelope = self.search_works_by_title(query, cursor=continuation or "*")
        meta = envelope.get("meta") or {}
        return PaperPage(
            papers=[
                self._resolve_by_openalex_work(work)
                for work in envelope.get("results") or []
            ],
            continuation=meta.get("next_cursor"),
        )

    def get_citations(
        self, identity: PaperIdentity, continuation: Any = None
    ) -> PaperPage:
        record = self._lookup_identity(identity)
        if record is None:
            return PaperPage()
        work_id = self._identity_to_work_id[
            (record.identity.arxiv_id, record.identity.doi)
        ]
        envelope = self.search_works_citing(work_id, cursor=continuation or "*")
        meta = envelope.get("meta") or {}
        return PaperPage(
            papers=[
                self._resolve_by_openalex_work(work)
                for work in envelope.get("results") or []
            ],
            continuation=meta.get("next_cursor"),
        )

    def get_references(
        self, identity: PaperIdentity, limit: int | None = None
    ) -> list[PaperRecord]:
        record = self._lookup_identity(identity)
        if record is None:
            return []
        work_id = self._identity_to_work_id[
            (record.identity.arxiv_id, record.identity.doi)
        ]
        references = self.get_work_references(work_id)
        records = [
            self._resolve_by_openalex_work(self.get_work_by_id(ref))
            for ref in references
        ]
        return records if limit is None else records[:limit]
