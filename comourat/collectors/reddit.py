"""Reddit feed and single-submission collection logic."""

from __future__ import annotations

import datetime
import math
import re
from typing import Any
from urllib.parse import urlsplit

import praw

from comourat.clients.reddit import PrawRedditSubmissionClient, RedditSubmissionClient
from comourat.models import RedditPostCollection, RedditPostInfo

_SUBMISSION_ID = re.compile(r"[0-9A-Za-z]+")
_REMOVED_TEXT = {"[deleted]", "[removed]"}


def _host_matches(hostname: str, domain: str) -> bool:
    return hostname == domain or hostname.endswith(f".{domain}")


def _submission_id_from_reference(reference: str) -> str:
    """Extract a bare ASCII base36 submission ID without network access."""
    if not isinstance(reference, str):
        raise ValueError("Reddit post reference must be a string")
    candidate = reference.strip()
    if _SUBMISSION_ID.fullmatch(candidate):
        return candidate.lower()
    if not candidate:
        raise ValueError("Reddit post reference is empty")
    try:
        parts = urlsplit(candidate)
        port = parts.port
    except ValueError as exc:
        raise ValueError("Reddit post URL is invalid") from exc
    hostname = (parts.hostname or "").rstrip(".").lower()
    if (
        parts.scheme.lower() not in {"http", "https"}
        or not hostname
        or parts.username is not None
        or parts.password is not None
        or port == 0
    ):
        raise ValueError("Reddit post URL is not supported")
    path = [segment for segment in parts.path.split("/") if segment]
    submission_id = None
    if _host_matches(hostname, "redd.it"):
        if len(path) == 1:
            submission_id = path[0]
    elif _host_matches(hostname, "reddit.com"):
        if len(path) >= 2 and path[0] == "comments":
            submission_id = path[1]
        elif len(path) >= 4 and path[0] == "r" and path[2] == "comments":
            submission_id = path[3]
        elif len(path) == 2 and path[0] == "gallery":
            submission_id = path[1]
    if submission_id is None or not _SUBMISSION_ID.fullmatch(submission_id):
        raise ValueError("Reddit post URL has no valid submission ID")
    return submission_id.lower()


class RedditPostCollectorCore:
    """Collects posts from Reddit subreddits within a time window."""

    def __init__(
        self,
        reddit_client: praw.Reddit,
        subreddits: list[str],
        time_window: dict,  # kwargs for timedelta (e.g., {"hours": 24})
        limit_per_subreddit: int = 100,
        max_posts_per_subreddit: int = 10,
        require_text: bool = True,
        now_fn=None,
    ) -> None:
        self.reddit_client = reddit_client
        self.subreddits = subreddits
        self.time_window = datetime.timedelta(**time_window)
        self.limit_per_subreddit = limit_per_subreddit
        self.max_posts_per_subreddit = max_posts_per_subreddit
        self.require_text = require_text
        self.now_fn = now_fn or (lambda: datetime.datetime.now(datetime.UTC))

    def collect(self) -> RedditPostCollection:
        cutoff = self.now_fn() - self.time_window
        all_posts: list[RedditPostInfo] = []

        for subreddit_name in self.subreddits:
            subreddit = self.reddit_client.subreddit(subreddit_name)
            posts = []

            for post in subreddit.new(limit=self.limit_per_subreddit):
                post_time = datetime.datetime.fromtimestamp(
                    post.created_utc, tz=datetime.UTC
                )
                if post_time < cutoff:
                    break

                if self.require_text and not post.selftext.strip():
                    continue

                posts.append(
                    RedditPostInfo(
                        subreddit=subreddit_name,
                        submission_id=post.id,
                        title=post.title,
                        author=str(post.author),
                        date=post_time.isoformat(),
                        url=post.url,
                        text=post.selftext,
                        score=post.score,
                    )
                )

            posts.sort(key=lambda x: x.score, reverse=True)
            all_posts.extend(posts[: min(len(posts), self.max_posts_per_subreddit)])

        return RedditPostCollection(posts=all_posts)


