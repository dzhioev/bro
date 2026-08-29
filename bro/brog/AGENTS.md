# brog task tracker facade

brog ("bro backlog") is minimal and development-shaped, exposed as the `brog` MCP namespace:
create, read, close, and comment operations with the concrete tracker selected by config.
Switching a repository's tracker is a config change only.

## Design

- The tool surface deliberately covers only the task operations the development workflow needs;
  tracker-specific capabilities stay behind the backend boundary.
- The backend is selected by the `brog` credential (schemas in `bro/setup/AGENTS.md`).
  The config is self-contained:
  every credential the active backend needs is embedded
  — literally or as `$cred` reference nodes the resolver expands before brog reads the config
  — so the toolset's manifest is a static `('brog',)` and brog makes no assumption about other secrets granted to the session.
  The `backend` field resolves through the built-in GitHub backend or the matching `bro.brog.backends` entry point;
  an unknown backend fails at build.
- Task statuses collapse to three:
  `open` (workable), `done`, and `dropped` (won't happen).
  Tasks are born open.
- Conceptually a task is a description plus an append-only comment stream.
  `read_task` returns the description;
  `read_comments` returns structured `bro.brog.model.Comment` entries with topic, author, UTC timestamp, and body.
  Metadata comes from the backend, never the caller;
  topic and author are `None` when the backend recorded neither.
  `add_comment` appends an entry from topic and body, while `append_description` and `edit_description` operate only on the description.
  Storage is a backend detail.
- Ids are opaque strings in the backend's native canonical form;
  each backend accepts its natural refs and returns the canonical form.

## Backends

Each backend implements `System`;
its module owns the mapping between brog concepts and the tracker.
The built-in GitHub Issues backend lives in `github.py`.
Installed distributions add other implementations through the `bro.brog.backends` entry-point group, which keeps their modules, schemas, and storage rules outside the framework.

The GitHub backend uses `bro/extra/github/api.py`:
one issue per task, labels as tags, and no projects.
Refs may be an issue number, `#N`, or an issue URL;
the repo comes from config `repo` or the workspace's `origin` remote (`origin_repo`).
Pull requests share issue numbering, so every operation rejects a ref resolving to one and `list_tasks` filters them out.
Comment author and timestamp come from GitHub;
the topic is stored as the body's leading `### <topic>` heading, while a headingless comment has no topic.
`blocked_by` reads the issue-dependencies endpoint, short-circuited by the issue's dependency-count summary.

## Components

- `model.py` — `Task` / `Project` / `Comment` / `Status`
- `system.py` — the `System` ABC, config parsing and validation, backend discovery, and `build_system` / `default_system`;
  author is the session persona from `RIDE_BRO`
- `github.py` — built-in GitHub Issues backend
- `mcp.py` — the brog `Toolset`, exported as `toolset` for persona mounts and the standalone entry point

## Testing

`system_test.py` covers config validation and backend discovery, `github_test.py` covers the GitHub backend over a fake API, and `mcp_test.py` covers tool round-trips against a fake `System`.
All are in `bro/local/run_tests.py`'s explicit `PYTEST_FILES` roster.
