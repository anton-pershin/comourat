"""arXiv client for metadata lookup, title search, and PDF URL construction.

Plain (non-`Function`) client called from within pipeline components. arXiv
answers Atom XML (not JSON), so response parsing is handled here rather than by
callers. Every operation has a monotonic overall deadline.
"""

# Internal deadline methods are shared with the package fetch components.
# pylint: disable=protected-access

import logging
import math
import re
import time
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import urlsplit

import requests

from comourat.fetch import FetchPolicy, SafeHttpClient, _Deadline, _DeadlineExceeded
from comourat.models import PaperIdentity, PaperRecord, normalize_arxiv_id
from comourat.proxy import validate_proxy_url

logger = logging.getLogger(__name__)

ARXIV_API_QUERY_URL = "https://export.arxiv.org/api/query"
ARXIV_PDF_URL_TEMPLATE = "https://arxiv.org/pdf/{arxiv_id}"

ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}

_CURRENT_ID_RE = re.compile(
    r"^(?P<id>[0-9]{4}\.[0-9]{4,5})(?:v[0-9]+)?$", re.IGNORECASE
)
_LEGACY_ID_RE = re.compile(
    r"^(?P<id>[a-z][a-z0-9.-]*/[0-9]{7})(?:v[0-9]+)?$", re.IGNORECASE
)
_ARXIV_HOSTS = {"arxiv.org", "www.arxiv.org"}


class ArxivMetadataError(RuntimeError):
    """arXiv returned an error entry or unusable paper metadata."""


def _has_error_path(url: str | None) -> bool:
    return bool(url and urlsplit(url.strip()).path == "/api/errors")


def _raise_for_provider_error(entry: ET.Element) -> None:
    urls = [entry.findtext("a:id", namespaces=ATOM_NS)]
    urls.extend(
        link.get("href") for link in entry.findall("a:link", namespaces=ATOM_NS)
    )
    if not any(_has_error_path(url) for url in urls):
        return
    summary = " ".join((entry.findtext("a:summary", namespaces=ATOM_NS) or "").split())
    message = "arXiv API returned a provider error"
    if summary:
        message = f"{message}: {summary}"
    raise ArxivMetadataError(message)