class RedditSubmissionCollector:
    """Collect one specified Reddit submission through a submission-only client."""

    def __init__(
        self, reddit_client: RedditSubmissionClient | PrawRedditSubmissionClient
    ) -> None:
        self.reddit_client = reddit_client

    def collect(self, reference: str) -> RedditPostInfo:
        """Collect a post by bare submission ID or direct Reddit post URL."""
        submission_id = _submission_id_from_reference(reference)
        submission = self.reddit_client.fetch_submission(submission_id)
        return self._to_post_info(submission, submission_id)

    @staticmethod
    def _required(submission: dict[str, Any], field: str) -> Any:
        try:
            return submission[field]
        except KeyError as exc:
            raise RuntimeError(f"Reddit submission is missing {field!r}") from exc

    @staticmethod
    def _validate_availability(submission: dict[str, Any]) -> None:
        if submission.get("removed_by_category") is not None:
            raise LookupError("Reddit submission is deleted or removed")
        selftext_marker = submission.get("selftext")
        if isinstance(selftext_marker, str) and selftext_marker in _REMOVED_TEXT:
            raise LookupError("Reddit submission is deleted or removed")

    @classmethod
    def _to_post_info(
        cls, submission: dict[str, Any], expected_id: str
    ) -> RedditPostInfo:
        if not isinstance(submission, dict):
            raise RuntimeError("Reddit submission data must be an object")
        cls._validate_availability(submission)

        source_id = cls._required(submission, "id")
        if not isinstance(source_id, str) or not _SUBMISSION_ID.fullmatch(source_id):
            raise RuntimeError("Reddit submission has an invalid ID")
        if source_id.lower() != expected_id:
            raise LookupError("Reddit returned a different submission")

        subreddit = cls._required(submission, "subreddit")
        title = cls._required(submission, "title")
        author = cls._required(submission, "author")
        selftext = cls._required(submission, "selftext")
        permalink = cls._required(submission, "permalink")
        if not isinstance(subreddit, str) or not subreddit.strip():
            raise RuntimeError("Reddit submission has an invalid subreddit")
        if not isinstance(title, str) or not title.strip():
            raise RuntimeError("Reddit submission has an invalid title")
        if not isinstance(author, str):
            raise RuntimeError("Reddit submission has an invalid author")
        if not isinstance(selftext, str):
            raise RuntimeError("Reddit submission has invalid text")
        if not isinstance(permalink, str) or not permalink.strip():
            raise RuntimeError("Reddit submission has an invalid permalink")

        created_utc = cls._required(submission, "created_utc")
        if (
            isinstance(created_utc, bool)
            or not isinstance(created_utc, (int, float))
            or not math.isfinite(created_utc)
        ):
            raise RuntimeError("Reddit submission has an invalid creation time")
        try:
            post_time = datetime.datetime.fromtimestamp(created_utc, tz=datetime.UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise RuntimeError(
                "Reddit submission has an invalid creation time"
            ) from exc

        score = cls._required(submission, "score")
        if isinstance(score, bool) or not isinstance(score, int):
            raise RuntimeError("Reddit submission has an invalid score")
        canonical_url = cls._canonical_permalink(permalink, expected_id)
        try:
            return RedditPostInfo(
                subreddit=subreddit,
                submission_id=source_id.lower(),
                title=title,
                author=author,
                date=post_time.isoformat(),
                url=canonical_url,
                text=selftext,
                score=score,
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Reddit submission cannot form a post model") from exc

    @staticmethod
    def _canonical_permalink(permalink: str, expected_id: str) -> str:
        try:
            parts = urlsplit(permalink.strip())
            port = parts.port
            if parts.query or parts.fragment:
                raise ValueError("permalink has unexpected query or fragment data")
            if parts.scheme or parts.netloc:
                hostname = (parts.hostname or "").rstrip(".").lower()
                if (
                    parts.scheme.lower() != "https"
                    or not _host_matches(hostname, "reddit.com")
                    or parts.username is not None
                    or parts.password is not None
                    or port not in {None, 443}
                ):
                    raise ValueError("absolute permalink is not a Reddit HTTPS URL")
            path = parts.path
            if not path:
                raise ValueError("permalink has no path")
            canonical_url = f"https://www.reddit.com/{path.lstrip('/')}"
            permalink_id = _submission_id_from_reference(canonical_url)
        except ValueError as exc:
            raise RuntimeError("Reddit submission has an invalid permalink") from exc
        if permalink_id != expected_id:
            raise RuntimeError("Reddit permalink identifies a different submission")
        return canonical_url
