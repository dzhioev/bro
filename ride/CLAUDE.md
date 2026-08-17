# ride/CLAUDE.md

`ride/` is the `bro-ride` uv workspace member. It publishes the top-level `ride` package and depends on the framework's `bro` distribution; `bro` never imports `ride`. The root repository owns formatting, lint, typing, packaging policy, and the test gate. Build this member with `uv build --package bro-ride`; regenerate its scripts and committed `ride/_entrypoints.py` with `sync-scripts --project ride`.

## Runtime map

- `ride/cli.py` — the `ride` dispatcher. `along` is the interactive mode verb; `resume`, `list`, `clean`, `exec`, `check-clean`, `scope`, and `banner` are the lifecycle verbs.
- `ride/cw.py` — the compatibility `cw` parser and dispatcher. It preserves the existing command line, nested-container fallback, and `cw ss --in-place` inner contract while delegating to the moved runtime.
- `ride/dive_in.py` — the task utility wrapper. It retains task prefetch, task-derived workspace naming, `CW_TASK_ID`, fresh-origin base selection, and hold defaults, then invokes `ride along` with the resolved project-default bro.
- `ride/session.py` — harness-neutral outer lifecycle: recorded `SessionSpec`, base resolution, auth/scope preflight, workspace kind and lock, resume records, launch dispatch, and keep/drop finish behavior.
- `ride/harness.py` — the `Harness` protocol and lazy harness resolver. The unsupported `bro` value fails here until its stage lands.
- `ride/flags.py` — common session, scope, and LLM flag registration shared with the compatibility wrappers.
- `ride/listing.py`, `ride/clean.py`, `ride/scope_report.py` — lifecycle implementations. Workspace subject and teardown operations resolve through the recorded harness, falling back to Claude for pre-ride records.
- `ride/claude/` — the Claude Code harness implementation; see `ride/claude/CLAUDE.md`.

## Invariants

- The runtime layer names no Claude detail in its serialized harness options. `SessionSpec.harness_options` belongs to the selected implementation and is validated there.
- The harness seam owns scope recipes, auth, LLM resolution, inner command, workspace state operations, host/container launch, and the in-place runner. Generic credential computation remains in `bro.launch.scope` for native launch and summon reuse.
- The compatibility inner command remains `cw ss --in-place` until the retirement stage. Do not switch it to `ride` early: host and workspace checkouts may be on opposite sides of this feature branch.
- A pinned `ride along -w NAME` workspace is never auto-dropped. An unpinned interactive workspace is kept unless `--drop` is explicit.
- `ride` refuses nested launches while process-host mode is unavailable. `cw` alone retains the previous host-worktree fallback.
