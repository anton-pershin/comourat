"""HN Algolia API client."""

from __future__ import annotations

import datetime
from typing import Any

import requests

DEFAULT_BASE_URL = "https://hn.algolia.com/api/v1/search_by_date"
DEFAULT_USER_AGENT = "mourat-hn-collector/0.1"
DEFAULT_PAGE_SIZE = 1000


class HNAlgoliaClient:
    """Wraps the HN Algolia `search_by_date` endpoint.

    All failures (HTTP errors, non-JSON bodies, malformed payloads) raise:
    there is no silent fallback to partial or empty results.
    """

    def __init__(
        self,
        http_client: Any | None = None,
        base_url: str = DEFAULT_BASE_URL,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.http_client = http_client if http_client is not None else requests
        self.base_url = base_url
        self.user_agent = user_agent

    def _build_params(
        self,
        lo: int,
        hi: int,
        min_points: int,
        tags: list[str],
        page: int,
        page_size: int,
    ) -> dict:
        numeric_filters = f"created_at_i>{lo},created_at_i<{hi},points>={min_points}"
        return {
            "query": "",
            "tags": ",".join(tags),
            "numericFilters": numeric_filters,
            "hitsPerPage": page_size,
            "page": page,
        }

    @staticmethod
    def parse_hit(hit: dict) -> dict:
        created = datetime.datetime.fromtimestamp(hit["created_at_i"], tz=datetime.UTC)
        return {
            "hn_id": str(hit["objectID"]),
            "title": hit.get("title") or "",
            "author": hit.get("author") or "",
            "date": created.isoformat(),
            "url": hit.get("url") or "",
            "text": hit.get("story_text") or "",
            "points": int(hit.get("points") or 0),
            "num_comments": int(hit.get("num_comments") or 0),
        }

    def search_page(
        self,
        lo: int,
        hi: int,
        min_points: int = 0,
        tags: list[str] | None = None,
        page: int = 0,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> dict:
        """Fetch one page of date-ordered stories; returns the raw payload."""
        params = self._build_params(
            lo, hi, min_points, tags or ["story"], page, page_size
        )
        response = self.http_client.get(
            self.base_url,
            params=params,
            headers={"User-Agent": self.user_agent},
            timeout=30,
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"HN Algolia returned a non-JSON response: {exc}"
            ) from exc
        if not isinstance(payload, dict) or "hits" not in payload:
            raise RuntimeError("HN Algolia response is missing the 'hits' key")
        return payload

    def search_by_date(
        self,
        lo: int,
        hi: int,
        min_points: int = 0,
        tags: list[str] | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> list[dict]:
        """Fetch the FIRST page of date-ordered stories as parsed post dicts.

        The collector uses `search_page` for full pagination; this method is
        the single-page convenience wrapper returning parsed hits.
        """
        payload = self.search_page(
            lo, hi, min_points=min_points, tags=tags, page=0, page_size=page_size
        )
        return [self.parse_hit(hit) for hit in payload["hits"]]
