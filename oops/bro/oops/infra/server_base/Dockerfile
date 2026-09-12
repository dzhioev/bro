# syntax=docker/dockerfile:1
ARG UV_VERSION
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv-dist
FROM python:3.12-slim

COPY --from=uv-dist /uv /usr/local/bin/uv
COPY --chmod=0755 install_launcher.sh /usr/local/bin/install-launcher

RUN useradd --create-home --shell /bin/bash server

WORKDIR /app
ENV PYTHONPATH=/app
