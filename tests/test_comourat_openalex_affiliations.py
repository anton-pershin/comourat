"""Mocked OpenAlex paper-author affiliation conversion tests."""

from datetime import date
from typing import Any
from unittest.mock import MagicMock

import pytest
import requests

from comourat.clients.openalex import (
    DEFAULT_SELECT_FIELDS,
    OPENALEX_API_BASE_URL,
    OpenAlexClient,
)
from comourat.models import AuthorRecord


def _json_response(payload: dict[str, Any], status_code: int = 200) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    if status_code >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(
            str(status_code), response=response
        )
    return response


def _make_client(http_client: MagicMock, **kwargs: Any) -> OpenAlexClient:
    settings: dict[str, Any] = {
        "user_agent": "comourat-test/0.1 (mailto:test@example.com)",
        "regular_delay_seconds": 0,
        "backoff_seconds": 0,
        "timeout_seconds": 7,
    }
    settings.update(kwargs)
    return OpenAlexClient(**settings, http_client=http_client)


def _authorship(
    name: str,
    author_id: Any = None,
    institutions: Any = None,
    raw_affiliation_strings: Any = None,
) -> dict[str, Any]:
    author: dict[str, Any] = {"display_name": name}
    if author_id is not ...:
        author["id"] = author_id
    return {
        "author": author,
        "institutions": institutions,
        "raw_affiliation_strings": raw_affiliation_strings,
    }


def _work(authorships: list[Any], **overrides: Any) -> dict[str, Any]:
    work: dict[str, Any] = {
        "id": "https://openalex.org/W1",
        "doi": "https://doi.org/10.1234/example",
        "display_name": "A paper",
        "locations": [],
        "authorships": authorships,
    }
    work.update(overrides)
    return work


def _resolve_authorships(authorships: list[Any]):
    http_client = MagicMock()
    http_client.get.return_value = _json_response(
        {"meta": {}, "results": [_work(authorships)]}
    )
    result = _make_client(http_client).resolve_by_doi("10.1234/example")
    assert result is not None
    return result


def test_resolve_by_doi_exposes_author_records_and_preserves_paper_fields():
    http_client = MagicMock()
    authorships = [
        _authorship(
            "Alice",
            "https://openalex.org/A1",
            [{"display_name": "Institute A"}],
            ["Lab A"],
        ),
        _authorship("Bob", ..., [], []),
    ]
    work = _work(
        authorships,
        abstract_inverted_index={"abstract": [0], "existing": [1]},
        publication_date="2026-01-02",
        cited_by_count=9,
        fwci=1.25,
        locations=[{"landing_page_url": "https://arxiv.org/abs/2401.00001v2"}],
    )
    http_client.get.return_value = _json_response({"meta": {}, "results": [work]})

    result = _make_client(http_client).resolve_by_doi("10.1234/example")

    assert result is not None
    assert result.identity.arxiv_id == "2401.00001"
    assert result.identity.doi == "10.1234/example"
    assert result.title == "A paper"
    assert result.authors == ["Alice", "Bob"]
    assert result.author_records == [
        AuthorRecord(
            author_id="https://openalex.org/A1",
            name="Alice",
            affiliations=["Institute A", "Lab A"],
        ),
        AuthorRecord(author_id=None, name="Bob", affiliations=[]),
    ]
    assert result.abstract == ""
    assert result.publication_date == date(2026, 1, 2)
    assert result.citation_count == 9
    assert result.raw_influence == {"fwci": 1.25}
    http_client.get.assert_called_once_with(
        OPENALEX_API_BASE_URL,
        params={
            "filter": "doi:10.1234/example",
            "per-page": 1,
            "select": ",".join(DEFAULT_SELECT_FIELDS),
        },
        timeout=7,
    )
    assert "authorships" in DEFAULT_SELECT_FIELDS


@pytest.mark.parametrize(
    ("institutions", "raw_strings", "expected"),
    [
        (
            [
                {"display_name": "Institute A"},
                {"display_name": ""},
                {"display_name": "Institute A"},
                {"display_name": None},
                {"display_name": 4},
                {},
                None,
                "malformed",
            ],
            None,
            ["Institute A"],
        ),
        (
            None,
            ["Raw A", "", "Raw A", None, 4, "Raw B"],
            ["Raw A", "Raw B"],
        ),
        (
            [{"display_name": "Shared"}, {"display_name": "Institute"}],
            ["Raw", "Shared", "institute", "Raw"],
            ["Shared", "Institute", "Raw", "institute"],
        ),
    ],
    ids=["institutions", "raw-strings", "combined-with-exact-duplicates"],
)
def test_affiliation_sources_keep_order_and_remove_exact_duplicates(
    institutions: Any, raw_strings: Any, expected: list[str]
):
    result = _resolve_authorships(
        [
            _authorship(
                "Alice",
                "https://openalex.org/A1",
                institutions,
                raw_strings,
            )
        ]
    )

    assert result.authors == ["Alice"]
    assert result.author_records == [
        AuthorRecord(
            author_id="https://openalex.org/A1",
            name="Alice",
            affiliations=expected,
        )
    ]


