# syntax=docker/dockerfile:1
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git curl ca-certificates nodejs npm gosu procps \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN npm install -g @anthropic-ai/claude-code

RUN useradd -m -u 1000 -s /bin/bash cw \
 && mkdir -p /home/cw/.claude \
 && chown cw:cw /home/cw/.claude

# pre-warm the uv wheel cache from the project's pyproject + uv.lock so the
# entrypoint's `uv sync` against the bind-mounted /workspace doesn't have to
# re-download wheels. /opt is on the image overlay (different fs from the
# bind mount), so hardlinks won't work — UV_LINK_MODE=copy makes runtime
# `uv sync` skip the failed hardlink probe and copy straight from cache.
# Cache is chmod'd world-writable because the entrypoint usermod's `cw` to
# match the host uid at runtime, so build-time ownership is irrelevant.
ENV UV_CACHE_DIR=/opt/uv-cache
ENV UV_LINK_MODE=copy
COPY --from=proj pyproject.toml uv.lock /tmp/uv-warm/
RUN cd /tmp/uv-warm \
 && uv sync --no-install-project --frozen --all-groups \
 && rm -rf /tmp/uv-warm \
 && chmod -R a+rwX /opt/uv-cache

WORKDIR /workspace
ENV CW_IN_CONTAINER=1

COPY entrypoint.sh /usr/local/bin/cw-entrypoint
ENTRYPOINT ["/usr/local/bin/cw-entrypoint"]
