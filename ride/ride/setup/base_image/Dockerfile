# syntax=docker/dockerfile:1
# general-purpose base for the framework's container images: python:3.12-slim plus
# the tooling every container carries — jq (JSON debugging) and the uv binary.
# the `bro-base` tag is local-only — never pushed to a registry — (re)built by
# build.sh next to this file before any image that starts FROM it.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends jq \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