def test_partial_and_malformed_author_data_is_independent():
    result = _resolve_authorships(
        [
            _authorship(
                "Complete",
                "https://openalex.org/A1",
                [{"display_name": "Institute A"}],
                ["Raw A"],
            ),
            _authorship(
                "Missing ID",
                ...,
                [{"display_name": "Institute B"}],
                ["Raw B"],
            ),
            _authorship("Null ID", None, None, ["Raw C"]),
            _authorship("Empty ID", "", "malformed", {"malformed": True}),
            _authorship(
                "Malformed ID",
                42,
                [None, {"display_name": None}, {"display_name": ""}, "bad"],
                [None, "", 4],
            ),
            {"author": None, "institutions": [{"display_name": "Unused"}]},
            _authorship(
                "Other Complete",
                "https://openalex.org/A2",
                [{"display_name": "Institute D"}],
                [],
            ),
        ]
    )

    assert result.authors == [
        "Complete",
        "Missing ID",
        "Null ID",
        "Empty ID",
        "Malformed ID",
        "Other Complete",
    ]
    assert result.author_records == [
        AuthorRecord(
            author_id="https://openalex.org/A1",
            name="Complete",
            affiliations=["Institute A", "Raw A"],
        ),
        AuthorRecord(
            author_id=None,
            name="Missing ID",
            affiliations=["Institute B", "Raw B"],
        ),
        AuthorRecord(author_id=None, name="Null ID", affiliations=["Raw C"]),
        AuthorRecord(author_id=None, name="Empty ID", affiliations=[]),
        AuthorRecord(author_id=None, name="Malformed ID", affiliations=[]),
        AuthorRecord(
            author_id="https://openalex.org/A2",
            name="Other Complete",
            affiliations=["Institute D"],
        ),
    ]


def test_authors_without_usable_affiliations_keep_names_and_ids():
    result = _resolve_authorships(
        [
            _authorship("Alice", "https://openalex.org/A1"),
            _authorship("Bob", ..., [], []),
        ]
    )

    assert result.authors == ["Alice", "Bob"]
    assert result.author_records == [
        AuthorRecord(
            author_id="https://openalex.org/A1", name="Alice", affiliations=[]
        ),
        AuthorRecord(author_id=None, name="Bob", affiliations=[]),
    ]


@pytest.mark.parametrize("payload", [{"meta": {}, "results": []}, {"meta": {}}])
def test_unresolved_doi_returns_none(payload: dict[str, Any]):
    http_client = MagicMock()
    http_client.get.return_value = _json_response(payload)

    assert _make_client(http_client).resolve_by_doi("10.9999/missing") is None


def test_not_found_doi_returns_none():
    http_client = MagicMock()
    http_client.get.return_value = _json_response({}, status_code=404)

    assert _make_client(http_client).resolve_by_doi("10.9999/missing") is None
    assert http_client.get.call_count == 1


def test_terminal_http_failure_propagates_after_bounded_retries(monkeypatch):
    monkeypatch.setattr("comourat.clients.openalex.time.sleep", lambda _: None)
    http_client = MagicMock()
    http_client.get.side_effect = [
        _json_response({}, status_code=500),
        _json_response({}, status_code=500),
        _json_response({"meta": {}, "results": []}),
    ]

    with pytest.raises(requests.HTTPError):
        _make_client(http_client, max_retries=1).resolve_by_doi("10.1234/example")

    assert http_client.get.call_count == 2


def test_terminal_transport_failure_propagates_after_bounded_retries(monkeypatch):
    monkeypatch.setattr("comourat.clients.openalex.time.sleep", lambda _: None)
    http_client = MagicMock()
    http_client.get.side_effect = requests.Timeout("terminal")

    with pytest.raises(requests.Timeout):
        _make_client(http_client, max_retries=2).resolve_by_doi("10.1234/example")

    assert http_client.get.call_count == 3
