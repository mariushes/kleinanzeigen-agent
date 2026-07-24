"""AWS Bedrock implementation of the structured half of LLMProvider.

Bedrock has no direct equivalent of Gemini's `response_schema`, so structured output is
obtained via the Converse API's **tool use**: we expose a single tool whose input schema
is the Pydantic model's JSON schema and force the model to call it (`toolChoice`), then
validate the tool-call input back into the model. This is the standard way to get
schema-constrained JSON from Claude models on Bedrock.

Grounding is deliberately NOT implemented here: in this project's setup, grounded
web-search calls stay on Gemini (Bedrock has no built-in web search), so
`grounded_completion` raises. See `app/knowledge/sources/web_search.py`.
"""

from typing import TYPE_CHECKING

from pydantic import BaseModel

from app.config import get_settings
from app.llm.provider import GroundedResult, LLMCallResult

if TYPE_CHECKING:  # boto3 client is created lazily; keep import light for callers
    pass

# The model must emit its answer by "calling" this tool; its input is the JSON we want.
_RESPONSE_TOOL_NAME = "emit_structured_response"


class BedrockProvider:
    def __init__(self, client=None, region: str | None = None):
        settings = get_settings()
        if client is not None:
            self._client = client
        else:
            import boto3

            self._client = boto3.client(
                "bedrock-runtime",
                region_name=region or settings.bedrock_region,
            )

    def structured_completion(
        self,
        *,
        purpose: str,
        system: str,
        user: str,
        response_model: type[BaseModel],
        model: str,
    ) -> LLMCallResult:
        tool_spec = {
            "toolSpec": {
                "name": _RESPONSE_TOOL_NAME,
                "description": (
                    "Return the answer as structured JSON matching the required schema. "
                    "Call this tool exactly once with the full result."
                ),
                "inputSchema": {"json": response_model.model_json_schema()},
            }
        }

        response = self._client.converse(
            modelId=model,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            toolConfig={
                "tools": [tool_spec],
                # Force the model to answer via the tool, so we always get schema-shaped JSON.
                "toolChoice": {"tool": {"name": _RESPONSE_TOOL_NAME}},
            },
        )

        tool_input = _extract_tool_input(response, _RESPONSE_TOOL_NAME)
        parsed = response_model.model_validate(tool_input)

        usage = response.get("usage", {})
        return LLMCallResult(
            parsed=parsed,
            model=model,
            purpose=purpose,
            input_tokens=usage.get("inputTokens", 0),
            output_tokens=usage.get("outputTokens", 0),
        )

    def grounded_completion(
        self, *, purpose: str, user: str, model: str
    ) -> GroundedResult:
        raise NotImplementedError(
            "BedrockProvider does not support grounded search; grounded knowledge "
            "research stays on Gemini (see app/knowledge/sources/web_search.py)."
        )


def _extract_tool_input(response: dict, tool_name: str) -> dict:
    """Pull the forced tool-call's input out of a Converse response, or fail clearly."""
    content = response.get("output", {}).get("message", {}).get("content", [])
    for block in content:
        tool_use = block.get("toolUse")
        if tool_use and tool_use.get("name") == tool_name:
            return tool_use.get("input", {})
    stop_reason = response.get("stopReason")
    raise ValueError(
        f"Bedrock response contained no '{tool_name}' tool call "
        f"(stopReason={stop_reason!r}); cannot extract structured output."
    )
