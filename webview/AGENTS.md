# Webview worker

The `bro-webview` member publishes the registered `webview` worker type and its container-side daemon in the `bro.webview` namespace.
It depends on core for broker and artifact contracts and on the MCP SDK for the Playwright stdio client.

## Development

Formatting, linting, typing, tests, and packaging use the root repository gate.
Run `sync-scripts --project webview` after adding or removing a CLI, and build the wheel with `uv build --package bro-webview`.

## Components

- `bro/webview/worker.py` — the dependency-light registered type, launch validation, and packaged container specification
- `bro/webview/container/Dockerfile` — the runtime-derived image with Xvfb, noVNC, Chromium, and the pinned Playwright MCP
- `bro/webview/serve.py` — the container daemon and Playwright MCP command loop
- `bro/webview/cli.py` (`webview`) — the session command; this stage carries the container-side `serve` verb

## Mission wire

A webview mission has talk `{owner.question, worker.say}` and carries JSON objects as its chat payloads.
An owner question is either one Playwright MCP call as `{tool, arguments?}` or `{webview: tools|close}`.
The daemon answers a tool call with `{text, files}`, where each file names its workspace-relative path and artifact ref or mint error, and answers a malformed or failed call with `{error}`.
An oversized reply names the artifact holding its text or its whole object instead.
The `ready` say is `{event: ready, vnc}`, and a close ends the mission with the number of successful tool calls.

The daemon collects files new or changed anywhere under `/workspace` around each tool call, except the read-only artifact view, then mints and removes them.
It withholds every tool in `WITHHELD_TOOLS` from both the live roster and calls.

## Image pin

The version on the `npm install -g @playwright/mcp@…` instruction in `container/Dockerfile` pins both the tool roster and Chromium dependency used by the image.
Bump it by editing that instruction, then run the webview unit tests and build `bro-webview`; the Dockerfile content changes the worker image hash.
