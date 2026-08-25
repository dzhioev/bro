# syntax=docker/dockerfile:1
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY --chmod=0755 install_launcher.sh /usr/local/bin/install-launcher

RUN useradd --create-home --shell /bin/bash server

WORKDIR /app
ENV PYTHONPATH=/app
