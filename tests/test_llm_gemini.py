from types import SimpleNamespace

from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, LlmCall
from app.llm.gemini import GeminiProvider
from app.llm.logging import record_llm_call


class Greeting(BaseModel):
    message: str
    enthusiasm: int


class FakeModels:
    def __init__(self, response_json: str, prompt_tokens: int, output_tokens: int):
        self._response_json = response_json
        self._prompt_tokens = prompt_tokens
        self._output_tokens = output_tokens
        self.last_call_kwargs: dict | None = None

    def generate_content(self, **kwargs):
        self.last_call_kwargs = kwargs
        return SimpleNamespace(
            text=self._response_json,
            usage_metadata=SimpleNamespace(
                prompt_token_count=self._prompt_tokens,
                candidates_token_count=self._output_tokens,
            ),
        )


def make_provider(response_json: str, prompt_tokens=10, output_tokens=5) -> GeminiProvider:
    provider = GeminiProvider(api_key="test-key")
    provider._client = SimpleNamespace(models=FakeModels(response_json, prompt_tokens, output_tokens))
    provider._min_interval = 0  # don't slow down tests
    return provider


def test_structured_completion_parses_response_and_reports_usage():
    provider = make_provider('{"message": "hi", "enthusiasm": 9}', prompt_tokens=42, output_tokens=7)

    result = provider.structured_completion(
        purpose="test_purpose",
        system="You are a greeter.",
        user="Greet the user.",
        response_model=Greeting,
        model="gemini-3.1-flash-lite",
    )

    assert result.parsed == Greeting(message="hi", enthusiasm=9)
    assert result.model == "gemini-3.1-flash-lite"
    assert result.purpose == "test_purpose"
    assert result.input_tokens == 42
    assert result.output_tokens == 7


def test_structured_completion_passes_schema_and_system_instruction():
    provider = make_provider('{"message": "hi", "enthusiasm": 9}')

    provider.structured_completion(
        purpose="test_purpose",
        system="You are a greeter.",
        user="Greet the user.",
        response_model=Greeting,
        model="gemini-3.1-flash-lite",
    )

    call_kwargs = provider._client.models.last_call_kwargs
    assert call_kwargs["model"] == "gemini-3.1-flash-lite"
    assert call_kwargs["contents"] == "Greet the user."
    assert call_kwargs["config"].response_schema is Greeting
    assert call_kwargs["config"].system_instruction == "You are a greeter."


def test_record_llm_call_persists_row():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    provider = make_provider('{"message": "hi", "enthusiasm": 9}', prompt_tokens=42, output_tokens=7)
    result = provider.structured_completion(
        purpose="test_purpose", system="sys", user="usr", response_model=Greeting, model="m",
    )

    record_llm_call(db, result, related_entity="listing:123")

    row = db.query(LlmCall).one()
    assert row.purpose == "test_purpose"
    assert row.input_tokens == 42
    assert row.output_tokens == 7
    assert row.related_entity == "listing:123"
