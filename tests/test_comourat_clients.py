"""comourat client tests: identity retrieval, search and citations (AC1/AC2).

Mirrors the mocked-HTTP style of tests/test_clients.py but imports the classes
from comourat, proving the library works standalone (spec 22 AC1/AC2).
"""

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

import comourat.clients.arxiv as arxiv_module
from comourat.clients.arxiv import ArxivClient
from comourat.clients.openalex import DEFAULT_SELECT_FIELDS, OpenAlexClient
from comourat.models import PaperIdentity, normalize_arxiv_id, normalize_doi


def _make_client(http_client: MagicMock, **kwargs) -> OpenAlexClient:
    defaults: dict = {"user_agent": "comourat-test/0.1 (mailto:test@example.com)"}
    defaults.update(kwargs)
    return OpenAlexClient(**defaults, http_client=http_client)


def _make_arxiv_client(http_client: MagicMock, **kwargs) -> ArxivClient:
    defaults: dict = {
        "user_agent": "comourat-test/0.1 (mailto:test@example.com)",
        "regular_delay_seconds": 0,
    }
    defaults.update(kwargs)
    return ArxivClient(**defaults, http_client=http_client)


def _json_response(payload: dict, status_code: int = 200) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    if isinstance(payload, dict):
        response.text = json.dumps(payload)
    if status_code >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(f"{status_code}")
    return response


def _arxiv_response(text: str, status_code: int = 200) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.text = text
    response.headers = {}
    response.iter_content.return_value = iter([b""])
    if status_code >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(
            f"{status_code}", response=response
        )
    return response


def _arxiv_metadata_feed(
    title: str = "  A   multi\n    line   title ",
    abstract: str = "  An   abstract\n    with   whitespace ",
) -> str:
    return (
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
        f"<title>{title}</title><summary>{abstract}</summary>"
        "</entry></feed>"
    )


def _arxiv_error_feed(marker: str) -> str:
    error_url = "http://arxiv.org/api/errors#incorrect_id_format_for_1234.12345"
    entry_id = error_url if marker == "id" else "http://arxiv.org/abs/1234.12345"
    link = (
        ""
        if marker == "id"
        else f'<link href="{error_url}" rel="alternate" type="text/html"/>'
    )
    return (
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
        f"<id>{entry_id}</id><title>Error</title>"
        "<summary>  incorrect id format\n for 1234.12345 </summary>"
        f"{link}</entry></feed>"
    )


class _ControlledClock:
    def __init__(self):
        self.now = 0.0
        self.delays = []

    def monotonic(self):
        return self.now

    def sleep(self, delay):
        self.delays.append(delay)
        self.now += delay


class TestOpenAlexIdentityRetrieval:
    def test_resolve_by_arxiv_id_returns_paper_record(self):
        http_client = MagicMock()
        http_client.get.return_value = _json_response(
            {
                "meta": {},
                "results": [
                    {
                        "id": "https://openalex.org/W1",
                        "doi": "https://doi.org/10.48550/arXiv.1706.03762",
                        "display_name": "A paper",
                        "publication_date": "2017-06-02",
                        "locations": [],
                        "authorships": [],
                        "fwci": 2.5,
                        "cited_by_count": 12,
                    }
                ],
            }
        )
        result = _make_client(http_client).resolve_by_arxiv_id("1706.03762")
        assert result.identity.arxiv_id == "1706.03762"
        assert result.identity.doi is None
        assert result.title == "A paper"
        assert result.raw_influence == {"fwci": 2.5}

    def test_resolve_by_doi_returns_paper_record(self):
        http_client = MagicMock()
        http_client.get.return_value = _json_response(
            {
                "meta": {},
                "results": [
                    {
                        "id": "https://openalex.org/W2",
                        "doi": "https://doi.org/10.1234/foo",
                        "display_name": "Another paper",
                        "locations": [],
                        "authorships": [],
                    }
                ],
            }
        )
        result = _make_client(http_client).resolve_by_doi("10.1234/foo")
        assert result.identity.doi == "10.1234/foo"
        assert result.title == "Another paper"

    def test_resolve_unknown_id_returns_none(self):
        http_client = MagicMock()
        http_client.get.return_value = _json_response({"meta": {}, "results": []})
        assert _make_client(http_client).resolve_by_doi("10.9999/nope") is None

    def test_identity_normalization_exposed_from_comourat(self):
        assert normalize_arxiv_id("arXiv:1706.03762v2") == "1706.03762"
        assert normalize_doi("https://doi.org/10.1234/ABC") == "10.1234/ABC"


