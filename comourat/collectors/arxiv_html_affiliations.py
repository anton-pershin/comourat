"""Author affiliation extraction from arXiv HTML renderings.

arXiv's cheap metadata surfaces (RSS feed, abs page, Atom API) carry no
affiliations. The HTML rendering at https://arxiv.org/html/<id>v1 can carry
inline affiliation spans, numbered affiliation notes, or tabular author blocks.

Deterministic fetching only — no LLM. Papers whose render is missing
(HTTP error) or carries no non-empty affiliations get affiliations=None:
an explicit "unknown" state, not an empty result.
"""

import logging
import re
import time
import xml.etree.ElementTree as ET

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag

from comourat.models import PaperCandidateCollection

logger = logging.getLogger(__name__)

_CORRESPONDENCE_RE = re.compile(r"\s*Correspondence\s+to\s*:.*$", re.IGNORECASE)


def _class_names(node: Tag) -> frozenset[str]:
    classes = node.get("class")
    if classes is None:
        return frozenset()
    if isinstance(classes, str):
        return frozenset(classes.split())
    return frozenset(classes)


def _normalized_text(
    node: Tag,
    *,
    excluded_tags: frozenset[str] = frozenset(),
    excluded_classes: frozenset[str] = frozenset(),
) -> str:
    """Return normalized visible text, excluding selected nested markup."""
    parts: list[str] = []
    for text in node.find_all(string=True):
        parent = text.parent
        excluded = False
        while parent is not None and parent is not node:
            classes = _class_names(parent)
            if parent.name in excluded_tags or classes & excluded_classes:
                excluded = True
                break
            parent = parent.parent
        if not excluded:
            parts.append(str(text))
    return " ".join(" ".join(parts).split())


def _add_affiliations(
    target: dict[str, list[str]], author: str, affiliations: list[str]
) -> None:
    if not author:
        return
    retained = target.setdefault(author, [])
    for affiliation in affiliations:
        if affiliation and affiliation not in retained:
            retained.append(affiliation)


def _merge_affiliations(
    target: dict[str, list[str]], source: dict[str, list[str]]
) -> None:
    for author, affiliations in source.items():
        _add_affiliations(target, author, affiliations)


def _extract_inline_affiliations(soup: BeautifulSoup) -> dict[str, list[str]]:
    """Extract affiliation spans assigned to the most recent inline author."""
    result: dict[str, list[str]] = {}
    current_author: str | None = None
    nodes = soup.select(".ltx_personname, .ltx_contact.ltx_role_affiliation")
    for node in nodes:
        if "ltx_personname" in _class_names(node):
            if node.select_one(".ltx_tabular") is not None:
                current_author = None
                continue
            current_author = _normalized_text(
                node, excluded_classes=frozenset({"ltx_role_affiliation"})
            )
            _add_affiliations(result, current_author, [])
            continue

        if current_author is not None:
            affiliation = _normalized_text(
                node, excluded_classes=frozenset({"ltx_contact_name"})
            )
            _add_affiliations(result, current_author, [affiliation])
    return result


def _extract_tabular_affiliations(soup: BeautifulSoup) -> dict[str, list[str]]:
    """Align bold name rows and affiliation rows in tabular author blocks."""
    result: dict[str, list[str]] = {}
    for person in soup.select(".ltx_personname"):
        for table in person.select(".ltx_tabular"):
            current_authors: list[str] = []
            for row in table.select(".ltx_tr"):
                cells = row.select(".ltx_td")
                if not cells:
                    continue
                if any(cell.select_one(".ltx_font_bold") for cell in cells):
                    current_authors = [
                        _normalized_text(
                            cell,
                            excluded_tags=frozenset({"sup"}),
                            excluded_classes=frozenset({"ltx_font_typewriter"}),
                        )
                        for cell in cells
                    ]
                    for author in current_authors:
                        _add_affiliations(result, author, [])
                    continue
                if any(cell.select_one(".ltx_font_typewriter") for cell in cells):
                    current_authors = []
                    continue
                for author, cell in zip(current_authors, cells, strict=False):
                    affiliation = _normalized_text(
                        cell, excluded_tags=frozenset({"sup"})
                    )
                    _add_affiliations(result, author, [affiliation])
    return result


def _numbered_note_affiliations(note: Tag) -> dict[str, str]:
    """Read numeric references and affiliation text from one footnote."""
    content = note.select_one(".ltx_note_content") or note
    affiliations: dict[str, str] = {}
    reference: str | None = None
    parts: list[str] = []

    def retain_current() -> None:
        if reference is None:
            return
        text = " ".join(" ".join(parts).split())
        text = _CORRESPONDENCE_RE.sub("", text).strip()
        if reference and text:
            affiliations[reference] = text

    for child in content.children:
        if (
            isinstance(child, Tag)
            and child.name == "sup"
            and "ltx_sup" in _class_names(child)
        ):
            retain_current()
            reference = _normalized_text(child)
            if not reference.isdecimal():
                reference = None
            parts = []
            continue
        if reference is None:
            continue
        if isinstance(child, NavigableString):
            parts.append(str(child))
        elif isinstance(child, Tag):
            parts.append(
                _normalized_text(
                    child,
                    excluded_tags=frozenset({"sup"}),
                    excluded_classes=frozenset({"ltx_note_mark", "ltx_note_type"}),
                )
            )
    retain_current()
    return affiliations


def _next_superscript_sibling(node: Tag) -> Tag | None:
    sibling = node.next_sibling
    while isinstance(sibling, NavigableString) and not str(sibling).strip():
        sibling = sibling.next_sibling
    if (
        not isinstance(sibling, Tag)
        or sibling.name != "sup"
        or "ltx_sup" not in _class_names(sibling)
    ):
        return None
    return sibling


def _extract_numbered_affiliations(soup: BeautifulSoup) -> dict[str, list[str]]:
    """Map bold authors to affiliations through numeric note references."""
    result: dict[str, list[str]] = {}
    for note in soup.select(".ltx_role_footnotetext"):
        affiliations = _numbered_note_affiliations(note)
        if not affiliations:
            continue
        name_nodes = note.find_all_previous("span", class_="ltx_font_bold")
        for name_node in reversed(name_nodes):
            if "ltx_text" not in _class_names(name_node):
                continue
            marker = _next_superscript_sibling(name_node)
            if marker is None:
                continue
            marker_text = _normalized_text(marker)
            if re.fullmatch(r"[\d\s,]+", marker_text) is None:
                continue
            references = [
                reference
                for reference in re.findall(r"\d+", marker_text)
                if reference in affiliations
            ]
            if references:
                author = _normalized_text(name_node)
                _add_affiliations(
                    result, author, [affiliations[ref] for ref in references]
                )
    return result


def _extract_affiliations(html: str) -> dict[str, list[str]]:
    """Author -> affiliation pairs from supported arXiv HTML layouts."""
    soup = BeautifulSoup(html, "html.parser")
    result: dict[str, list[str]] = {}
    for extracted in (
        _extract_inline_affiliations(soup),
        _extract_numbered_affiliations(soup),
        _extract_tabular_affiliations(soup),
    ):
        _merge_affiliations(result, extracted)
    return result


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
        except Exception as exc:  # pylint: disable=broad-exception-caught
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
            except Exception as exc:  # pylint: disable=broad-exception-caught
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
            if any(affs.values()):
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
