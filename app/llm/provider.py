"""Provider-agnostic structured LLM call interface.

Gemini is the only implementation today (`app/llm/gemini.py`), but every call site
depends on this `Protocol`, not on Gemini directly, so another provider can be added
later without touching callers.
"""

from dataclasses import dataclass, field
from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


@dataclass
class LLMCallResult:
    parsed: BaseModel
    model: str
    purpose: str
    input_tokens: int
    output_tokens: int


@dataclass
class Citation:
    title: str
    url: str


@dataclass
class GroundedResult:
    text: str
    citations: list[Citation] = field(default_factory=list)
    model: str = ""
    purpose: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


class LLMProvider(Protocol):
    def structured_completion(
        self,
        *,
        purpose: str,
        system: str,
        user: str,
        response_model: type[T],
        model: str,
    ) -> LLMCallResult: ...

    def grounded_completion(
        self,
        *,
        purpose: str,
        user: str,
        model: str,
    ) -> GroundedResult:
        """Free-form completion backed by the provider's web-search grounding.

        Returns the synthesized answer plus source citations. Cannot return structured
        output — search tools and JSON-schema constraints are mutually exclusive on
        Gemini, so extraction into typed records is always a separate call.
        """
        ...
