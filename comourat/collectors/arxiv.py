"""arXiv collection logic: RSS feed retrieval and candidate building.

Plain-OOP collection logic extracted from mourat; the mourat pipeline layer
wraps `ArxivPaperCollectorCore` in a `Function` adapter.
"""

from __future__ import annotations

import datetime
import logging
import re
from typing import Literal, TypeAlias

import feedparser
import httpx

from comourat.models import PaperCandidate, PaperCandidateCollection

ArxivSearchMode: TypeAlias = Literal["newest", "most_relevant"]

_ARXIV_ABS_ID_RE = re.compile(r"/abs/([\d.]+)(?:v\d+)?$")
_ANNOUNCE_TYPE_RE = re.compile(r"Announce Type: (\w+)")
logger = logging.getLogger(__name__)


def normalize_author_name(s: str) -> str:
    names = s.split(" ")
    return " ".join([n for n in names if "." not in n])


def extract_arxiv_id(link: str) -> str | None:
    """Base arXiv id from an abs link; version suffix stripped."""
    m = _ARXIV_ABS_ID_RE.search(link)
    return m.group(1) if m else None


def candidate_from_entry(entry, candidate_model=PaperCandidate):
    """One RSS item -> PaperCandidate.

    The feed items carry no per-item date (feedparser's published_parsed is
    the feed-generation date), so publication_date is left None here; the
    affiliation fetcher sets it from the arXiv Atom API's `published`
    field (first submission date). The announce type is read from the
    structured RSS field.
    """
    link = entry.link
    title = entry.title.replace("\n", " ")
    abstract = entry.description.split("\n")[1][10:]
    authors = [
        normalize_author_name(author_data["name"]) for author_data in entry.authors
    ]
    announce_type = getattr(entry, "arxiv_announce_type", None)
    if announce_type is None:
        m = _ANNOUNCE_TYPE_RE.search(entry.description)
        announce_type = m.group(1).lower() if m else None

    candidate = candidate_model(
        title=title,
        authors=authors,
        description=abstract,
        urls_seen=[link],
        arxiv_id=extract_arxiv_id(link),
        publication_date=None,
    )
    # provenance is an application-level concern: only set it when the
    # candidate model carries the field (mourat's extended model does).
    if "provenance" in type(candidate).model_fields:
        candidate.provenance = ["arxiv_rss"]
    # announce_type is an application-level concern: only set it when the
    # candidate model carries the field (mourat's extended model does).
    if announce_type is not None and "announce_type" in type(candidate).model_fields:
        candidate.announce_type = announce_type
    return candidate


class ArxivPaperCollectorCore:
    """Collects paper candidates from the arXiv RSS feed.

    Constructor-argument configuration only; the caller supplies an
    httpx.Client. The new-submission filter is applied by the caller when
    desired (see `filter_new_submissions`).
    """

    def __init__(
        self,
        http_client: httpx.Client,
        api_url: str,
        mode: ArxivSearchMode,
        start_date: str | None = None,  # YYYY-MM-DD
        end_date: str | None = None,  # YYYY-MM-DD
        max_results: int | None = None,
        keywords: str | None = None,
        candidate_model: type[PaperCandidate] = PaperCandidate,
        feed_parser=None,
        today_fn=None,
    ) -> None:
        self.candidate_model = candidate_model
        self.feed_parser = feed_parser if feed_parser is not None else feedparser.parse
        self.http_client = http_client
        self.api_url = api_url
        if start_date is None:
            if mode == "most_relevant":
                raise ValueError(f"Mode '{mode}' implies that start_date is set")
        else:
            self.start_date: datetime.date = datetime.date.fromisoformat(start_date)

        self.end_date: datetime.date
        if end_date is None:
            today_fn = today_fn or datetime.date.today
            self.end_date = today_fn()
        else:
            self.end_date = datetime.date.fromisoformat(end_date)

        self.max_results = max_results
        self.keywords = keywords
        self.mode = mode
        self.mode_to_handler = {
            "newest": self._handle_newest_mode,
            "most_relevant": self._handle_most_relevant_mode,
        }

    def collect(self) -> PaperCandidateCollection:
        return self.mode_to_handler[self.mode]()

    def filter_new_submissions(
        self, output: PaperCandidateCollection
    ) -> tuple[PaperCandidateCollection, int, int]:
        """Keep only 'new' announce-type entries; returns (out, kept, excluded)."""
        collected_count = len(output.papers)
        kept = [paper for paper in output.papers if paper.announce_type == "new"]
        excluded_count = collected_count - len(kept)
        return PaperCandidateCollection(papers=kept), len(kept), excluded_count

    def _handle_newest_mode(self) -> PaperCandidateCollection:
        r: httpx.Response = self.http_client.get(self.api_url)
        feed = self.feed_parser(r.text)
        return PaperCandidateCollection(
            papers=[
                candidate_from_entry(entry, self.candidate_model)
                for entry in feed.entries
            ]
        )

    def _handle_most_relevant_mode(self) -> PaperCandidateCollection:
        search_query = ""
        if self.keywords is not None:
            search_query += "all:"
            search_query += self.keywords.replace(" ", "+")
            search_query += "+AND+"
        date_range = "["
        date_range += self.start_date.strftime("%Y%m%d") + "0000"
        date_range += "+TO+"
        date_range += self.end_date.strftime("%Y%m%d") + "0000"
        date_range += "]"

        search_query += f"submittedDate:{date_range}"

        request_data = {
            "search_query": search_query,
            "max_results": self.max_results,
        }

        r: httpx.Response = self.http_client.get(
            self.api_url,
            params=request_data,
        )

        feed = feedparser.parse(r.text)
        return PaperCandidateCollection(
            papers=[
                candidate_from_entry(entry, self.candidate_model)
                for entry in feed.entries
            ]
        )
