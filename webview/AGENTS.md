# Webview worker

The `bro-webview` member publishes the registered `webview` worker type, its owner and setup commands, and its container-side daemon and capture process in the `bro.webview` namespace.
It depends on core for broker and artifact contracts, on `bro-ride` for setup's foreground worker run, and on the MCP SDK for the Playwright stdio client.
Only `bro.webview.setup` imports `ride`;
the registered type and container-side modules stay dependency-light because every launch-scope fold and broker root loads them.

## Development

Formatting, linting, typing, tests, and packaging use the root repository gate.
The host-only real-browser route runs separately with `run-tests --only webview_e2e`.
Run `sync-scripts --project webview` after adding or removing a CLI, and build the wheel with `uv build --package bro-webview`.

## Components

- `bro/webview/worker.py` — the dependency-light registered type, its bounded `pass` and `vnc` launch schema, launch validation, and packaged container specification.
  A caller needs `:launch.webview`, plus `:launch.webview.vnc` for the optional noVNC endpoint (`bro/reference/ride.md`, "Session permissions and credentials")
- `bro/webview/profile.py` — the profile bound, envelope validation, and profile-version to Playwright MCP pin pairing
- `bro/webview/container/Dockerfile` — the runtime-derived image with Xvfb, noVNC, Chromium, and the pinned Playwright MCP
- `bro/webview/serve.py` — the shared display, VNC, and Playwright startup plus the container daemon, passed-profile loader, and command loop
- `bro/webview/capture_protocol.py` — the options environment name and bounded exchange-line size shared by setup and capture
- `bro/webview/capture.py` — the container-side capture exchange and fixed storage-state call
- `bro/webview/setup.py` — the host-side profile lock, seed and capture workflow, guarded store write, and summary
- `bro/webview/cli.py` (`webview`) — the owner-side `open`, `close`, and `setup` verbs and the container-side `serve` and `capture` verbs
- `bro/webview/mcp.py` — the registered `webview` toolset for typed owner-side open, command, live-roster, sharing, reopen, and close operations

## Owner toolset

The `webview` toolset is the typed owner surface over the mission wire.
`open` retains the launch options and initial shares, `command` bounds reply text while preserving the whole text by artifact ref, and `tools` condenses the live Playwright roster.
`share` returns the worker-side upload path, `reopen` repeats every retained option and share before restoring the last reported page URL, and `close` returns the terminal outcome.

## Owner command

`webview open` forwards optional VNC, a requested VNC port, a `cookies` profile pass, advisory origin lists, shared artifact refs, and a lifetime bound to `launch`, then waits through the accepted and started marks for the daemon's ready event.
`--cookies <instance>` passes that profile without putting it in the owner's store;
`--vnc --port <port>` fixes the noVNC view's launcher-loopback port.
It prints the mission id and optional noVNC URL as JSON;
a denial, failed launch, or startup silence exits non-zero, with startup expiry cancelling the launch.
`webview close MISSION` asks the daemon to close, then waits for its acknowledgement, terminal outcome, and settled worker supervision.
`webview setup <instance>` runs only on a host terminal, opens a foreground capture container and prints its noVNC URL, then stores the state after Enter under `cookies+<instance>`.
It seeds an existing profile unless `--fresh`, keeps IndexedDB when the seed contains it or `--indexed-db` requests it, and accepts `--url` and a fixed `--port`.
It holds the stored name's lock for the run, writes only while the starting material is unchanged, and reports counts and bytes without values.
Commands between `open` and `close` use `mission ask <id> '<json>' --wait`, with `mission history <id>` retaining their full payloads.
`mission share <id> <ref>` makes a later-minted upload available under `/workspace/artifacts/<ref>` without reopening the webview.

## Browser profile

The daemon reads the selected `cookies` kind before it listens and refuses an oversized, malformed, extra-kind, or newer-version profile without exposing its material.
It strips `profile_version`, writes the Playwright storage state at 0600 in its private configuration directory, and starts Playwright MCP with `--isolated --storage-state <path>`.
The file remains for the daemon's life so a re-created isolated context starts from the same profile.
The Playwright MCP `storage` capability and `browser_run_code_unsafe` remain withheld.

## Capture wire

Setup starts capture with `WEBVIEW_CAPTURE_OPTIONS={"url", "indexed_db"}` and exchanges ASCII-escaped JSON objects one per line over its stdin and stdout.
Every read is bounded at the profile limit plus 1 KiB, and capture's child processes write to stderr so stdout carries only this sequence:
setup sends `{"seed": <state>|null}`, capture sends `{"event":"ready"}`, setup sends `{"request":"state"}`, and capture sends either `{"event":"state","state":<state>}` or `{"event":"error","error":<reason>}`.
Capture runs the display, noVNC, and Playwright startup shared with serve in a private temporary directory and saves state through its fixed `browser_run_code_unsafe` call.

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
Bump it by editing that instruction, incrementing `PROFILE_VERSION` with the new pin in `profile.py`, and adding the outgoing version's synthetic profile to `webview_e2e`.
Then run the webview unit tests and build `bro-webview`;
the Dockerfile content changes the worker image hash.
