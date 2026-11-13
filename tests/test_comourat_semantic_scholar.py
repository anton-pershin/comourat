"""Mocked Semantic Scholar bulk-search collector contracts."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from comourat.collectors.semantic_scholar import SemanticScholarPaperCollectorCore
from comourat.models import PaperInfoCollection, SemanticScholarBulkSearchResult

API_URL = "https://api.semanticscholar.org/graph/v1"
REQUEST = httpx.Request("GET", API_URL)


def _response(payload, status_code=200, headers=None):
    return httpx.Response(
        status_code,
        json=payload,
        headers=headers,
        request=REQUEST,
    )


def _paper(
    paper_id="ss-1",
    title="Journal paper",
    doi="10.1234/journal",
    arxiv_id=None,
    publication_date="2024-01-02",
):
    external_ids = {}
    if doi is not None:
        external_ids["DOI"] = doi
    if arxiv_id is not None:
        external_ids["ArXiv"] = arxiv_id
    return {
        "paperId": paper_id,
        "title": title,
        "url": f"https://example.com/{paper_id}",
        "abstract": "Abstract",
        "publicationDate": publication_date,
        "authors": [{"name": "A. Author"}, {"name": "B. Author"}],
        "externalIds": external_ids,
    }


def _legacy_paper(title="Legacy paper"):
    return {
        "title": title,
        "url": "https://example.com/legacy",
        "abstract": "Legacy abstract",
        "citationCount": 7,
        "publicationDate": "2024-01-02",
        "authors": [{"name": "Legacy Author"}],
    }


def _collector(http_client, **overrides):
    values = {
        "http_client": http_client,
        "api_url": API_URL,
        "mode": "publication_window",
        "queries": ["keyword query"],
        "start_date": "2024-01-02",
        "end_date": "2024-01-03",
        "regular_delay_seconds": 0,
        "jitter_seconds": 0,
        "backoff_seconds": 0,
    }
    values.update(overrides)
    return SemanticScholarPaperCollectorCore(**values)


def _legacy_collector(http_client, mode="newest", max_results=1):
    return SemanticScholarPaperCollectorCore(
        http_client,
        API_URL,
        mode,
        "2024-01-01",
        "2024-01-31",
        max_results,
        "legacy query",
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"queries": []},
        {"queries": ["valid", " "]},
        {"start_date": None},
        {"end_date": None},
        {"start_date": "2024-01-04", "end_date": "2024-01-03"},
        {"max_results_per_query": 0},
        {"max_retries": -1},
    ],
)
def test_publication_window_validates_configuration(overrides):
    http = MagicMock()

    with pytest.raises(ValueError):
        _collector(http, **overrides)

    http.get.assert_not_called()


def test_multiple_queries_map_candidates_and_report_counts():
    http = MagicMock()
    http.get.side_effect = [
        _response(
            {
                "data": [
                    _paper(
                        arxiv_id="arXiv:2401.00001v2",
                        doi="https://doi.org/10.1234/First",
                    )
                ],
                "token": None,
            }
        ),
        _response(
            {
                "data": [
                    _paper(
                        paper_id="ss-2",
                        title="Second paper",
                        doi="DOI:10.5678/Second",
                        publication_date="2024-01-03",
                    )
                ],
                "token": None,
            }
        ),
    ]

    result = _collector(
        http,
        queries=["  first query  ", "second query"],
        api_key="test-key",
        timeout_seconds=12.5,
    ).collect()

    assert isinstance(result, SemanticScholarBulkSearchResult)
    assert [paper.semantic_scholar_id for paper in result.papers] == ["ss-1", "ss-2"]
    first = result.papers[0]
    assert first.title == "Journal paper"
    assert first.authors == ["A. Author", "B. Author"]
    assert first.description == "Abstract"
    assert first.urls_seen == ["https://example.com/ss-1"]
    assert first.arxiv_id == "2401.00001"
    assert first.doi == "10.1234/First"
    assert first.publication_date == "2024-01-02"
    assert first.provenance == ["semantic_scholar"]
    assert result.papers[1].doi == "10.5678/Second"
    assert [
        (query.query, query.candidate_count, query.cap_reached)
        for query in result.query_results
    ] == [("first query", 1, False), ("second query", 1, False)]
    assert (
        SemanticScholarBulkSearchResult.model_validate_json(result.model_dump_json())
        == result
    )

    assert http.get.call_count == 2
    for index, call in enumerate(http.get.call_args_list):
        assert call.args[0] == f"{API_URL}/paper/search/bulk"
        assert call.kwargs["headers"] == {"x-api-key": "test-key"}
        assert call.kwargs["timeout"] == 12.5
        params = call.kwargs["params"]
        assert params["query"] == ("first query" if index == 0 else "second query")
        assert params["sort"] == "publicationDate:desc"
        assert params["publicationDateOrYear"] == "2024-01-02:2024-01-03"
        assert params["fields"].split(",") == [
            "paperId",
            "title",
            "url",
            "abstract",
            "publicationDate",
            "authors",
            "externalIds",
        ]


def test_candidate_filtering_and_provider_id_absence():
    http = MagicMock()
    http.get.return_value = _response(
        {
            "data": [
                _paper("start", publication_date="2024-01-02"),
                _paper("end", publication_date="2024-01-03"),
                _paper("before", publication_date="2024-01-01"),
                _paper("after", publication_date="2024-01-04"),
                _paper("missing", publication_date=None),
                _paper("malformed", publication_date="not-a-date"),
                _paper("no-title", title=None),
                _paper(None, title="No provider ID"),
            ],
            "token": None,
        }
    )

    result = _collector(http).collect()

    assert [paper.semantic_scholar_id for paper in result.papers] == [
        "start",
        "end",
        None,
    ]
    assert result.query_results[0].candidate_count == 3


def test_arxiv_doi_uses_normalized_arxiv_identity():
    http = MagicMock()
    http.get.return_value = _response(
        {
            "data": [
                _paper(
                    arxiv_id="2401.00001v3",
                    doi="10.48550/arXiv.2401.00001",
                )
            ],
            "token": None,
        }
    )

    result = _collector(http).collect()

    assert result.papers[0].arxiv_id == "2401.00001"
    assert result.papers[0].doi is None


def test_uncapped_pagination_follows_all_tokens():
    http = MagicMock()
    http.get.side_effect = [
        _response({"data": [_paper("ss-1")], "token": "page-two"}),
        _response({"data": [_paper("ss-2")], "token": "page-three"}),
        _response({"data": [_paper("ss-3")], "token": None}),
    ]

    result = _collector(http, max_results_per_query=None).collect()

    assert [paper.semantic_scholar_id for paper in result.papers] == [
        "ss-1",
        "ss-2",
        "ss-3",
    ]
    assert http.get.call_count == 3
    assert "token" not in http.get.call_args_list[0].kwargs["params"]
    assert http.get.call_args_list[1].kwargs["params"]["token"] == "page-two"
    assert http.get.call_args_list[2].kwargs["params"]["token"] == "page-three"
    assert result.query_results[0].candidate_count == 3
    assert result.query_results[0].cap_reached is False


def test_per_query_cap_stops_within_and_across_pages():
    http = MagicMock()
    http.get.side_effect = [
        _response({"data": [_paper("q1-first")], "token": "q1-page-two"}),
        _response({"data": [_paper("q1-second")], "token": "q1-unused"}),
        _response(
            {
                "data": [_paper("q2-first"), _paper("q2-second")],
                "token": "q2-unused",
            }
        ),
    ]

    result = _collector(
        http,
        queries=["first", "second"],
        max_results_per_query=2,
    ).collect()

    assert [paper.semantic_scholar_id for paper in result.papers] == [
        "q1-first",
        "q1-second",
        "q2-first",
        "q2-second",
    ]
    assert http.get.call_count == 3
    assert http.get.call_args_list[1].kwargs["params"]["token"] == "q1-page-two"
    assert all(
        "token" not in call.kwargs["params"]
        for call in [http.get.call_args_list[0], http.get.call_args_list[2]]
    )
    assert [query.candidate_count for query in result.query_results] == [2, 2]
    assert [query.cap_reached for query in result.query_results] == [True, True]


def test_cap_counts_only_accepted_candidates_per_query():
    http = MagicMock()
    http.get.side_effect = [
        _response(
            {
                "data": [
                    _paper("q1-outside", publication_date="2024-01-01"),
                    _paper("q1-first"),
                    _paper("q1-malformed", publication_date="invalid"),
                    _paper("q1-second"),
                ],
                "token": "q1-unused",
            }
        ),
        _response(
            {
                "data": [
                    _paper("q2-first"),
                    _paper("q2-outside", publication_date="2024-01-04"),
                ],
                "token": None,
            }
        ),
    ]

    result = _collector(
        http,
        queries=["first", "second"],
        max_results_per_query=2,
    ).collect()

    assert [paper.semantic_scholar_id for paper in result.papers] == [
        "q1-first",
        "q1-second",
        "q2-first",
    ]
    assert http.get.call_count == 2
    assert [query.candidate_count for query in result.query_results] == [2, 1]
    assert [query.cap_reached for query in result.query_results] == [True, False]


def test_retry_after_recovers_and_status_exhaustion_raises(monkeypatch):
    from comourat.collectors import semantic_scholar

    sleeps = []
    monkeypatch.setattr(semantic_scholar.time, "sleep", sleeps.append)
    monkeypatch.setattr(semantic_scholar.random, "uniform", lambda low, high: high)
    http = MagicMock()
    http.get.side_effect = [
        _response({"message": "limited"}, 429, {"Retry-After": "3"}),
        _response({"data": [_paper()], "token": None}),
    ]

    result = _collector(
        http,
        max_retries=1,
        jitter_seconds=0.25,
        regular_delay_seconds=0.5,
    ).collect()

    assert len(result.papers) == 1
    assert sleeps == [3.25, 0.5]

    http.reset_mock()
    http.get.side_effect = [
        _response({"message": "down"}, 500),
        _response({"message": "down"}, 500),
    ]
    with pytest.raises(httpx.HTTPStatusError):
        _collector(http, max_retries=1).collect()
    assert http.get.call_count == 2


def test_backoff_and_non_retryable_status(monkeypatch):
    from comourat.collectors import semantic_scholar

    sleeps = []
    monkeypatch.setattr(semantic_scholar.time, "sleep", sleeps.append)
    http = MagicMock()
    http.get.side_effect = [
        _response({"message": "down"}, 500),
        _response({"data": [_paper()], "token": None}),
    ]

    result = _collector(http, max_retries=1, backoff_seconds=2).collect()

    assert len(result.papers) == 1
    assert sleeps == [2.0]

    http.reset_mock()
    http.get.side_effect = None
    http.get.return_value = _response({"message": "bad request"}, 400)
    with pytest.raises(httpx.HTTPStatusError):
        _collector(http).collect()
    assert http.get.call_count == 1


@pytest.mark.parametrize("retry_after", ["-1", "nan", "inf", "not-a-date"])
def test_invalid_retry_after_uses_exponential_backoff(monkeypatch, retry_after):
    from comourat.collectors import semantic_scholar

    sleeps = []
    monkeypatch.setattr(semantic_scholar.time, "sleep", sleeps.append)
    http = MagicMock()
    http.get.side_effect = [
        _response({"message": "down"}, 500, {"Retry-After": retry_after}),
        _response({"data": [_paper()], "token": None}),
    ]

    result = _collector(http, max_retries=1, backoff_seconds=2).collect()

    assert len(result.papers) == 1
    assert sleeps == [2.0]


def test_transport_errors_retry_and_propagate_after_exhaustion(monkeypatch):
    from comourat.collectors import semantic_scholar

    sleeps = []
    monkeypatch.setattr(semantic_scholar.time, "sleep", sleeps.append)
    http = MagicMock()
    http.get.side_effect = [
        httpx.ConnectError("connection failed", request=REQUEST),
        _response({"data": [_paper()], "token": None}),
    ]

    result = _collector(http, max_retries=1, backoff_seconds=1).collect()

    assert len(result.papers) == 1
    assert sleeps == [1.0]

    http.reset_mock()
    http.get.side_effect = httpx.TimeoutException("timeout", request=REQUEST)
    with pytest.raises(httpx.TimeoutException):
        _collector(http, max_retries=1).collect()
    assert http.get.call_count == 2


@pytest.mark.parametrize(
    ("mode", "sort"),
    [("newest", "publicationDate:desc"), ("most_influential", "citationCount:desc")],
)
def test_legacy_bulk_modes_keep_request_and_result_contracts(mode, sort):
    http = MagicMock()
    http.get.return_value = _response({"data": [_legacy_paper()], "token": "unused"})

    result = _legacy_collector(http, mode=mode).collect()

    assert isinstance(result, PaperInfoCollection)
    assert result.papers[0].title == "Legacy paper"
    assert result.papers[0].citation_count == 7
    assert http.get.call_count == 1
    call = http.get.call_args
    assert call.args[0] == f"{API_URL}/paper/search/bulk"
    assert call.kwargs["params"]["query"] == "legacy query"
    assert call.kwargs["params"]["sort"] == sort
    assert call.kwargs["params"]["publicationDateOrYear"] == ("2024-01-01:2024-01-31")
    assert call.kwargs["params"]["fields"] == (
        "title,url,abstract,citationCount,publicationDate,authors"
    )


def test_legacy_bulk_mode_retains_aligned_author_ids():
    paper = _legacy_paper()
    paper["authors"] = [
        {"authorId": "author-1", "name": "First Author"},
        {"name": "Second Author"},
        {"authorId": None, "name": "Third Author"},
        {"authorId": "author-4", "name": "Fourth Author"},
    ]
    http = MagicMock()
    http.get.return_value = _response({"data": [paper], "token": None})

    result = _legacy_collector(http).collect()

    assert result.papers[0].authors == [
        "First Author",
        "Second Author",
        "Third Author",
        "Fourth Author",
    ]
    assert result.papers[0].author_ids == ["author-1", None, None, "author-4"]


def test_legacy_bulk_mode_follows_continuation_tokens():
    http = MagicMock()
    http.get.side_effect = [
        _response({"data": [_legacy_paper("First")], "token": "page-two"}),
        _response({"data": [_legacy_paper("Second")], "token": None}),
    ]

    result = _legacy_collector(http, max_results=2).collect()

    assert [paper.title for paper in result.papers] == ["First", "Second"]
    assert http.get.call_count == 2
    assert http.get.call_args.kwargs["params"]["token"] == "page-two"


def test_legacy_most_relevant_mode_keeps_offset_search_contract():
    http = MagicMock()
    http.get.side_effect = [
        _response({"data": [_legacy_paper("First")], "next": 2}),
        _response({"data": [_legacy_paper("Second")], "next": None}),
    ]

    result = _legacy_collector(http, mode="most_relevant", max_results=2).collect()

    assert isinstance(result, PaperInfoCollection)
    assert [paper.title for paper in result.papers] == ["First", "Second"]
    assert http.get.call_count == 2
    call = http.get.call_args
    assert call.args[0] == f"{API_URL}/paper/search"
    assert call.kwargs["params"] == {"query": "legacy query", "offset": 2}


def test_legacy_provider_error_retry_remains_five_seconds(monkeypatch):
    from comourat.collectors import semantic_scholar

    sleeps = []
    monkeypatch.setattr(semantic_scholar.time, "sleep", sleeps.append)
    http = MagicMock()
    http.get.side_effect = [
        _response({"code": 429, "message": "limited"}),
        _response({"data": [_legacy_paper()], "token": None}),
    ]

    result = _legacy_collector(http).collect()

    assert len(result.papers) == 1
    assert http.get.call_count == 2
    assert sleeps == [5]


def test_legacy_transport_error_is_not_retried():
    http = MagicMock()
    http.get.side_effect = httpx.ConnectError("connection failed", request=REQUEST)

    with pytest.raises(httpx.ConnectError):
        _legacy_collector(http).collect()

    assert http.get.call_count == 1
