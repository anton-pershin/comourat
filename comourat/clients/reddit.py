"""Submission-only access to Reddit's public API."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import requests

from comourat.fetch import FetchPolicy, SafeHttpClient

DEFAULT_REDDIT_API_URL = "https://www.reddit.com"
DEFAULT_CONNECTION_TIMEOUT = 10.0
DEFAULT_OPERATION_TIMEOUT = 30.0


class RedditSubmissionClient:
    """Retrieve one submission without using Reddit's comments endpoint."""

    def __init__(
        self,
        user_agent: str,
        *,
        http_session: requests.Session | None = None,
        api_url: str = DEFAULT_REDDIT_API_URL,
        connection_timeout: float = DEFAULT_CONNECTION_TIMEOUT,
        operation_timeout: float = DEFAULT_OPERATION_TIMEOUT,
    ) -> None:
        if not isinstance(user_agent, str) or not user_agent.strip():
            raise ValueError("user agent must be a non-empty string")
        self.user_agent = user_agent.strip()
        self.api_url = self._validate_api_url(api_url)
        policy = self._build_policy(connection_timeout, operation_timeout)
        self._http_client = SafeHttpClient(
            http_session=http_session, fetch_policy=policy
        )

    @staticmethod
    def _validate_api_url(api_url: str) -> str:
        if not isinstance(api_url, str) or not api_url.strip():
            raise ValueError("Reddit API URL must be a non-empty string")
        candidate = api_url.strip().rstrip("/")
        try:
            parts = urlsplit(candidate)
            port = parts.port
        except ValueError as exc:
            raise ValueError("Reddit API URL is invalid") from exc
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            raise ValueError("Reddit API URL is invalid")
        if (
            parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or port == 0
        ):
            raise ValueError("Reddit API URL is invalid")
        return candidate

    @staticmethod
    def _build_policy(
        connection_timeout: float, operation_timeout: float
    ) -> FetchPolicy:
        if connection_timeout is None:
            raise ValueError("connection timeout must be a positive finite number")
        return FetchPolicy(
            connection_timeout=connection_timeout,
            overall_timeout=operation_timeout,
            max_retries=0,
        )

    def fetch_submission(self, submission_id: str) -> dict[str, Any]:
        """Return the source data for one submission ID.

        Raises ValueError for an invalid ID, LookupError for an unavailable
        submission, TimeoutError for timeout expiry, and RuntimeError for other
        provider or response failures.
        """
        if not isinstance(submission_id, str) or not submission_id:
            raise ValueError("submission ID must be a non-empty string")
        try:
            response = self._http_client.get(
                f"{self.api_url}/api/info/",
                params={"id": f"t3_{submission_id}", "raw_json": 1},
                headers={"User-Agent": self.user_agent},
                allow_redirects=True,
            )
        except (requests.Timeout, TimeoutError) as exc:
            raise TimeoutError("Reddit submission request timed out") from exc
        except Exception as exc:
            raise RuntimeError("Reddit submission request failed") from exc

        try:
            return self._parse_response(response)
        finally:
            response.close()

    @staticmethod
    def _parse_response(response: requests.Response) -> dict[str, Any]:
        status = response.status_code
        if status in {404, 410}:
            try:
                response.raise_for_status()
            except requests.RequestException as exc:
                raise LookupError("Reddit submission is unavailable") from exc
            raise LookupError("Reddit submission is unavailable")
        if not isinstance(status, int) or not 200 <= status < 300:
            try:
                response.raise_for_status()
            except requests.RequestException as exc:
                raise RuntimeError("Reddit returned an HTTP error") from exc
            raise RuntimeError("Reddit returned an invalid HTTP response")
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("Reddit returned malformed JSON") from exc
        return RedditSubmissionClient._extract_submission(payload)

    @staticmethod
    def _extract_submission(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict) or payload.get("kind") != "Listing":
            raise RuntimeError("Reddit response is not a listing")
        listing = payload.get("data")
        if not isinstance(listing, dict):
            raise RuntimeError("Reddit listing has no valid data container")
        children = listing.get("children")
        if not isinstance(children, list):
            raise RuntimeError("Reddit listing has no valid children container")
        if not children:
            raise LookupError("Reddit submission is unavailable")
        if len(children) != 1:
            raise RuntimeError("Reddit returned an unexpected submission count")
        child = children[0]
        if not isinstance(child, dict) or child.get("kind") != "t3":
            raise RuntimeError("Reddit result is not a submission")
        submission = child.get("data")
        if not isinstance(submission, dict):
            raise RuntimeError("Reddit submission has no valid data container")
        return submission
