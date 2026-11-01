"""comourat collector tests: Reddit/HN/arXiv clock injection and cutoff (AC2).

The review on PR #16 flagged the direct system-clock reads; these tests pin
the injectable-clock behavior and the time-window boundary.
"""

import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from comourat.collectors.reddit import RedditPostCollectorCore
from comourat.models import RedditPostCollection


class TestRedditClockInjection:
    def _core(self, now_fn):
        client = MagicMock()
        client.subreddit.return_value.new.return_value = iter([])
        return (
            RedditPostCollectorCore(
                reddit_client=client,
                subreddits=["ml"],
                time_window={"hours": 24},
                now_fn=now_fn,
            ),
            client,
        )

    def test_default_clock_is_system(self):
        client = MagicMock()
        client.subreddit.return_value.new.return_value = iter([])
        core = RedditPostCollectorCore(
            reddit_client=client,
            subreddits=["ml"],
            time_window={"hours": 24},
        )
        assert core.now_fn().tzinfo == datetime.UTC
        assert (
            abs((core.now_fn() - datetime.datetime.now(datetime.UTC)).total_seconds())
            < 1.0
        )

    def test_injected_clock_sets_cutoff_boundary(self):
        now = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)
        cutoff = now - datetime.timedelta(hours=24)

        inside = SimpleNamespace(
            created_utc=cutoff.timestamp(),  # exactly at the boundary: kept
            id="keep",
            title="boundary post",
            author="a",
            url="https://e.com",
            selftext="text",
            score=1,
        )
        outside = SimpleNamespace(
            created_utc=(cutoff - datetime.timedelta(seconds=1)).timestamp(),
            id="drop",
            title="older post",
            author="a",
            url="https://e.com",
            selftext="text",
            score=1,
        )

        client = MagicMock()
        client.subreddit.return_value.new.return_value = iter([inside, outside])
        core = RedditPostCollectorCore(
            reddit_client=client,
            subreddits=["ml"],
            time_window={"hours": 24},
            now_fn=lambda: now,
        )
        result = core.collect()
        assert isinstance(result, RedditPostCollection)
        assert [p.submission_id for p in result.posts] == ["keep"]
