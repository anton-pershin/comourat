"""Tests for authenticated single-post Reddit retrieval."""

from __future__ import annotations

import math
from unittest.mock import MagicMock

import prawcore
import pytest
import requests

from comourat.clients.reddit import PrawRedditSubmissionClient
from comourat.collectors.reddit import RedditSubmissionCollector

DUMMY_CLIENT_ID = "dummy-client-id"
DUMMY_CLIENT_SECRET = "dummy-client-secret"
DUMMY_ACCESS_TOKEN = "dummy-access-token"
USER_AGENT = "comourat-tests/1.0"


def _submission(**overrides):
    data = {
        "id": "abc123",
        "subreddit": "python",
        "title": "An authenticated post",
        "author": "researcher",
        "created_utc": 1704067200.0,
        "permalink": "/r/python/comments/abc123/an_authenticated_post/",
        "url": "https://example.org/article",
        "selftext": "Post body",
        "score": 42,
        "removed_by_category": None,
    }
    data.update(overrides)
    return data


def _payload(submission=None):
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


def _praw_client(payload=None, error=None):
    client = MagicMock()
    if error is None:
        client.request.return_value = (
            _payload(_submission()) if payload is None else payload
        )
    else:
        client.request.side_effect = error
    return client


def _response_error(status):
    response = MagicMock()
    response.status_code = status
    if status == 404:
        return prawcore.NotFound(response)
    return prawcore.ResponseException(response)


def test_injected_praw_client_delegates_submission_only_retrieval():
    praw_client = _praw_client()
    client = PrawRedditSubmissionClient(praw_client=praw_client)
    collector = RedditSubmissionCollector(client)

    result = collector.collect(" ABC123 ")

    praw_client.request.assert_called_once_with(
        method="GET",
        path="/api/info/",
        params={"id": "t3_abc123"},
    )
    praw_client.submission.assert_not_called()
    praw_client.subreddit.assert_not_called()
    assert result.submission_id == "abc123"
    assert result.title == "An authenticated post"
    assert result.text == "Post body"
    assert result.score == 42
    assert (
        result.url
        == "https://www.reddit.com/r/python/comments/abc123/an_authenticated_post/"
    )


def test_explicit_oauth_configuration_constructs_dedicated_praw_client(monkeypatch):
    first_praw = _praw_client()
    second_praw = _praw_client()
    constructor = MagicMock(side_effect=[first_praw, second_praw])
    monkeypatch.setattr("comourat.clients.reddit.praw.Reddit", constructor)

    first = PrawRedditSubmissionClient(
        client_id=DUMMY_CLIENT_ID,
        client_secret=DUMMY_CLIENT_SECRET,
        user_agent=USER_AGENT,
        operation_timeout=12.5,
    )
    second = PrawRedditSubmissionClient(
        client_id="second-client-id",
        client_secret="second-client-secret",
        user_agent="second-user-agent",
    )

    assert first.praw_client is first_praw
    assert second.praw_client is second_praw
    assert first.operation_timeout == 12.5
    assert second.operation_timeout == 30.0
    assert constructor.call_args_list[0].kwargs == {
        "site_name": "DEFAULT",
        "client_id": DUMMY_CLIENT_ID,
        "client_secret": DUMMY_CLIENT_SECRET,
        "user_agent": USER_AGENT,
        "check_for_updates": False,
        "requestor_kwargs": {"timeout": 12.5},
    }
    assert constructor.call_args_list[1].kwargs["client_id"] == "second-client-id"
    assert constructor.call_args_list[1].kwargs["requestor_kwargs"] == {"timeout": 30.0}


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"praw_client": MagicMock(), "client_id": DUMMY_CLIENT_ID},
        {"praw_client": MagicMock(), "client_secret": DUMMY_CLIENT_SECRET},
        {"praw_client": MagicMock(), "user_agent": USER_AGENT},
        {"client_id": DUMMY_CLIENT_ID},
        {"client_secret": DUMMY_CLIENT_SECRET},
        {"user_agent": USER_AGENT},
        {"client_id": DUMMY_CLIENT_ID, "client_secret": DUMMY_CLIENT_SECRET},
        {"client_id": DUMMY_CLIENT_ID, "user_agent": USER_AGENT},
        {"client_secret": DUMMY_CLIENT_SECRET, "user_agent": USER_AGENT},
        {
            "client_id": "",
            "client_secret": DUMMY_CLIENT_SECRET,
            "user_agent": USER_AGENT,
        },
        {"client_id": DUMMY_CLIENT_ID, "client_secret": " ", "user_agent": USER_AGENT},
        {
            "client_id": DUMMY_CLIENT_ID,
            "client_secret": DUMMY_CLIENT_SECRET,
            "user_agent": "",
        },
        {
            "client_id": 123,
            "client_secret": DUMMY_CLIENT_SECRET,
            "user_agent": USER_AGENT,
        },
    ],
)
def test_invalid_authentication_configuration_fails_before_praw_construction(
    monkeypatch, kwargs
):
    constructor = MagicMock()
    monkeypatch.setattr("comourat.clients.reddit.praw.Reddit", constructor)

    with pytest.raises(ValueError):
        PrawRedditSubmissionClient(**kwargs)

    constructor.assert_not_called()


