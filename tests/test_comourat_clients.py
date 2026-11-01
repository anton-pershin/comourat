"""comourat client tests: identity retrieval, search and citations (AC1/AC2).

Mirrors the mocked-HTTP style of tests/test_clients.py but imports the classes
from comourat, proving the library works standalone (spec 22 AC1/AC2).
"""

import json
from unittest.mock import MagicMock

import requests

from comourat.clients.arxiv import ArxivClient
from comourat.clients.openalex import DEFAULT_SELECT_FIELDS, OpenAlexClient
from comourat.models import PaperIdentity, normalize_arxiv_id, normalize_doi


def _make_client(http_client: MagicMock, **kwargs) -> OpenAlexClient:
    defaults: dict = {"user_agent": "comourat-test/0.1 (mailto:test@example.com)"}
    defaults.update(kwargs)
    return OpenAlexClient(**defaults, http_client=http_client)


def _make_arxiv_client(http_client: MagicMock, **kwargs) -> ArxivClient:
    defaults: dict = {"user_agent": "comourat-test/0.1 (mailto:test@example.com)"}
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
