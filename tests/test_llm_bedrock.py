import pytest
from pydantic import BaseModel

from app.llm.bedrock import _RESPONSE_TOOL_NAME, BedrockProvider


class Greeting(BaseModel):
    message: str
    enthusiasm: int


class FakeBedrockClient:
    """Stands in for boto3's bedrock-runtime client. Records the last converse() kwargs
    and returns a canned Converse-shaped response."""

    def __init__(self, tool_input: dict | None, input_tokens=10, output_tokens=5, stop_reason="tool_use"):
        self._tool_input = tool_input
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self._stop_reason = stop_reason
        self.last_converse_kwargs: dict | None = None

    def converse(self, **kwargs):
        self.last_converse_kwargs = kwargs
        content = []
        if self._tool_input is not None:
            content = [{"toolUse": {"name": _RESPONSE_TOOL_NAME, "input": self._tool_input}}]
        return {
            "output": {"message": {"role": "assistant", "content": content}},
            "stopReason": self._stop_reason,
            "usage": {"inputTokens": self._input_tokens, "outputTokens": self._output_tokens},
        }


def make_provider(tool_input, **kwargs) -> BedrockProvider:
    return BedrockProvider(client=FakeBedrockClient(tool_input, **kwargs))


def test_structured_completion_parses_tool_input_and_reports_usage():
    provider = make_provider({"message": "hi", "enthusiasm": 9}, input_tokens=42, output_tokens=7)

    result = provider.structured_completion(
        purpose="test_purpose",
        system="You are a greeter.",
        user="Greet the user.",
        response_model=Greeting,
        model="anthropic.claude-x",
    )

    assert result.parsed == Greeting(message="hi", enthusiasm=9)
    assert result.model == "anthropic.claude-x"
    assert result.purpose == "test_purpose"
    assert result.input_tokens == 42
    assert result.output_tokens == 7


def test_structured_completion_forces_the_schema_tool():
    provider = make_provider({"message": "hi", "enthusiasm": 9})

    provider.structured_completion(
        purpose="p", system="You are a greeter.", user="Greet.", response_model=Greeting,
        model="anthropic.claude-x",
    )

    kwargs = provider._client.last_converse_kwargs
    assert kwargs["modelId"] == "anthropic.claude-x"
    assert kwargs["system"] == [{"text": "You are a greeter."}]
    assert kwargs["messages"] == [{"role": "user", "content": [{"text": "Greet."}]}]
    # the tool exposes the pydantic schema, and the model is forced to call it
    tool = kwargs["toolConfig"]["tools"][0]["toolSpec"]
    assert tool["name"] == _RESPONSE_TOOL_NAME
    assert tool["inputSchema"]["json"] == Greeting.model_json_schema()
    assert kwargs["toolConfig"]["toolChoice"] == {"tool": {"name": _RESPONSE_TOOL_NAME}}


def test_structured_completion_raises_when_no_tool_call_returned():
    provider = make_provider(None, stop_reason="max_tokens")  # model didn't call the tool

    with pytest.raises(ValueError, match="no 'emit_structured_response' tool call"):
        provider.structured_completion(
            purpose="p", system="s", user="u", response_model=Greeting, model="m",
        )


def test_grounded_completion_not_supported():
    provider = make_provider({"message": "x", "enthusiasm": 1})

    with pytest.raises(NotImplementedError, match="grounded"):
        provider.grounded_completion(purpose="p", user="u", model="m")
