# Webview worker

The `bro-webview` member publishes the registered `webview` worker type, its owner command, and its container-side daemon in the `bro.webview` namespace.
It depends on core for broker and artifact contracts and on the MCP SDK for the Playwright stdio client.

## Development

Formatting, linting, typing, tests, and packaging use the root repository gate.
The host-only real-browser route runs separately with `run-tests --only webview_e2e`.
Run `sync-scripts --project webview` after adding or removing a CLI, and build the wheel with `uv build --package bro-webview`.

## Components

- `bro/webview/worker.py` — the dependency-light registered type, its `vnc` launch-flag schema, launch validation, and packaged container specification.
  A caller needs `:launch.webview`, plus `:launch.webview.vnc` for the optional noVNC endpoint (`bro/reference/ride.md`, "Session permissions and credentials")
- `bro/webview/container/Dockerfile` — the runtime-derived image with Xvfb, noVNC, Chromium, and the pinned Playwright MCP
- `bro/webview/serve.py` — the container daemon and Playwright MCP command loop
- `bro/webview/cli.py` (`webview`) — the owner-side `open` and `close` verbs and the container-side `serve` verb

## Owner command

`webview open` forwards optional VNC, advisory origin lists, shared artifact refs, and a lifetime bound to `launch`, then waits through the accepted and started marks for the daemon's ready event.
It prints the mission id and optional noVNC URL as JSON;
a denial, failed launch, or startup silence exits non-zero, with startup expiry cancelling the launch.
`webview close MISSION` asks the daemon to close, then waits for its acknowledgement, terminal outcome, and settled worker supervision.
Commands between those verbs use `mission ask <id> '<json>' --wait`, with `mission history <id>` retaining their full payloads.
`mission share <id> <ref>` makes a later-minted upload available under `/workspace/artifacts/<ref>` without reopening the webview.

## Mission wire

A webview mission has talk `{owner.question, worker.say}` and carries JSON objects as its chat payloads.
An owner question is either one Playwright MCP call as `{tool, arguments?}` or `{webview: tools|close}`.
The daemon answers a tool call with `{text, files}`, where each file names its workspace-relative path and artifact ref or mint error, and answers a malformed or failed call with `{error}`.
An oversized reply names the artifact holding its text or its whole object instead.
The `ready` say is `{event: ready, vnc}`, and a close ends the mission with the number of successful tool calls.

The daemon collects files new or changed anywhere under `/workspace` around each tool call, except the read-only artifact view, then mints and removes them.
It withholds every tool in `WITHHELD_TOOLS` from both the live roster and calls.
A webview peer's own `launch` section is empty, so the generic type-key check prevents it from launching another worker.

## Image pin

The version on the `npm install -g @playwright/mcp@…` instruction in `container/Dockerfile` pins both the tool roster and Chromium dependency used by the image.
Bump it by editing that instruction, then run the webview unit tests and build `bro-webview`; the Dockerfile content changes the worker image hash.
