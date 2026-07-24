import pytest

from app.config import Settings
from app.llm import factory


def _patch_settings(monkeypatch, **overrides):
    settings = Settings(_env_file=None, **overrides)
    monkeypatch.setattr(factory, "get_settings", lambda: settings)
    return settings


def test_structured_provider_gemini(monkeypatch):
    _patch_settings(monkeypatch, llm_structured_provider="gemini")
    from app.llm.gemini import GeminiProvider

    assert isinstance(factory.get_structured_provider(), GeminiProvider)


def test_structured_provider_bedrock(monkeypatch):
    _patch_settings(monkeypatch, llm_structured_provider="bedrock")
    from app.llm.bedrock import BedrockProvider

    assert isinstance(factory.get_structured_provider(), BedrockProvider)


def test_unknown_structured_provider_raises(monkeypatch):
    _patch_settings(monkeypatch, llm_structured_provider="nope")
    with pytest.raises(ValueError, match="unknown LLM provider"):
        factory.get_structured_provider()


def test_grounded_provider_is_gemini(monkeypatch):
    _patch_settings(monkeypatch, llm_grounded_provider="gemini")
    from app.llm.gemini import GeminiProvider

    assert isinstance(factory.get_grounded_provider(), GeminiProvider)


def test_grounded_provider_rejects_bedrock(monkeypatch):
    # Bedrock can't ground; the factory must refuse rather than silently mis-route.
    _patch_settings(monkeypatch, llm_grounded_provider="bedrock")
    with pytest.raises(ValueError, match="only Gemini supports grounding"):
        factory.get_grounded_provider()


def test_model_id_follows_structured_provider():
    gemini = Settings(_env_file=None, llm_structured_provider="gemini")
    bedrock = Settings(_env_file=None, llm_structured_provider="bedrock")
    assert gemini.llm_model_quality == gemini.gemini_model_quality
    assert bedrock.llm_model_quality == bedrock.bedrock_model_quality
    # grounded model never changes with the structured provider
    assert gemini.llm_model_grounded == bedrock.llm_model_grounded