def _validate_timeout(name: str, value: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be a positive finite number")
    return float(value)


def _normalize_bare_identifier(value: str) -> str:
    for pattern in (_CURRENT_ID_RE, _LEGACY_ID_RE):
        match = pattern.fullmatch(value)
        if match:
            normalized = normalize_arxiv_id(match.group("id"))
            if normalized is not None:
                return normalized
    raise ValueError("Invalid arXiv identifier or URL")


def _normalize_reference(reference: str) -> str:
    if not isinstance(reference, str):
        raise ValueError("arXiv reference must be a string")
    value = reference.strip()
    if not value:
        raise ValueError("arXiv reference must not be empty")
    if value.lower().startswith("arxiv:"):
        return _normalize_bare_identifier(value[len("arxiv:") :])
    if "://" not in value:
        return _normalize_bare_identifier(value)

    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid arXiv identifier or URL") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or parsed.hostname not in _ARXIV_HOSTS
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("Invalid arXiv identifier or URL")

    if parsed.path.startswith("/abs/"):
        identifier = parsed.path[len("/abs/") :]
    elif parsed.path.startswith("/pdf/"):
        identifier = parsed.path[len("/pdf/") :]
        if identifier.lower().endswith(".pdf"):
            identifier = identifier[:-4]
    else:
        raise ValueError("Invalid arXiv identifier or URL")
    return _normalize_bare_identifier(identifier)


class ArxivClient:
    """Client for the unauthenticated arXiv API and PDF endpoint.

    `http_client` is injectable for tests. By default, a protected
    `SafeHttpClient` with the configured identifying User-Agent is used.
    `timeout_seconds` limits connection and read operations for each HTTP
    request. `overall_timeout_seconds` enforces one deadline for the complete
    public operation, including DNS, transfers, redirects, retries, delays,
    and response parsing.
    """

    def __init__(
        self,
        user_agent: str,
        timeout_seconds: float = 120.0,
        max_retries: int = 8,
        backoff_seconds: float = 2.0,
        regular_delay_seconds: float = 3.0,
        proxy: str | None = None,
        http_client: Any | None = None,
        overall_timeout_seconds: float = 120.0,
    ) -> None:
        self.user_agent = user_agent
        self.timeout_seconds = _validate_timeout("timeout_seconds", timeout_seconds)
        self.overall_timeout_seconds = _validate_timeout(
            "overall_timeout_seconds", overall_timeout_seconds
        )
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.regular_delay_seconds = regular_delay_seconds
        self.proxy = validate_proxy_url(proxy) if proxy else None
        self._http_client = http_client
        self._safe_client: SafeHttpClient | None = None
        if http_client is None:
            session = requests.Session()
            session.headers.update({"User-Agent": self.user_agent})
            policy = FetchPolicy(
                overall_timeout=self.overall_timeout_seconds,
                connection_timeout=self.timeout_seconds,
                read_timeout=self.timeout_seconds,
            )
            self._safe_client = SafeHttpClient(
                session,
                fetch_policy=policy,
                proxy=self.proxy,
            )
        elif isinstance(http_client, SafeHttpClient):
            self._safe_client = http_client

    def _start_operation(self) -> _Deadline:
        return _Deadline.start(
            self.overall_timeout_seconds,
            time.monotonic,
            read_timeout=self.timeout_seconds,
        )

    def _remaining(self, deadline: _Deadline) -> float:
        try:
            return deadline.remaining()
        except _DeadlineExceeded as exc:
            raise requests.Timeout(
                "arXiv operation exceeded "
                f"its {self.overall_timeout_seconds:g}-second overall timeout"
            ) from exc

    def _sleep(self, delay: float, deadline: _Deadline) -> None:
        if delay <= 0:
            return
        time.sleep(min(delay, self._remaining(deadline)))
        self._remaining(deadline)

    def _get(self, url: str, _deadline: _Deadline, **kwargs: Any) -> requests.Response:
        """GET with bounded retry, backoff, and one overall operation deadline."""
        deadline = _deadline
        attempt = 0
        while True:
            request_timeout = min(self.timeout_seconds, self._remaining(deadline))
            try:
                if self._safe_client is not None:
                    response = self._safe_client._call(
                        self._safe_client.session.get,
                        url,
                        _deadline=deadline,
                        **kwargs,
                    )
                else:
                    assert self._http_client is not None
                    response = self._http_client.get(
                        url, timeout=request_timeout, **kwargs
                    )
            except (requests.Timeout, requests.ConnectionError) as exc:
                self._remaining(deadline)
                if attempt >= self.max_retries:
                    raise
                attempt += 1
                delay = self.backoff_seconds * (2 ** (attempt - 1))
                logger.warning(
                    "arXiv request failed (%s); retry %d/%d in %.1fs: %s",
                    type(exc).__name__,
                    attempt,
                    self.max_retries,
                    delay,
                    url,
                )
                self._sleep(delay, deadline)
                continue

            self._remaining(deadline)
            retryable = response.status_code == 429 or response.status_code >= 500
            if not retryable or attempt >= self.max_retries:
                self._sleep(self.regular_delay_seconds, deadline)
                response.raise_for_status()
                return response
            attempt += 1
            delay = self.backoff_seconds * (2 ** (attempt - 1))
            logger.warning(
                "arXiv request failed with %d; retry %d/%d in %.1fs: %s",
                response.status_code,
                attempt,
                self.max_retries,
                delay,
                url,
            )
            self._sleep(delay, deadline)

    def _parse_xml(self, text: str, deadline: _Deadline) -> ET.Element:
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            self._remaining(deadline)
            raise
        self._remaining(deadline)
        return root

    def get_metadata(self, reference: str) -> PaperRecord | None:
        """Return arXiv identity, title, and abstract for an identifier or URL.

        Abstract and PDF URLs are parsed locally. Only the normalized identifier
        is sent to the Atom API; the paper body is never requested. An empty
        feed returns `None`. Invalid references raise `ValueError`. Provider
        error entries and unusable Atom responses raise `ArxivMetadataError`.
        The exception includes the provider summary for an error entry.
        """
        arxiv_id = _normalize_reference(reference)
        deadline = self._start_operation()
        try:
            response = self._get(
                ARXIV_API_QUERY_URL,
                params={"id_list": arxiv_id},
                _deadline=deadline,
            )
            try:
                root = self._parse_xml(response.text, deadline)
            except ET.ParseError as exc:
                raise ArxivMetadataError("arXiv returned malformed Atom XML") from exc

            entry = root.find("a:entry", namespaces=ATOM_NS)
            if entry is None:
                self._remaining(deadline)
                return None
            _raise_for_provider_error(entry)
            title = " ".join(
                (entry.findtext("a:title", namespaces=ATOM_NS) or "").split()
            )
            abstract = " ".join(
                (entry.findtext("a:summary", namespaces=ATOM_NS) or "").split()
            )
            self._remaining(deadline)
            if not title or not abstract:
                raise ArxivMetadataError(
                    "arXiv metadata must contain a title and abstract"
                )
            record = PaperRecord(
                identity=PaperIdentity.from_values(arxiv_id=arxiv_id),
                title=title,
                abstract=abstract,
            )
            self._remaining(deadline)
            return record
        finally:
            deadline.close()

    def get_title_by_id(self, arxiv_id: str) -> str | None:
        """Return the title of an arXiv id, or None when the feed is empty.

        An unknown arXiv id returns a normal `<feed>` with zero `<entry>`s —
        no 404 — so an empty feed is "not found", not an error. Provider error
        entries raise `ArxivMetadataError` with the provider summary.
        """
        deadline = self._start_operation()
        try:
            response = self._get(
                ARXIV_API_QUERY_URL, params={"id_list": arxiv_id}, _deadline=deadline
            )
            root = self._parse_xml(response.text, deadline)
            entry = root.find("a:entry", namespaces=ATOM_NS)
            if entry is None:
                self._remaining(deadline)
                return None
            _raise_for_provider_error(entry)
            title = entry.findtext("a:title", namespaces=ATOM_NS)
            self._remaining(deadline)
            if title is None:
                return None
            return " ".join(title.split())
        finally:
            deadline.close()

    def search_by_title(self, title: str) -> tuple[str, str] | None:
        """Find an arXiv preprint by title; returns (arxiv_id, arxiv_title).

        The top Atom entry of a `ti:"<title>"` search, with the id stripped of
        its version suffix and url prefix, and the title whitespace-collapsed.
        An empty feed (no preprint with that title) returns None — not an
        error, same convention as `get_title_by_id`. Callers must cross-check
        the returned title against their own claimed title: arXiv relevance
        matching can return a different paper.
        """
        deadline = self._start_operation()
        try:
            response = self._get(
                ARXIV_API_QUERY_URL,
                params={"search_query": f'ti:"{title}"', "max_results": 1},
                _deadline=deadline,
            )
            root = self._parse_xml(response.text, deadline)
            entry_id = root.findtext("a:entry/a:id", namespaces=ATOM_NS)
            entry_title = root.findtext("a:entry/a:title", namespaces=ATOM_NS)
            self._remaining(deadline)
            if entry_id is None or entry_title is None:
                return None
            bare_id = entry_id.rsplit("/", 1)[-1]
            arxiv_id = bare_id.split("v")[0] if "v" in bare_id[4:] else bare_id
            return arxiv_id, " ".join(entry_title.split())
        finally:
            deadline.close()
