# syntax=docker/dockerfile:1

# Pinned to Debian trixie: its own repos ship postgresql-client-17, matching the Postgres 17
# server (Aurora). pg_dump refuses to dump a server newer than itself.
ARG PYTHON_IMAGE=python:3.14-slim-trixie

# --- Build stage: resolve dependencies into a venv from the lockfile ----------------------
FROM ${PYTHON_IMAGE} AS build

COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/srv/.venv

WORKDIR /srv
# Only the lockfile and pyproject feed this layer, so code changes reuse the cached deps.
# --locked fails the build if uv.lock is out of date with pyproject.toml.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev

# --- Runtime stage: no uv, no build tools ------------------------------------------------
FROM ${PYTHON_IMAGE}

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-17 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home app

WORKDIR /srv
COPY --from=build /srv/.venv /srv/.venv
COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app

ENV PATH="/srv/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# Last, so a new commit only rebuilds these tiny layers.
ARG GIT_SHA=unknown
ARG GIT_BRANCH=unknown
ENV GIT_SHA=${GIT_SHA} \
    GIT_BRANCH=${GIT_BRANCH}

USER 10001:10001
EXPOSE 8000

# ECS picks the command per container: ["migrate"], ["seed"], ["copy-db"], or the default.
ENTRYPOINT ["python", "-m", "app"]
CMD ["serve"]
