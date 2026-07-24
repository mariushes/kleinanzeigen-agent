"""Provider selection for the Gemini/Bedrock split.

Two independent choices (see `app/config.py`):
- `llm_structured_provider` — who handles the 5 structured/analysis calls.
- `llm_grounded_provider` — who handles the 1 grounded web-research call. Only Gemini can
  ground, so this stays "gemini" even when structured runs on Bedrock.

Callers get a provider via these factories rather than instantiating a concrete class, so
the choice lives in config, not at every call site.
"""

from app.config import get_settings
from app.llm.provider import LLMProvider


def _build(provider_name: str) -> LLMProvider:
    if provider_name == "gemini":
        from app.llm.gemini import GeminiProvider

        return GeminiProvider()
    if provider_name == "bedrock":
        from app.llm.bedrock import BedrockProvider

        return BedrockProvider()
    raise ValueError(f"unknown LLM provider: {provider_name!r} (expected 'gemini' or 'bedrock')")


def get_structured_provider() -> LLMProvider:
    """Provider for identity/condition/criteria/judgment/extraction calls."""
    return _build(get_settings().llm_structured_provider)


def get_grounded_provider() -> LLMProvider:
    """Provider for grounded web research. Bedrock can't ground, so guard against it."""
    settings = get_settings()
    if settings.llm_grounded_provider != "gemini":
        raise ValueError(
            f"llm_grounded_provider must be 'gemini' (only Gemini supports grounding); "
            f"got {settings.llm_grounded_provider!r}"
        )
    return _build("gemini")
