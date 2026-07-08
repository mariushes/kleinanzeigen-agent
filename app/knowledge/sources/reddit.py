"""Reddit knowledge source via PRAW in read-only mode. Currently dormant.

Reddit blocks anonymous JSON access (403 without OAuth), so this needs a registered
Reddit API app: create one at https://www.reddit.com/prefs/apps (type "script"), then
set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET in .env. Read-only mode needs no
username/password. Until credentials exist, `WebSearchSource` is the active source;
this stays wired to the same protocol so it can be enabled by just adding credentials.
"""

import praw

from app.config import get_settings
from app.knowledge.sources.base import ResearchDocument

_MAX_POSTS_PER_THREAD = 20


class RedditSource:
    name = "reddit"

    def __init__(self, reddit: praw.Reddit | None = None):
        if reddit is None:
            settings = get_settings()
            if not settings.reddit_client_id or not settings.reddit_client_secret:
                raise RuntimeError(
                    "Reddit credentials missing: set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET "
                    "in .env (create a 'script' app at https://www.reddit.com/prefs/apps)."
                )
            reddit = praw.Reddit(
                client_id=settings.reddit_client_id,
                client_secret=settings.reddit_client_secret,
                user_agent=settings.reddit_user_agent,
            )
            reddit.read_only = True
        self._reddit = reddit

    def research(self, query: str, max_documents: int) -> list[ResearchDocument]:
        documents: list[ResearchDocument] = []
        for submission in self._reddit.subreddit("all").search(query, limit=max_documents):
            posts: list[str] = []
            if submission.selftext:
                posts.append(submission.selftext)

            submission.comments.replace_more(limit=0)  # top-level only, no extra API calls
            for comment in submission.comments[: max(0, _MAX_POSTS_PER_THREAD - len(posts))]:
                if comment.body:
                    posts.append(comment.body)

            documents.append(
                ResearchDocument(
                    source=self.name,
                    title=submission.title,
                    url=f"https://www.reddit.com{submission.permalink}",
                    text="\n\n---\n\n".join(posts),
                )
            )
        return documents
