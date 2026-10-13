# syntax=docker/dockerfile:1
# Multi-stage build: base -> deps -> build -> runtime (production) / dev (tests, lint, eval).

FROM python:3.13-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /uvx /bin/
WORKDIR /app

# Dependencies only (cached until pyproject.toml / uv.lock change).
FROM base AS deps
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# The application package itself (installed non-editable into the venv).
FROM deps AS build
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# Production image: only the virtualenv, non-root user, no build tools.
FROM python:3.13-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"
RUN groupadd --system app && useradd --system --gid app --home-dir /app --no-create-home app
WORKDIR /app
COPY --from=build --chown=app:app /app/.venv /app/.venv
USER app
EXPOSE 8000
CMD ["uvicorn", "ai_router.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-access-log"]

# Developer image: dev dependencies + tests; runs as UID/GID 1000 so files written to mounted
# directories (eval-results, formatted sources) stay owned by the host user.
FROM build AS dev
ARG DEV_UID=1000
ARG DEV_GID=1000
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen
COPY tests ./tests
RUN groupadd --gid "${DEV_GID}" dev \
    && useradd --uid "${DEV_UID}" --gid dev --home-dir /home/dev --create-home dev \
    && chown dev:dev /app
ENV PATH="/app/.venv/bin:$PATH" \
    HOME=/home/dev \
    RUFF_CACHE_DIR=/tmp/.ruff_cache \
    MYPY_CACHE_DIR=/tmp/.mypy_cache \
    COVERAGE_FILE=/tmp/.coverage
USER dev
CMD ["pytest", "-q"]
