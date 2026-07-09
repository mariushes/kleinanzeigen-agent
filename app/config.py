from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = f"sqlite:///{BASE_DIR / 'data' / 'app.db'}"

    gemini_api_key: str = ""
    llm_provider: str = "gemini"
    llm_model_fast: str = "gemini-3.1-flash-lite"
    # gemini-3-flash-preview's free-tier daily quota is small and shared across all dev
    # work; default to flash-lite everywhere until we're ready to spend that budget
    # deliberately (e.g. a real end-to-end run), overriding per-call when it matters.
    llm_model_quality: str = "gemini-3.1-flash-lite"
    # Only 2.5-flash has google_search grounding quota on the free tier (the 3.x models
    # return 429 for grounded calls regardless of remaining daily quota).
    llm_model_grounded: str = "gemini-2.5-flash"
    # Free-tier Gemini rate limits are per-project (as low as 10 RPM on gemini-3-flash-preview).
    # We throttle client-side to this interval rather than burning through the daily quota on 429 retries.
    llm_min_call_interval_seconds: float = 6.5

    kleinanzeigen_api_base_url: str = "http://127.0.0.1:8000"
    kleinanzeigen_api_timeout_seconds: float = 30.0

    default_max_listings: int = 10
    max_listings_hard_cap: int = 50

    # Knowledge builder cap: research queries per identity per collection run. Each query
    # is one grounded call + one extraction call, so this bounds free-tier quota spend.
    knowledge_default_max_queries: int = 2
    # Auto-collect a first knowledge pass when a search run surfaces an identity with an
    # empty KB, so the very first verdict already has reliability data. Budgeted per run.
    auto_collect_enabled: bool = True
    auto_collect_max_identities_per_run: int = 3
    auto_collect_max_queries: int = 2

    comparables_target_count: int = 8


@lru_cache
def get_settings() -> Settings:
    return Settings()
