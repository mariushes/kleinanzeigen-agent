# Application image (FastAPI + analysis pipeline). The kleinanzeigen scraping sidecar
# is a SEPARATE image built from vendor/ebay-kleinanzeigen-api/ — see docker-compose.yml.
# This image does not need the vendored submodule or a browser.
FROM python:3.12-slim-bookworm

# uv: fast, reproducible installs from the committed uv.lock.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

# Install deps first (cached layer) from lockfile only — no source yet.
# --no-install-project so app code changes don't bust the dependency layer.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# App source. vendor/ is excluded via .dockerignore (built as its own image).
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# entrypoint runs migrations, then execs the server.
COPY docker/app-entrypoint.sh /usr/local/bin/app-entrypoint.sh
RUN chmod +x /usr/local/bin/app-entrypoint.sh

EXPOSE 8080
ENTRYPOINT ["app-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
