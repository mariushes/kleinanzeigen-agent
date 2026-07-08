"""Gemini implementation of LLMProvider, using the free Gemini Developer API tier.

The free tier is rate-limited per project (as low as 10 requests/minute on the
higher-quality model). We throttle client-side to `llm_min_call_interval_seconds`
rather than repeatedly hitting 429s and burning through the daily quota on retries.
Transient 503 UNAVAILABLE ("model experiencing high demand") is retried with backoff —
distinct from 429 RESOURCE_EXHAUSTED, which is a quota wall and must not be retried.
"""

import threading
import time

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel

from app.config import get_settings
from app.llm.provider import Citation, GroundedResult, LLMCallResult

_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 5.0


class GeminiProvider:
    def __init__(self, api_key: str | None = None):
        settings = get_settings()
        self._client = genai.Client(api_key=api_key or settings.gemini_api_key)
        self._min_interval = settings.llm_min_call_interval_seconds
        self._lock = threading.Lock()
        self._last_call_at: float = 0.0

    def _throttle(self) -> None:
        with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last_call_at)
            if wait > 0:
                time.sleep(wait)
            self._last_call_at = time.monotonic()

    def _generate_with_retry(self, **kwargs):
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            self._throttle()
            try:
                return self._client.models.generate_content(**kwargs)
            except genai_errors.ServerError:
                if attempt == _MAX_ATTEMPTS:
                    raise
                time.sleep(_RETRY_BACKOFF_SECONDS * attempt)

    def structured_completion(
        self,
        *,
        purpose: str,
        system: str,
        user: str,
        response_model: type[BaseModel],
        model: str,
    ) -> LLMCallResult:
        response = self._generate_with_retry(
            model=model,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_schema=response_model,
            ),
        )
        parsed = response_model.model_validate_json(response.text)
        usage = response.usage_metadata
        return LLMCallResult(
            parsed=parsed,
            model=model,
            purpose=purpose,
            input_tokens=usage.prompt_token_count or 0,
            output_tokens=usage.candidates_token_count or 0,
        )

    def grounded_completion(
        self,
        *,
        purpose: str,
        user: str,
        model: str,
    ) -> GroundedResult:
        response = self._generate_with_retry(
            model=model,
            contents=user,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        )

        citations: list[Citation] = []
        candidate = response.candidates[0] if response.candidates else None
        metadata = candidate.grounding_metadata if candidate else None
        if metadata and metadata.grounding_chunks:
            for chunk in metadata.grounding_chunks:
                if chunk.web and chunk.web.uri:
                    citations.append(Citation(title=chunk.web.title or "", url=chunk.web.uri))

        usage = response.usage_metadata
        return GroundedResult(
            text=response.text or "",
            citations=citations,
            model=model,
            purpose=purpose,
            input_tokens=usage.prompt_token_count or 0,
            output_tokens=usage.candidates_token_count or 0,
        )
