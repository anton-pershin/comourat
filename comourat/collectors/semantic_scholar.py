import datetime
import json
import logging
import time
from typing import Any, Literal, TypeAlias

import httpx

from comourat.models import PaperInfo, PaperInfoCollection

logger = logging.getLogger(__name__)

SemanticScholarSearchMode: TypeAlias = Literal[
    "newest", "most_relevant", "most_influential"
]


class SemanticScholarPaperCollectorCore:
    def __init__(
        self,
        http_client: httpx.Client,
        api_url: str,
        mode: SemanticScholarSearchMode,
        start_date: str | None,  # YYYY-MM-DD
        end_date: str | None,  # YYYY-MM-DD
        max_results: int,
        strict_keyword_query: str,
    ) -> None:
        self.http_client = http_client
        self.api_url = api_url

        self.start_date: datetime.date | None = None
        if start_date is not None:
            self.start_date = datetime.date.fromisoformat(start_date)

        self.end_date: datetime.date | None = None
        if end_date is not None:
            self.end_date = datetime.date.fromisoformat(end_date)

        self.max_results = max_results
        self.strict_keyword_query = strict_keyword_query
        self.mode = mode
        self.mode_to_handler = {
            "newest": self._handle_newest_mode,
            "most_relevant": self._handle_most_relevant_mode,
            "most_influential": self._handle_most_influential_mode,
        }

    def collect(self) -> PaperInfoCollection:
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

    def _get_papers_via_bulk_search(
        self,
        request_data: dict[str, Any],
    ) -> PaperInfoCollection:
        output = PaperInfoCollection(papers=[])

        # Add standard fields to the request params
        request_data["fields"] = (
            "title,url,abstract,citationCount,publicationDate,authors"
        )

        # Add publication date range to the request params
        publication_date_range = (
            self.start_date.isoformat() if self.start_date is not None else ""
        )
        publication_date_range += ":"
        publication_date_range += (
            self.end_date.isoformat() if self.end_date is not None else ""
        )
        if publication_date_range != ":":
            request_data["publicationDateOrYear"] = publication_date_range

        n_papers_processed = 0
        token: str | None = None

        while n_papers_processed < self.max_results:
            if token is not None:
                request_data["token"] = token

            papers, r_data = self._run_one_paper_request_via_api(
                full_api_url=self.api_url + "/paper/search/bulk",
                request_data=request_data,
                n_papers_processed=n_papers_processed,
            )
            output.papers.extend(papers)
            n_papers_processed = len(output.papers)

            if (
                r_data["token"] is None
            ):  # token is present only if we can fetch more results
                break

            token = r_data["token"]

        return output

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
                    publication_date=publication_date,
                )
            )
            n_papers_processed += 1
            if n_papers_processed >= self.max_results:
                break

        return papers, r_data

    @staticmethod
    def _mandatory_fields_absent(ss_paper_data: dict[str, Any]) -> bool:
        mandatory_fields = ["title", "url", "abstract"]
        return any(ss_paper_data[f] is None for f in mandatory_fields)