class TestOpenAlexSearch:
    def test_search_papers_uses_cursor_sentinel_and_select_fields(self):
        http_client = MagicMock()
        http_client.get.return_value = _json_response({"meta": {}, "results": []})
        client = _make_client(http_client)
        client.search_papers("attention")
        _, kwargs = http_client.get.call_args
        assert kwargs["params"]["cursor"] == "*"
        requested = set(kwargs["params"]["select"].split(","))
        assert requested == set(DEFAULT_SELECT_FIELDS)


class TestOpenAlexCitations:
    def test_get_citations_resolves_then_cites_filter(self):
        http_client = MagicMock()
        http_client.get.side_effect = [
            _json_response(
                {
                    "meta": {},
                    "results": [
                        {
                            "id": "https://openalex.org/W1",
                            "doi": "https://doi.org/10.48550/arXiv.1706.03762",
                            "display_name": "A paper",
                            "locations": [],
                            "authorships": [],
                        }
                    ],
                }
            ),
            _json_response({"results": [], "meta": {}}),
        ]
        client = _make_client(http_client)
        page = client.get_citations(PaperIdentity(arxiv_id="1706.03762"))
        assert page.papers == []
        assert http_client.get.call_args.kwargs["params"]["filter"] == "cites:W1"


class TestArxivClient:
    def test_get_title_by_id_collapses_whitespace(self):
        feed_xml = (
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
            "<title>  A   multi\n    line   title </title>"
            "</entry></feed>"
        )
        http_client = MagicMock()
        http_client.get.return_value = _arxiv_response(feed_xml)
        assert _make_arxiv_client(http_client).get_title_by_id("1706.03762") == (
            "A multi line title"
        )

    def test_empty_feed_returns_none(self):
        http_client = MagicMock()
        http_client.get.return_value = _arxiv_response("<feed></feed>")
        assert _make_arxiv_client(http_client).get_title_by_id("1706.03762") is None

    @pytest.mark.parametrize("method_name", ["get_metadata", "get_title_by_id"])
    @pytest.mark.parametrize("marker", ["id", "link"])
    def test_provider_error_entry_is_explicit(self, method_name, marker):
        http_client = MagicMock()
        http_client.get.return_value = _arxiv_response(_arxiv_error_feed(marker))
        client = _make_arxiv_client(http_client)

        with pytest.raises(arxiv_module.ArxivMetadataError) as exc_info:
            getattr(client, method_name)("1234.12345")

        assert "incorrect id format for 1234.12345" in str(exc_info.value)

    @pytest.mark.parametrize("method_name", ["get_metadata", "get_title_by_id"])
    def test_paper_titled_error_is_not_a_provider_error(self, method_name):
        feed_xml = (
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
            "<id>http://arxiv.org/abs/1706.03762v2</id>"
            '<link href="http://arxiv.org/abs/1706.03762v2" rel="alternate"/>'
            "<title>Error</title><summary>A real paper abstract</summary>"
            "</entry></feed>"
        )
        http_client = MagicMock()
        http_client.get.return_value = _arxiv_response(feed_xml)
        client = _make_arxiv_client(http_client)

        result = getattr(client, method_name)("1706.03762")

        if method_name == "get_metadata":
            assert result is not None
            assert result.title == "Error"
            assert result.abstract == "A real paper abstract"
        else:
            assert result == "Error"


