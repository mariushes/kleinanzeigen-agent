"""KnowledgeSource protocol: where raw research material comes from.

Each source turns a research query (e.g. "VW T5 2.0 TDI 180 PS common problems") into
`ResearchDocument`s — a chunk of text plus where it came from. For a forum source
that's one thread (url = the thread, no separate citations); for the grounded
web-search source it's one synthesized answer whose `citations` list the web pages it
was grounded on. Extraction into structured `KnowledgeEntry` rows happens downstream
in the knowledge builder, identically for every source.
"""

from typing import Protocol

from pydantic import BaseModel


class SourceCitation(BaseModel):
    title: str
    url: str


class ResearchDocument(BaseModel):
    source: str
    title: str
    url: str | None = None
    text: str
    citations: list[SourceCitation] = []


class KnowledgeSource(Protocol):
    name: str

    def research(self, query: str, max_documents: int) -> list[ResearchDocument]: ...
