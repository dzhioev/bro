# brog/CLAUDE.md

brog ("bro backlog") is a minimal, dev-shaped task-tracker facade exposed as the `brog` MCP namespace: exactly what the dev workflow needs — create, read, close, comment — with the concrete tracker behind it selected by config. Switching a repo's tracker is a config change only.

## Design

- The tool surface is deliberately narrower than flow's: no focus, media, triage, or importance/driver/deadline. That richness stays on `flow::`, which brog does not replace — it fronts it (and, later, other trackers) for dev sessions.
- The backend is selected by the `brog` secret (`~/.ppp/brog.json`, schemas in `setup/CLAUDE.md`). The config is **self-contained**: every credential the active backend needs is embedded, so the toolset's manifest is a static `('brog',)` and brog makes no assumption about other secrets being granted to the session. An unknown backend or transport fails at build, loudly.
- Task statuses collapse to three: `open` (workable), `done`, `dropped` (won't happen). Tasks are born open — `create_task` yields an active task, so dev-created tasks bypass Inbox triage.
- Conceptually a task is a *description* plus an append-only *comment stream*; `read_task` returns the description, `read_comments` the stream as structured entries (`brog.model.Comment`: topic, author, UTC timestamp, body — all metadata is the backend's own record, never caller-supplied; topic and author are None when the backend recorded none). `add_comment` appends an entry from topic + body; `append_description` / `edit_description` scope to the description and refuse to touch the stream. How the boundary and the metadata are stored is a backend detail.
- Ids are opaque strings in the backend's native canonical form; each backend accepts its natural refs and returns the canonical form.

## Backends

Each backend is a `System` implementation; how brog concepts map onto the tracker lives in its module.

- **Flow proxy** (`flow_proxy.py`) — brog ops over flow tool calls, behind a `Transport` seam with two implementations: `HTTPTransport`, a raw stateless JSON-RPC POST client of a deployed flow MCP server (one bare `tools/call` per op; requires `json_response=True` server-side and fails pointedly on an SSE-framed response), and `LocalTransport`, an in-process `flow.System` from embedded Notion credentials. Refs: dashed UUID or Notion URL. Status tables: `STATUS_TO_FLOW` / `STATUS_FROM_FLOW`. Flow pages have no structural body/comments split, so the page is the record: the proxy writes the `## Comments` sentinel heading into the page (with the first comment) and splits/scopes against it, and each entry's metadata lives in a `### <topic> — <author> @<ts>` heading the proxy renders and parses back — author = the hosting persona, timestamp = the write moment.
- **GitHub Issues** (`github.py`) — REST via `github/api.py`; issue-per-task, labels as tags, no projects. Refs: issue number, `#N`, or issue URL; the repo comes from config `repo` or the workspace's `origin` remote (`origin_repo`). Status tables: `_STATUS_TO_PATCH` / `_status_from_issue`. Pull requests share the issues numbering and listings — every op rejects a ref resolving to one and `list_tasks` filters them out. Comment metadata is native: author = the comment's login (the config token's account is the acting identity), timestamp = its creation time; only the topic needs a place, written as the body's leading `### <topic>` heading (a headingless comment reads whole with no topic). `blocked_by` reads the issue-dependencies endpoint, short-circuited by the issue's dependency-count summary.

## Components

- `model.py` — `Task` / `Project` / `Comment` / `Status`
- `system.py` — the `System` ABC (the backend surface, parallel to `flow.System`), config parsing/validation, `build_system` / `default_system` (author = the session persona from `CW_BRO`)
- `flow_proxy.py` / `github.py` — the backends (see above)
- `mcp.py` — the `brog` Toolset (`spec`), registered as a static server in the root `mcp_server.py`

## Testing

`system_test.py` (config validation), `flow_proxy_test.py` (proxy logic over a fake transport; http transport over mocked urllib, including the SSE error path; local transport over a mocked flow System), `github_test.py` (the GitHub backend over a fake `github.api`), `mcp_test.py` (tool round-trips against a fake `System`). All in `run_tests.py` `PYTEST_FILES`.
