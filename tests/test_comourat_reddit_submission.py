"""Tests for collecting one specified Reddit post without comments."""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest
import requests

from comourat.clients.reddit import RedditSubmissionClient
from comourat.collectors.reddit import RedditSubmissionCollector
from comourat.fetch import FetchDeadlineExceeded
from comourat.models import RedditPostInfo

USER_AGENT = "comourat-tests/1.0"


def _submission(**overrides):
    data = {
        "id": "abc123",
        "subreddit": "python",
        "title": "A specified post",
        "author": "researcher",
        "created_utc": 1704067200.0,
        "permalink": "/r/python/comments/abc123/a_specified_post/",
        "url": "https://example.org/article",
        "selftext": "Post body",
        "score": 42,
        "removed_by_category": None,
    }
    data.update(overrides)
    return data


def _payload(submission=None, children=None):
    if children is None:
        children = [] if submission is None else [{"kind": "t3", "data": submission}]
    return {
        "kind": "Listing",
        "data": {
            "after": None,
            "before": None,
            "dist": len(children),
            "children": children,
        },
    }


def _response(status=200, payload=None, json_error=None):
    response = MagicMock()
    response.status_code = status
    if json_error is not None:
        response.json.side_effect = json_error
    else:
        response.json.return_value = payload
    if status >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(
            f"HTTP {status}", response=response
        )
    return response


def _collector(response=None, *, session=None, **client_kwargs):
    session = session or MagicMock()
    session.proxies = {}
    if response is not None:
        session.get.return_value = response
    client = RedditSubmissionClient(
        user_agent=USER_AGENT,
        http_session=session,
        **client_kwargs,
    )
    return RedditSubmissionCollector(client), session


def _assert_info_request(session, submission_id="abc123"):
    session.get.assert_called_once()
    args, kwargs = session.get.call_args
    assert args == ("https://www.reddit.com/api/info/",)
    assert kwargs["params"] == {"id": f"t3_{submission_id}", "raw_json": 1}
    assert kwargs["headers"] == {"User-Agent": USER_AGENT}
    assert kwargs["timeout"] == 30.0
    assert kwargs["allow_redirects"] is True
    assert "comments" not in args[0]
    assert "subreddit" not in args[0]


@pytest.mark.parametrize(
    ("reference", "submission_id"),
    [
        ("abc123", "abc123"),
        (" ABC123 ", "abc123"),
        ("https://www.reddit.com/r/python/comments/abc123/a_title/", "abc123"),
        ("https://old.reddit.com/comments/abc123/", "abc123"),
        (
            "https://np.reddit.com/r/python/comments/abc123/a_title/?utm_source=x",
            "abc123",
        ),
        ("https://reddit.com/gallery/abc123", "abc123"),
        ("https://redd.it/abc123", "abc123"),
        ("http://redd.it/abc123/", "abc123"),
    ],
)
def test_collect_accepts_ids_and_direct_reddit_urls(reference, submission_id):
    response = _response(payload=_payload(_submission(id=submission_id)))
    collector, session = _collector(response)

    result = collector.collect(reference)

    assert isinstance(result, RedditPostInfo)
    assert result.submission_id == submission_id
    _assert_info_request(session, submission_id)


def test_collect_maps_canonical_permalink_and_all_model_fields():
    submission = _submission(
        permalink="/r/python/comments/abc123/canonical_title/",
        url="https://example.org/external-article",
        created_utc=1704067200,
        score=-3,
    )
    collector, session = _collector(_response(payload=_payload(submission)))

    result = collector.collect("abc123")

    assert result == RedditPostInfo(
        subreddit="python",
        submission_id="abc123",
        title="A specified post",
        author="researcher",
        date="2024-01-01T00:00:00+00:00",
        url="https://www.reddit.com/r/python/comments/abc123/canonical_title/",
        text="Post body",
        score=-3,
    )
    _assert_info_request(session)


def test_collect_accepts_link_post_with_empty_text():
    submission = _submission(selftext="", url="https://example.org/linked-content")
    collector, _ = _collector(_response(payload=_payload(submission)))

    result = collector.collect("abc123")

    assert result.text == ""
    assert (
        result.url
        == "https://www.reddit.com/r/python/comments/abc123/a_specified_post/"
    )


@pytest.mark.parametrize(
    "reference",
    [
        None,
        123,
        "",
        "   ",
        "not-an-id",
        "t3_abc123",
        "abc_123",
        "é",
        "https://example.com/comments/abc123",
        "https://www.reddit.com.example.com/comments/abc123",
        "ftp://reddit.com/comments/abc123",
        "file:///etc/passwd",
        "https://reddit.com/r/python",
        "https://reddit.com/comments/",
        "https://reddit.com/r/python/comments",
        "https://reddit.com/gallery",
        "https://redd.it/",
        "https://redd.it/abc123/extra",
        "https://reddit.com/r/python/s/share-token",
        "not a url",
    ],
)
def test_invalid_references_fail_before_any_source_request(reference):
    collector, session = _collector()

    with pytest.raises(ValueError):
        collector.collect(reference)

    session.get.assert_not_called()


@pytest.mark.parametrize("status", [404, 410])
def test_unavailable_http_status_raises_lookup_error(status):
    collector, session = _collector(_response(status=status))

    with pytest.raises(LookupError) as error:
        collector.collect("abc123")

    assert isinstance(error.value.__cause__, requests.HTTPError)
    _assert_info_request(session)


