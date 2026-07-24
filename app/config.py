from functools import cached_property, lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Two ways to specify the database, in priority order (see `database_url` below):
    #  1. `DATABASE_URL` — a full SQLAlchemy URL. Used for local SQLite (the default) and
    #     the local docker-compose `db` container, where there are no special-char secrets.
    #  2. `DB_HOST` (+ DB_USER/DB_PASSWORD/DB_NAME/DB_SSLMODE/DB_SSLROOTCERT) — discrete
    #     parts, assembled via SQLAlchemy's `URL.create`, which encodes the password
    #     itself. This is the RDS path: the password comes straight from Secrets Manager
    #     as a *raw* string and is never hand-URL-encoded in a shell, so there's exactly
    #     one place (here) that knows how to build a connection.
    # Reads the `DATABASE_URL` env var (kept as an alias so existing deploys/tests that set
    # DATABASE_URL keep working); the computed `database_url` property is what callers use.
    database_url_override: str | None = Field(default=None, validation_alias="DATABASE_URL")

    db_host: str | None = None
    db_port: int = 5432
    db_user: str = "postgres"
    db_password: str = ""
    db_name: str = "postgres"
    db_driver: str = "postgresql+psycopg"
    db_sslmode: str | None = None          # e.g. "verify-full" for RDS
    db_sslrootcert: str | None = None       # e.g. "/certs/global-bundle.pem"

    @cached_property
    def database_url(self) -> str:
        """The effective SQLAlchemy URL. A full DATABASE_URL wins; otherwise assemble from
        DB_* parts (URL.create handles password escaping). Falls back to local SQLite."""
        if self.database_url_override:
            return self.database_url_override
        if self.db_host:
            query = {}
            if self.db_sslmode:
                query["sslmode"] = self.db_sslmode
            if self.db_sslrootcert:
                query["sslrootcert"] = self.db_sslrootcert
            return URL.create(
                self.db_driver,
                username=self.db_user,
                password=self.db_password,  # raw; URL.create percent-encodes it
                host=self.db_host,
                port=self.db_port,
                database=self.db_name,
                query=query,
            ).render_as_string(hide_password=False)
        return f"sqlite:///{BASE_DIR / 'data' / 'app.db'}"

    gemini_api_key: str = ""

    # --- Provider split ---------------------------------------------------------------
    # The 5 structured/analysis calls (identity, condition, criteria, judgment, extraction)
    # and the 1 grounded web-research call can use DIFFERENT providers. Grounded search has
    # no Bedrock equivalent, so it stays on Gemini even when structured runs on Bedrock.
    llm_structured_provider: str = "gemini"   # "gemini" | "bedrock"
    llm_grounded_provider: str = "gemini"     # "gemini" only (bedrock can't ground)

    # AWS Bedrock — auth is via the instance IAM role (no key), only the region + model ids.
    bedrock_region: str = "eu-central-1"
    bedrock_model_fast: str = "qwen.qwen3-235b-a22b-2507-v1:0"
    bedrock_model_quality: str = "qwen.qwen3-235b-a22b-2507-v1:0"

    # Gemini model ids.
    gemini_model_fast: str = "gemini-3.1-flash-lite"
    # gemini-3-flash-preview's free-tier daily quota is small and shared across all dev
    # work; default to flash-lite everywhere until we're ready to spend that budget
    # deliberately (e.g. a real end-to-end run), overriding per-call when it matters.
    gemini_model_quality: str = "gemini-3.1-flash-lite"
    # Only 2.5-flash has google_search grounding quota on the free tier (the 3.x models
    # return 429 for grounded calls regardless of remaining daily quota).
    llm_model_grounded: str = "gemini-2.5-flash"

    @property
    def llm_model_fast(self) -> str:
        """The 'fast' model id for the selected STRUCTURED provider. Call sites pass this
        to structured_completion, so it must match whichever provider will handle the call."""
        return self.bedrock_model_fast if self.llm_structured_provider == "bedrock" else self.gemini_model_fast

    @property
    def llm_model_quality(self) -> str:
        """The 'quality' model id for the selected STRUCTURED provider (see llm_model_fast)."""
        return self.bedrock_model_quality if self.llm_structured_provider == "bedrock" else self.gemini_model_quality
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