@pytest.mark.parametrize("value", [0, -1, True, False, math.nan, math.inf, "10", None])
def test_invalid_operation_timeout_fails_before_praw_construction(monkeypatch, value):
    constructor = MagicMock()
    monkeypatch.setattr("comourat.clients.reddit.praw.Reddit", constructor)

    with pytest.raises(ValueError):
        PrawRedditSubmissionClient(
            client_id=DUMMY_CLIENT_ID,
            client_secret=DUMMY_CLIENT_SECRET,
            user_agent=USER_AGENT,
            operation_timeout=value,
        )

    constructor.assert_not_called()


@pytest.mark.parametrize("error", [_response_error(404), _response_error(410)])
def test_praw_unavailable_responses_raise_lookup_error(error):
    client = PrawRedditSubmissionClient(praw_client=_praw_client(error=error))

    with pytest.raises(LookupError) as caught:
        client.fetch_submission("abc123")

    assert caught.value.__cause__ is error


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        (_payload(), LookupError),
        (_payload(_submission(id="different")), LookupError),
        ({"kind": "Listing", "data": {"children": None}}, RuntimeError),
        ({"kind": "t3", "data": _submission()}, RuntimeError),
    ],
)
def test_listing_validation_matches_public_json_client(payload, expected_error):
    client = PrawRedditSubmissionClient(praw_client=_praw_client(payload=payload))
    collector = RedditSubmissionCollector(client)

    with pytest.raises(expected_error):
        collector.collect("abc123")


@pytest.mark.parametrize("error_kind", ["provider", "praw"])
def test_provider_failures_have_sanitized_fixed_messages(error_kind):
    sensitive_text = " | ".join(
        (
            DUMMY_CLIENT_SECRET,
            DUMMY_ACCESS_TOKEN,
            "https://user:secret@example.org/private?token=secret",
            "raw-provider-response",
        )
    )
    if error_kind == "provider":
        error = RuntimeError(sensitive_text)
    else:
        error = prawcore.RequestException(
            RuntimeError(sensitive_text),
            ("https://www.reddit.com/api/v1/access_token",),
            {"data": {"client_secret": DUMMY_CLIENT_SECRET}},
        )
    client = PrawRedditSubmissionClient(praw_client=_praw_client(error=error))

    with pytest.raises(RuntimeError) as caught:
        client.fetch_submission("abc123")

    assert str(caught.value) == "Reddit authenticated submission request failed"
    assert caught.value.__cause__ is error
    for sensitive_value in (
        DUMMY_CLIENT_SECRET,
        DUMMY_ACCESS_TOKEN,
        "raw-provider-response",
    ):
        assert sensitive_value not in str(caught.value)


@pytest.mark.parametrize(
    "transport_error",
    [
        requests.ConnectTimeout("connection timeout"),
        requests.ReadTimeout("read timeout"),
        TimeoutError("socket timeout"),
        prawcore.RequestException(requests.ReadTimeout("wrapped read timeout"), (), {}),
    ],
)
def test_direct_and_praw_wrapped_timeouts_have_one_public_outcome(transport_error):
    client = PrawRedditSubmissionClient(praw_client=_praw_client(error=transport_error))

    with pytest.raises(TimeoutError) as caught:
        client.fetch_submission("abc123")

    assert str(caught.value) == "Reddit authenticated submission request timed out"
    assert caught.value.__cause__ is transport_error


def test_late_praw_result_cannot_become_success(monkeypatch):
    clock = MagicMock(side_effect=[100.0, 130.0])
    monkeypatch.setattr("comourat.clients.reddit.time.monotonic", clock)
    client = PrawRedditSubmissionClient(
        praw_client=_praw_client(), operation_timeout=30
    )

    with pytest.raises(TimeoutError):
        client.fetch_submission("abc123")


def test_late_praw_failure_is_a_timeout(monkeypatch):
    clock = MagicMock(side_effect=[100.0, 131.0])
    monkeypatch.setattr("comourat.clients.reddit.time.monotonic", clock)
    error = RuntimeError("late provider failure")
    client = PrawRedditSubmissionClient(
        praw_client=_praw_client(error=error), operation_timeout=30
    )

    with pytest.raises(TimeoutError) as caught:
        client.fetch_submission("abc123")

    assert caught.value.__cause__ is error


@pytest.mark.parametrize("submission_id", [None, 123, ""])
def test_invalid_submission_ids_fail_before_authenticated_request(submission_id):
    praw_client = _praw_client()
    client = PrawRedditSubmissionClient(praw_client=praw_client)

    with pytest.raises(ValueError):
        client.fetch_submission(submission_id)

    praw_client.request.assert_not_called()