@pytest.mark.parametrize(
    "submission",
    [
        None,
        _submission(id="different"),
        _submission(selftext="[deleted]"),
        _submission(selftext="[removed]"),
        _submission(removed_by_category="moderator"),
        _submission(removed_by_category="deleted"),
    ],
)
def test_unavailable_submission_data_raises_lookup_error(submission):
    collector, session = _collector(_response(payload=_payload(submission)))

    with pytest.raises(LookupError):
        collector.collect("abc123")

    _assert_info_request(session)


@pytest.mark.parametrize("status", [400, 403, 429, 500, 503])
def test_other_http_errors_raise_runtime_error(status):
    collector, _ = _collector(_response(status=status))

    with pytest.raises(RuntimeError) as error:
        collector.collect("abc123")

    assert isinstance(error.value.__cause__, requests.HTTPError)


@pytest.mark.parametrize(
    "provider_error",
    [
        requests.ConnectionError("connection failed"),
        requests.RequestException("provider failed"),
        RuntimeError("provider failed"),
    ],
)
def test_provider_errors_raise_runtime_error_with_cause(provider_error):
    collector, session = _collector()
    session.get.side_effect = provider_error

    with pytest.raises(RuntimeError) as error:
        collector.collect("abc123")

    assert error.value.__cause__ is provider_error


def test_malformed_json_raises_runtime_error_with_cause():
    json_error = ValueError("not JSON")
    collector, _ = _collector(_response(json_error=json_error))

    with pytest.raises(RuntimeError) as error:
        collector.collect("abc123")

    assert error.value.__cause__ is json_error


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"kind": "Listing", "data": None},
        {"kind": "Listing", "data": {"children": None}},
        {"kind": "Listing", "data": {"children": {}}},
        _payload(children=[{"kind": "t1", "data": _submission()}]),
        _payload(children=[{"kind": "t3"}]),
        _payload(children=[{"kind": "t3", "data": []}]),
        _payload(
            children=[
                {"kind": "t3", "data": _submission()},
                {"kind": "t3", "data": _submission()},
            ]
        ),
    ],
)
def test_malformed_result_containers_raise_runtime_error(payload):
    collector, _ = _collector(_response(payload=payload))

    with pytest.raises(RuntimeError):
        collector.collect("abc123")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", None),
        ("subreddit", None),
        ("subreddit", ""),
        ("title", None),
        ("author", None),
        ("created_utc", None),
        ("created_utc", True),
        ("created_utc", "yesterday"),
        ("created_utc", math.inf),
        ("permalink", None),
        ("permalink", "https://example.org/not-reddit"),
        ("permalink", "/r/python/comments/different/wrong_post/"),
        ("selftext", None),
        ("score", None),
        ("score", True),
        ("score", "42"),
    ],
)
def test_invalid_required_submission_fields_raise_runtime_error(field, value):
    collector, _ = _collector(
        _response(payload=_payload(_submission(**{field: value})))
    )

    with pytest.raises(RuntimeError):
        collector.collect("abc123")


@pytest.mark.parametrize(
    "field",
    [
        "id",
        "subreddit",
        "title",
        "author",
        "created_utc",
        "permalink",
        "selftext",
        "score",
    ],
)
def test_missing_required_submission_fields_raise_runtime_error(field):
    submission = _submission()
    submission.pop(field)
    collector, _ = _collector(_response(payload=_payload(submission)))

    with pytest.raises(RuntimeError):
        collector.collect("abc123")


def test_timeout_defaults_and_policy_are_visible_on_protected_client():
    collector, _ = _collector()
    policy = collector.reddit_client._http_client.policy

    assert policy.connection_timeout == 10.0
    assert policy.overall_timeout == 30.0
    assert policy.max_retries == 0


@pytest.mark.parametrize("value", [0, -1, True, False, math.nan, math.inf, "10", None])
@pytest.mark.parametrize("argument", ["connection_timeout", "operation_timeout"])
def test_invalid_timeout_configuration_fails_before_source_request(argument, value):
    with pytest.raises(ValueError):
        _collector(**{argument: value})


@pytest.mark.parametrize(
    "transport_error",
    [
        requests.ConnectTimeout("connection timeout"),
        requests.ReadTimeout("read timeout"),
        FetchDeadlineExceeded("overall timeout"),
        TimeoutError("socket timeout"),
    ],
)
def test_transport_timeouts_have_one_public_timeout_outcome(transport_error):
    collector, session = _collector()
    session.get.side_effect = transport_error

    with pytest.raises(TimeoutError) as error:
        collector.collect("abc123")

    assert error.value.__cause__ is transport_error


def test_callers_use_isolated_clients_sessions_and_timeouts_concurrently():
    first_response = _response(payload=_payload(_submission(title="First")))
    second_response = _response(payload=_payload(_submission(title="Second")))
    first, first_session = _collector(
        first_response, connection_timeout=1, operation_timeout=5
    )
    second, second_session = _collector(
        second_response, connection_timeout=2, operation_timeout=6
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(lambda collector: collector.collect("abc123"), [first, second])
        )

    assert [result.title for result in results] == ["First", "Second"]
    assert first.reddit_client._http_client.policy.connection_timeout == 1
    assert first.reddit_client._http_client.policy.overall_timeout == 5
    assert second.reddit_client._http_client.policy.connection_timeout == 2
    assert second.reddit_client._http_client.policy.overall_timeout == 6
    assert first_session.get.call_args.kwargs["timeout"] == 5.0
    assert second_session.get.call_args.kwargs["timeout"] == 6.0


def test_environment_configuration_is_not_used(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("REDDIT_TIMEOUT", "1")
    session = requests.Session()

    RedditSubmissionClient(user_agent=USER_AGENT, http_session=session)

    assert session.trust_env is False
    assert session.proxies == {}
