"""Web-search knowledge source via Gemini's google_search grounding.

The only web-backed source that works without extra credentials: Reddit's JSON API
returns 403 without an OAuth app, DuckDuckGo's HTML endpoint anti-bot-challenges
scripted clients, and motor-talk.de renders search results client-side. Grounding has
free-tier quota only on gemini-2.5-flash (`llm_model_grounded` in config) — the 3.x
models reject grounded calls with 429 regardless of remaining daily quota.

One grounded call per query → one ResearchDocument whose citations are the web pages
Gemini grounded the answer on.
"""

from sqlalchemy.orm import Session

from app.config import get_settings
from app.knowledge.sources.base import ResearchDocument, SourceCitation
from app.llm.logging import record_llm_call
from app.llm.provider import LLMProvider


class WebSearchSource:
    name = "web_search"

    def __init__(self, provider: LLMProvider, db: Session):
        self._provider = provider
        self._db = db

    def research(self, query: str, max_documents: int = 1) -> list[ResearchDocument]:
        settings = get_settings()
        result = self._provider.grounded_completion(
            purpose="knowledge_research",
            user=(
                f"Research the following topic about a used vehicle, for a prospective buyer: {query}\n\n"
                "Summarize what owners, mechanics, and buying guides report. Be specific: name components, "
                "failure modes, affected years/engine variants, typical costs, and mileage figures where sources give them. "
                "If sources disagree or evidence is thin, say so."
            ),
            model=settings.llm_model_grounded,
        )
        record_llm_call(self._db, result, related_entity=f"knowledge_query:{query[:80]}")

        if not result.text.strip():
            return []

        return [
            ResearchDocument(
                source=self.name,
                title=query,
                url=None,
                text=result.text,
                citations=[SourceCitation(title=c.title, url=c.url) for c in result.citations],
            )
        ]
