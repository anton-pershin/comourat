"""Author affiliation extraction from arXiv HTML renderings.

arXiv's cheap metadata surfaces (RSS feed, abs page, Atom API) carry no
affiliations. The HTML rendering at https://arxiv.org/html/<id>v1 does:
each author block may carry ltx_role_affiliation spans. Measured on recent
cs.LG submissions: 9/9 rendered, 8/9 with parseable affiliation nodes.

Deterministic fetching only — no LLM. Papers whose render is missing
(HTTP error) or carries zero affiliation nodes get affiliations=None:
an explicit "unknown" state, not an empty result.
"""

import logging
import re
import time
from typing import Any

import httpx

from comourat.models import PaperCandidate, PaperCandidateCollection

logger = logging.getLogger(__name__)

# Author blocks in the HTML render look like:
# <span class="ltx_contact ltx_role_affiliation"><span class="ltx_contact_name">
# Affiliation: </span>University of Somewhere</span>
_AFFILIATION_SPAN_RE = re.compile(
    r'<span class="ltx_contact ltx_role_affiliation"[^>]*>.*?'
    r'<span class="ltx_contact_name">\s*Affiliation:\s*</span>'
    r"(.*?)</span>",
    re.S,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _extract_affiliations(html: str) -> dict[str, list[str]]:
    """Author -> affiliation pairs from an arXiv HTML rendering.

    The render interleaves author names and affiliation spans in document
    order: each affiliation span belongs to the most recently seen author
    name. Multiple affiliation spans per author are common (dual
    appointments) and accumulate.
    """
    # Author names appear as <span class="ltx_personname">Full Name</span>
    author_spans = re.findall(
        r'<span class="ltx_personname"[^>]*>(.*?)</span>', html, re.S
    )

    # Walk the html in order, tracking the current author.
    author_to_affs: dict[str, list[str]] = {}
    pattern = re.compile(
        r'<span class="ltx_personname"[^>]*>(.*?)</span>'
        r'|<span class="ltx_contact ltx_role_affiliation"[^>]*>.*?'
        r'<span class="ltx_contact_name">\s*Affiliation:\s*</span>'
        r"(.*?)</span>",
        re.S,
    )
    current_author: str | None = None
    for m in pattern.finditer(html):
        person, affil = m.group(1), m.group(2)
        if person is not None:
            current_author = _TAG_RE.sub(" ", person).strip()
            author_to_affs.setdefault(current_author, [])
        elif affil is not None and current_author is not None:
            aff = _TAG_RE.sub(" ", affil).strip()
            if aff and aff not in author_to_affs[current_author]:
                author_to_affs[current_author].append(aff)
    return author_to_affs


class ArxivHtmlAffiliationFetcherCore:
    """Attaches author affiliations and true publication dates.

    Pass-through by count: every candidate is returned. Two in-flight
    fields change: `affiliations` (None when unknown) and
    `publication_date`, set from the arXiv Atom API's `published` field —
    the FIRST submission date, not the last revision. On Atom API failure
    the date stays None rather than a wrong value. Fetching is paced:
    `request_delay_seconds` between requests.
    """

    _ATOM_API_URL = "https://export.arxiv.org/api/query"

    def __init__(
        self,
        http_client: httpx.Client,
        request_delay_seconds: float = 1.0,
    ) -> None:
        self.http_client = http_client
        self.request_delay_seconds = request_delay_seconds

    def _fetch_first_publication_date(self, arxiv_id: str) -> str | None:
        """First-publication date (YYYY-MM-DD) from the Atom API; None on failure.

        `published` is the original submission date; `updated` would be the
        last revision, which is NOT what the pipeline stores.
        """
        import xml.etree.ElementTree as ET

        ns = {"a": "http://www.w3.org/2005/Atom"}
        try:
            r: httpx.Response = self.http_client.get(
                self._ATOM_API_URL, params={"id_list": arxiv_id}
            )
            r.raise_for_status()
            root = ET.fromstring(r.text)
            entry = root.find("a:entry", ns)
            if entry is None:
                logger.warning("Atom API returned no entry for %s", arxiv_id)
                return None
            published = entry.find("a:published", ns)
            if published is None or not published.text:
                return None
            return published.text[:10]  # YYYY-MM-DD
        except Exception as exc:
            logger.warning("publication-date fetch failed for '%s': %s", arxiv_id, exc)
            return None

    def fetch_affiliations(
        self, data: PaperCandidateCollection
    ) -> tuple[PaperCandidateCollection, list[str]]:
        t_start = time.monotonic()
        fetched = 0
        with_affiliations = 0
        dated = 0
        unknown: list[str] = []
        undated: list[str] = []
        total = len(data.papers)

        for i, paper in enumerate(data.papers, 1):
            if paper.arxiv_id is None:
                unknown.append(paper.title)
                continue

            # True first-publication date from the Atom API (one request).
            date = self._fetch_first_publication_date(paper.arxiv_id)
            if date is not None:
                paper.publication_date = date
                dated += 1
            else:
                undated.append(paper.title)

            url = f"https://arxiv.org/html/{paper.arxiv_id}v1"
            try:
                r: httpx.Response = self.http_client.get(url)
                r.raise_for_status()
                html = r.text
            except Exception as exc:
                logger.warning(
                    "affiliation fetch failed for '%s' (%s): %s",
                    paper.title,
                    url,
                    exc,
                )
                unknown.append(paper.title)
                paper.affiliations = None
                if i < total:
                    time.sleep(self.request_delay_seconds)
                continue

            fetched += 1
            affs = _extract_affiliations(html)
            if affs:
                paper.affiliations = affs
                with_affiliations += 1
            else:
                paper.affiliations = None
                unknown.append(paper.title)

            if i < total:
                time.sleep(self.request_delay_seconds)

        update_count = sum(1 for p in data.papers if p.announce_type == "replace")
        lines = [
            (
                f"Papers: {total}, fetched: {fetched}, with affiliations: "
                f"{with_affiliations}, affiliations unknown: {len(unknown)}, "
                f"dated from Atom API: {dated}, undated: {len(undated)}, "
                f"updates (announce type 'replace'): {update_count}"
            ),
            "Affiliations unknown for:",
        ]
        lines.extend(f"- {t}" for t in unknown)
        if undated:
            lines.append("Publication date unavailable for:")
            lines.extend(f"- {t}" for t in undated)
        lines.append(f"(stage took {time.monotonic() - t_start:.1f}s)")
        return data, lines
