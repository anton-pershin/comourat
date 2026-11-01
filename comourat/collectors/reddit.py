"""Reddit post collection logic via praw."""

from __future__ import annotations

import datetime

import praw

from comourat.models import RedditPostCollection, RedditPostInfo


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
