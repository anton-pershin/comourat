"""Mocked Semantic Scholar author-batch client contracts."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from comourat.clients import semantic_scholar
from comourat.clients.semantic_scholar import SemanticScholarClient

API_URL = "https://api.semanticscholar.org/graph/v1"


def _response(payload, status_code=200, headers=None):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    response.headers = headers or {}
    if status_code >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(
            f"HTTP {status_code}", response=response
        )
    return response


def _client(http_client, **overrides):
    values = {
        "http_client": http_client,
        "api_url": API_URL,
        "regular_delay_seconds": 0,
        "backoff_seconds": 0,
        "jitter_seconds": 0,
    }
    values.update(overrides)
    return SemanticScholarClient(**values)


def test_author_batch_request_and_complete_response_conversion():
    http = MagicMock()
    http.post.return_value = _response(
        [
            {
                "authorId": "author-1",
                "name": "First Author",
                "affiliations": ["First University", "Research Institute"],
            },
            {
                "authorId": "author-2",
                "name": "Second Author",
                "affiliations": [],
            },
        ]
    )

    records = _client(
        http,
        api_key="test-key",
        proxy="http://proxy.example:8080",
        timeout_seconds=12.5,
    ).get_authors(["author-1", "author-2"])

    assert [record.author_id for record in records] == ["author-1", "author-2"]
    assert [record.name for record in records] == ["First Author", "Second Author"]
    assert records[0].affiliations == ["First University", "Research Institute"]
    assert records[1].affiliations == []
    assert (
        type(records[0]).model_validate_json(records[0].model_dump_json()) == records[0]
    )
    http.post.assert_called_once_with(
        f"{API_URL}/author/batch",
        params={"fields": "name,affiliations"},
        headers={"x-api-key": "test-key"},
        timeout=12.5,
        json={"ids": ["author-1", "author-2"]},
    )
    http.proxies.update.assert_called_once_with(
        {"http": "http://proxy.example:8080", "https": "http://proxy.example:8080"}
    )


def test_author_batch_preserves_valid_partial_records():
    http = MagicMock()
    http.post.return_value = _response(
        [
            None,
            {
                "authorId": "author-1",
                "name": "First Author",
                "affiliations": ["First University"],
            },
            {"name": "No returned ID", "affiliations": ["Ignored Institute"]},
            {"authorId": "author-2", "name": "Second Author"},
            {"authorId": "author-3", "name": None, "affiliations": None},
            {"authorId": "author-4", "affiliations": []},
        ]
    )

    records = _client(http).get_authors(
        ["author-1", "omitted", "author-2", "author-3", "author-4"]
    )

    assert [record.author_id for record in records] == [
        "author-1",
        "author-2",
        "author-3",
        "author-4",
    ]
    assert [record.name for record in records] == [
        "First Author",
        "Second Author",
        "",
        "",
    ]
    assert [record.affiliations for record in records] == [
        ["First University"],
        [],
        [],
        [],
    ]


def test_author_batch_empty_response_returns_empty_list():
    http = MagicMock()
    http.post.return_value = _response([])

    assert _client(http).get_authors(["unknown-author"]) == []


def test_author_batch_empty_input_makes_no_request():
    http = MagicMock()

    assert _client(http).get_authors([]) == []

    http.post.assert_not_called()
    http.get.assert_not_called()


def test_author_batch_chunks_input_and_preserves_order(monkeypatch):
    monkeypatch.setattr(semantic_scholar, "AUTHOR_BATCH_SIZE", 2)
    http = MagicMock()

    def post(*args, **kwargs):
        return _response(
            [
                {"authorId": author_id, "name": author_id.upper()}
                for author_id in kwargs["json"]["ids"]
            ]
        )

    http.post.side_effect = post

    records = _client(http).get_authors(["a1", "a2", "a3", "a4", "a5"])

    assert [record.author_id for record in records] == ["a1", "a2", "a3", "a4", "a5"]
    assert http.post.call_count == 3
    assert [call.kwargs["json"] for call in http.post.call_args_list] == [
        {"ids": ["a1", "a2"]},
        {"ids": ["a3", "a4"]},
        {"ids": ["a5"]},
    ]


def test_author_batch_retry_after_and_regular_delay(monkeypatch):
    sleeps = []
    monkeypatch.setattr(semantic_scholar.time, "sleep", sleeps.append)
    monkeypatch.setattr(semantic_scholar.random, "uniform", lambda low, high: high)
    http = MagicMock()
    http.post.side_effect = [
        _response({"message": "limited"}, 429, {"Retry-After": "3"}),
        _response([{"authorId": "a1", "name": "Author", "affiliations": []}]),
    ]

    records = _client(
        http,
        max_retries=1,
        jitter_seconds=0.25,
        regular_delay_seconds=0.5,
    ).get_authors(["a1"])

    assert len(records) == 1
    assert sleeps == [3.25, 0.5]
    assert http.post.call_count == 2


def test_author_batch_uses_exponential_backoff(monkeypatch):
    sleeps = []
    monkeypatch.setattr(semantic_scholar.time, "sleep", sleeps.append)
    http = MagicMock()
    http.post.side_effect = [
        _response({"message": "down"}, 500),
        _response([]),
    ]

    assert _client(http, max_retries=1, backoff_seconds=2).get_authors(["a1"]) == []

    assert sleeps == [2.0]


def test_author_batch_status_exhaustion_raises(monkeypatch):
    monkeypatch.setattr(semantic_scholar.time, "sleep", lambda delay: None)
    http = MagicMock()
    http.post.return_value = _response({"message": "down"}, 500)

    with pytest.raises(requests.HTTPError):
        _client(http, max_retries=1).get_authors(["a1"])

    assert http.post.call_count == 2


def test_author_batch_non_retryable_status_raises_at_once():
    http = MagicMock()
    http.post.return_value = _response({"message": "bad request"}, 400)

    with pytest.raises(requests.HTTPError):
        _client(http).get_authors(["a1"])

    http.post.assert_called_once()


def test_author_batch_terminal_transport_failure_reaches_caller():
    http = MagicMock()
    failure = requests.ConnectionError("connection failed")
    http.post.side_effect = failure

    with pytest.raises(requests.ConnectionError) as exc_info:
        _client(http, max_retries=2).get_authors(["a1"])

    assert exc_info.value is failure
    http.post.assert_called_once()
