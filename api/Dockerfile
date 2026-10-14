# syntax=docker/dockerfile:1.7
# Multi-stage build: base -> deps -> build -> runtime (production) / dev (tests, lint, eval).
# Base images are pinned by digest (reproducible builds); Renovate keeps the pins up to date.

ARG PYTHON_IMAGE=python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.0@sha256:cdc6093146eb3ff6a40107b38f008b789e050e77ad87865e381d9917da55a168

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
COPY --from=uv /uv /uvx /bin/
WORKDIR /app

# Swagger UI / ReDoc assets, verified by checksum - the docs work without internet access.
FROM ${PYTHON_IMAGE} AS docs-assets
ADD --checksum=sha256:6b3bfcaff3fc82c0e6992ca4b370c4f22970dec35fff60d229302b3ee7688715 \
    https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.33.2/swagger-ui-bundle.js /assets/swagger-ui-bundle.js
ADD --checksum=sha256:1ac324f7dcd27e4b9386b4bd6421271ec147e922a22c05ba24b11515e9aa6321 \
    https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.33.2/swagger-ui.css /assets/swagger-ui.css
ADD --checksum=sha256:3ed612f41e050ca5e7000cad6f1cbe7e7da39f65fca99c02e99e6591056e5837 \
    https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.33.2/favicon-32x32.png /assets/favicon-32x32.png
ADD --checksum=sha256:dcaf76612bc4a3fbcc923a8966dee2f6146a5f32e5ce1b6f02dd60cbbf89500b \
    https://cdn.jsdelivr.net/npm/redoc@2.5.4/bundles/redoc.standalone.js /assets/redoc.standalone.js

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

# Production image: virtualenv + static docs assets, non-root user, no build tools.
FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"
# pip is not needed at runtime (the venv is complete); removing it also removes its vendored copies
# of urllib3/msgpack/setuptools, which vulnerability scanners report against the image.
RUN python -m pip uninstall --yes --quiet pip \
    && groupadd --system app && useradd --system --gid app --home-dir /app --no-create-home app \
    && mkdir -p /app/data && chown app:app /app/data
WORKDIR /app
COPY --from=build --chown=app:app /app/.venv /app/.venv
COPY --from=docs-assets --chown=app:app /assets /app/static
USER app
EXPOSE 8000
VOLUME ["/app/data"]
CMD ["uvicorn", "ai_router.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-access-log"]

# Developer image: dev dependencies + tests. UID/GID come from the Makefile (`id -u`), so files
# written to mounted directories (eval-results, formatted sources) belong to the host user.
FROM build AS dev
ARG DEV_UID=1000
ARG DEV_GID=1000
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen
COPY tests ./tests
COPY --from=docs-assets /assets /app/static
RUN (getent group "${DEV_GID}" >/dev/null || groupadd --gid "${DEV_GID}" dev) \
    && (getent passwd "${DEV_UID}" >/dev/null \
        || useradd --uid "${DEV_UID}" --gid "${DEV_GID}" --home-dir /home/dev --no-create-home dev) \
    && mkdir -p /app/data /home/dev \
    && chown "${DEV_UID}:${DEV_GID}" /app /app/data /home/dev
ENV PATH="/app/.venv/bin:$PATH" \
    HOME=/home/dev \
    RUFF_CACHE_DIR=/tmp/.ruff_cache \
    MYPY_CACHE_DIR=/tmp/.mypy_cache \
    COVERAGE_FILE=/tmp/.coverage \
    DATABASE_PATH=/app/data/dev.sqlite3
USER ${DEV_UID}:${DEV_GID}
CMD ["pytest", "-q"]