class TestArxivMetadata:
    def test_current_identifier_returns_complete_paper_record(self):
        http_client = MagicMock()
        http_client.get.return_value = _arxiv_response(_arxiv_metadata_feed())

        result = _make_arxiv_client(http_client).get_metadata("1706.03762")

        assert result is not None
        assert result.identity.arxiv_id == "1706.03762"
        assert result.identity.doi is None
        assert result.title == "A multi line title"
        assert result.abstract == "An abstract with whitespace"
        assert result.authors == []
        assert result.publication_date is None
        assert result.citation_count is None
        assert result.raw_influence == {}
        http_client.get.assert_called_once()
        assert http_client.get.call_args.args[0] == arxiv_module.ARXIV_API_QUERY_URL
        assert http_client.get.call_args.kwargs["params"] == {"id_list": "1706.03762"}

    @pytest.mark.parametrize(
        ("reference", "expected_id"),
        [
            ("arXiv:1706.03762v2", "1706.03762"),
            (" 1706.03762 ", "1706.03762"),
            ("hep-th/9901001v1", "hep-th/9901001"),
        ],
    )
    def test_identifier_variants_are_normalized(self, reference, expected_id):
        http_client = MagicMock()
        http_client.get.return_value = _arxiv_response(_arxiv_metadata_feed())

        result = _make_arxiv_client(http_client).get_metadata(reference)

        assert result is not None
        assert result.identity.arxiv_id == expected_id
        assert http_client.get.call_args.kwargs["params"]["id_list"] == expected_id

    @pytest.mark.parametrize(
        ("reference", "expected_id"),
        [
            ("http://arxiv.org/abs/1706.03762", "1706.03762"),
            ("https://www.arxiv.org/abs/1706.03762v2", "1706.03762"),
            (
                " https://arxiv.org/abs/1706.03762?context=cs#paper ",
                "1706.03762",
            ),
            ("https://arxiv.org/pdf/1706.03762", "1706.03762"),
            ("https://arxiv.org/pdf/1706.03762.pdf", "1706.03762"),
            ("https://www.arxiv.org/pdf/1706.03762v2.pdf", "1706.03762"),
            (
                "https://arxiv.org/pdf/hep-th/9901001v1.pdf?download=1#view",
                "hep-th/9901001",
            ),
            ("https://www.arxiv.org/abs/hep-th/9901001", "hep-th/9901001"),
        ],
    )
    def test_urls_use_only_the_atom_api(self, reference, expected_id):
        http_client = MagicMock()
        http_client.get.return_value = _arxiv_response(_arxiv_metadata_feed())

        result = _make_arxiv_client(http_client).get_metadata(reference)

        assert result is not None
        assert result.identity.arxiv_id == expected_id
        http_client.get.assert_called_once()
        assert http_client.get.call_args.args[0] == arxiv_module.ARXIV_API_QUERY_URL
        assert http_client.get.call_args.kwargs["params"]["id_list"] == expected_id

    @pytest.mark.parametrize(
        "reference",
        [
            "",
            "   ",
            "not-an-id",
            "1706.123",
            "1706.03762v",
            "arxiv:",
            "ftp://arxiv.org/abs/1706.03762",
            "https://example.org/abs/1706.03762",
            "https://arxiv.org.evil.example/abs/1706.03762",
            "https://arxiv.org@evil.example/abs/1706.03762",
            "https://arxiv.org/search/?query=attention",
            "https://arxiv.org/abs/",
            "https://arxiv.org/abs/1706.03762/extra",
            "https://arxiv.org/pdf/1706.03762.pdfx",
            "https://arxiv.org/pdf/1706.03762v2.pdf.extra",
        ],
    )
    def test_invalid_references_fail_without_http(self, reference):
        http_client = MagicMock()

        with pytest.raises(ValueError):
            _make_arxiv_client(http_client).get_metadata(reference)

        http_client.get.assert_not_called()

    def test_unknown_paper_returns_none(self):
        http_client = MagicMock()
        http_client.get.return_value = _arxiv_response(
            '<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
        )

        assert _make_arxiv_client(http_client).get_metadata("1706.03762") is None

    @pytest.mark.parametrize(
        "entry",
        [
            "<summary>An abstract</summary>",
            "<title> </title><summary>An abstract</summary>",
            "<title>A title</title>",
            "<title>A title</title><summary> \n </summary>",
        ],
    )
    def test_incomplete_metadata_is_explicit(self, entry):
        http_client = MagicMock()
        http_client.get.return_value = _arxiv_response(
            f'<feed xmlns="http://www.w3.org/2005/Atom"><entry>{entry}</entry></feed>'
        )

        with pytest.raises(arxiv_module.ArxivMetadataError):
            _make_arxiv_client(http_client).get_metadata("1706.03762")

    def test_malformed_xml_is_explicit(self):
        http_client = MagicMock()
        http_client.get.return_value = _arxiv_response("<feed><entry></feed>")

        with pytest.raises(arxiv_module.ArxivMetadataError) as exc_info:
            _make_arxiv_client(http_client).get_metadata("1706.03762")

        assert isinstance(exc_info.value.__cause__, ET.ParseError)

    def test_non_retryable_http_failure_is_explicit(self):
        http_client = MagicMock()
        http_client.get.return_value = _arxiv_response("failure", status_code=400)

        with pytest.raises(requests.HTTPError):
            _make_arxiv_client(http_client, max_retries=0).get_metadata("1706.03762")

        assert http_client.get.call_count == 1

    @pytest.mark.parametrize("status_code", [429, 500])
    def test_exhausted_http_retries_are_explicit(self, status_code):
        http_client = MagicMock()
        http_client.get.return_value = _arxiv_response(
            "failure", status_code=status_code
        )

        with pytest.raises(requests.HTTPError):
            _make_arxiv_client(
                http_client, max_retries=2, backoff_seconds=0
            ).get_metadata("1706.03762")

        assert http_client.get.call_count == 3

    @pytest.mark.parametrize(
        "exception_type", [requests.ConnectionError, requests.Timeout]
    )
    def test_exhausted_transport_retries_are_explicit(self, exception_type):
        http_client = MagicMock()
        http_client.get.side_effect = exception_type("provider failure")

        with pytest.raises(exception_type):
            _make_arxiv_client(
                http_client, max_retries=2, backoff_seconds=0
            ).get_metadata("1706.03762")

        assert http_client.get.call_count == 3

    def test_timeout_defaults(self):
        client = _make_arxiv_client(MagicMock())

        assert client.timeout_seconds == 120.0
        assert client.overall_timeout_seconds == 120.0

    @pytest.mark.parametrize(
        "value", [0, -1, float("nan"), float("inf"), True, "1", None]
    )
    @pytest.mark.parametrize("setting", ["timeout_seconds", "overall_timeout_seconds"])
    def test_timeout_configuration_is_validated(self, setting, value):
        http_client = MagicMock()

        with pytest.raises(ValueError):
            _make_arxiv_client(http_client, **{setting: value})

        http_client.get.assert_not_called()

    def test_each_request_receives_only_the_remaining_budget(self, monkeypatch):
        clock = _ControlledClock()
        monkeypatch.setattr(arxiv_module.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(arxiv_module.time, "sleep", clock.sleep)
        http_client = MagicMock()
        request_timeouts = []

        def get(*args, **kwargs):
            request_timeouts.append(kwargs["timeout"])
            if len(request_timeouts) == 1:
                clock.now += 5
                return _arxiv_response("failure", status_code=500)
            return _arxiv_response(_arxiv_metadata_feed())

        http_client.get.side_effect = get
        client = _make_arxiv_client(
            http_client,
            timeout_seconds=120,
            overall_timeout_seconds=20,
            max_retries=1,
            backoff_seconds=0,
        )

        result = client.get_metadata("1706.03762")

        assert result is not None
        assert request_timeouts == [20, 15]

    def test_overall_timeout_limits_backoff_and_stops_retries(self, monkeypatch):
        clock = _ControlledClock()
        monkeypatch.setattr(arxiv_module.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(arxiv_module.time, "sleep", clock.sleep)
        http_client = MagicMock()

        def get(*args, **kwargs):
            clock.now += 4
            return _arxiv_response("failure", status_code=500)

        http_client.get.side_effect = get
        client = _make_arxiv_client(
            http_client,
            timeout_seconds=20,
            overall_timeout_seconds=10,
            max_retries=1,
            backoff_seconds=20,
        )

        with pytest.raises(requests.Timeout):
            client.get_metadata("1706.03762")

        assert http_client.get.call_count == 1
        assert clock.delays == [6]

    def test_overall_timeout_limits_politeness_delay(self, monkeypatch):
        clock = _ControlledClock()
        monkeypatch.setattr(arxiv_module.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(arxiv_module.time, "sleep", clock.sleep)
        http_client = MagicMock()

        def get(*args, **kwargs):
            clock.now += 2
            return _arxiv_response(_arxiv_metadata_feed())

        http_client.get.side_effect = get
        client = _make_arxiv_client(
            http_client,
            timeout_seconds=20,
            overall_timeout_seconds=5,
            max_retries=0,
            regular_delay_seconds=10,
        )

        with pytest.raises(requests.Timeout):
            client.get_metadata("1706.03762")

        assert http_client.get.call_count == 1
        assert clock.delays == [3]

    def test_overall_timeout_includes_response_parsing(self, monkeypatch):
        clock = _ControlledClock()
        monkeypatch.setattr(arxiv_module.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(arxiv_module.time, "sleep", clock.sleep)
        http_client = MagicMock()
        http_client.get.return_value = _arxiv_response(_arxiv_metadata_feed())
        original_fromstring = arxiv_module.ET.fromstring

        def parse_and_advance(*args, **kwargs):
            clock.now += 6
            return original_fromstring(*args, **kwargs)

        monkeypatch.setattr(arxiv_module.ET, "fromstring", parse_and_advance)
        client = _make_arxiv_client(
            http_client, timeout_seconds=20, overall_timeout_seconds=5
        )

        with pytest.raises(requests.Timeout):
            client.get_metadata("1706.03762")

    def test_title_search_remains_compatible(self):
        feed_xml = (
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
            "<id>http://arxiv.org/abs/1706.03762v2</id>"
            "<title> A   title </title><summary>An abstract</summary>"
            "</entry></feed>"
        )
        http_client = MagicMock()
        http_client.get.return_value = _arxiv_response(feed_xml)

        result = _make_arxiv_client(http_client).search_by_title("A title")

        assert result == ("1706.03762", "A title")
        assert http_client.get.call_args.kwargs["params"] == {
            "search_query": 'ti:"A title"',
            "max_results": 1,
        }

    def test_existing_retry_policy_remains_compatible(self):
        http_client = MagicMock()
        http_client.get.side_effect = [
            _arxiv_response("failure", status_code=500),
            _arxiv_response(_arxiv_metadata_feed()),
        ]

        result = _make_arxiv_client(
            http_client, max_retries=1, backoff_seconds=0
        ).get_title_by_id("1706.03762")

        assert result == "A multi line title"
        assert http_client.get.call_count == 2

    def test_readme_documents_metadata_contract(self):
        readme = (Path(__file__).parents[1] / "README.md").read_text()

        for text in [
            "get_metadata",
            "timeout_seconds",
            "overall_timeout_seconds",
            "ArxivMetadataError",
            "requests.HTTPError",
            "requests.ConnectionError",
            "requests.Timeout",
            "paper body",
            "120",
            "None",
            "get_title_by_id",
            "/api/errors",
            "provider summary",
            "titled `Error`",
        ]:
            assert text in readme
