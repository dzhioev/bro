# syntax=docker/dockerfile:1
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git curl ca-certificates nodejs npm gosu \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN npm install -g @anthropic-ai/claude-code

RUN useradd -m -u 1000 -s /bin/bash cw \
 && mkdir -p /home/cw/.claude \
 && chown cw:cw /home/cw/.claude

WORKDIR /workspace
ENV UV_PROJECT_ENVIRONMENT=/workspace/.venv-linux \
    CW_IN_CONTAINER=1

COPY entrypoint.sh /usr/local/bin/cw-entrypoint
ENTRYPOINT ["/usr/local/bin/cw-entrypoint"]
