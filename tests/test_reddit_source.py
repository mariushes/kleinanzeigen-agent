from types import SimpleNamespace

import pytest

from app.knowledge.sources.reddit import RedditSource


class FakeComments(list):
    def replace_more(self, limit):
        self.replace_more_called_with = limit


def make_submission(title, permalink, selftext, comments):
    fake_comments = FakeComments(SimpleNamespace(body=c) for c in comments)
    return SimpleNamespace(title=title, permalink=permalink, selftext=selftext, comments=fake_comments)


class FakeReddit:
    def __init__(self, submissions):
        self._submissions = submissions
        self.search_calls = []

    def subreddit(self, name):
        outer = self

        class _Sub:
            def search(self, query, limit):
                outer.search_calls.append((name, query, limit))
                return outer._submissions[:limit]

        return _Sub()


def test_research_returns_capped_documents_with_thread_urls():
    reddit = FakeReddit([
        make_submission(
            "T5 buying advice", "/r/vandwellers/comments/abc/t5/",
            "Looking at a 2012 T5...",
            ["Avoid the 180hp biturbo", "2.0 TDI 140 is solid"],
        ),
        make_submission("Another thread", "/r/vw/comments/def/x/", "", ["only comment"]),
        make_submission("Third thread", "/r/vw/comments/ghi/y/", "body", []),
    ])
    source = RedditSource(reddit=reddit)

    documents = source.research("VW T5 problems", max_documents=2)

    assert len(documents) == 2
    first = documents[0]
    assert first.source == "reddit"
    assert first.title == "T5 buying advice"
    assert first.url == "https://www.reddit.com/r/vandwellers/comments/abc/t5/"
    assert "Looking at a 2012 T5..." in first.text
    assert "Avoid the 180hp biturbo" in first.text
    assert reddit.search_calls == [("all", "VW T5 problems", 2)]


def test_research_skips_empty_selftext():
    reddit = FakeReddit([make_submission("t", "/r/x/1/", "", ["a comment"])])
    source = RedditSource(reddit=reddit)

    documents = source.research("q", max_documents=5)

    assert documents[0].text == "a comment"


def test_missing_credentials_raises_helpful_error(monkeypatch):
    monkeypatch.setattr("app.knowledge.sources.reddit.get_settings", lambda: SimpleNamespace(
        reddit_client_id="", reddit_client_secret="", reddit_user_agent="x"
    ))

    with pytest.raises(RuntimeError, match="reddit.com/prefs/apps"):
        RedditSource()
