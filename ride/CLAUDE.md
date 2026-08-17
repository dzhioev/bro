# ride/CLAUDE.md

`ride/` is the `bro-ride` uv workspace member. It publishes the top-level `ride` package and depends on the framework's `bro` distribution; `bro` never imports `ride`. The root repository owns formatting, lint, typing, packaging policy, and the test gate. Build this member with `uv build --package bro-ride`; regenerate its scripts and committed `ride/_entrypoints.py` with `sync-scripts --project ride`.

## Runtime map

- `ride/cli.py` — the `ride` dispatcher. `solo` is the one-shot mode verb, `along` is the interactive mode verb; `resume`, `list`, `clean`, `exec`, `check-clean`, `scope`, and `banner` are the lifecycle verbs.
- `ride/cw.py` — the compatibility `cw` parser and dispatcher. It preserves the existing command line, nested-container fallback, and `cw ss --in-place` inner contract while delegating to the moved runtime.
- `ride/dive_in.py` — the task utility wrapper. It retains task prefetch, task-derived workspace naming, `CW_TASK_ID`, fresh-origin base selection, and hold defaults, then invokes `ride along` with the resolved project-default bro.
- `ride/session.py` — harness-neutral outer lifecycle: recorded `SessionSpec`, base resolution, auth/scope preflight, workspace kind and lock, resume records, launch dispatch, and keep/drop finish behavior.
- `ride/harness.py` — the `Harness` protocol and lazy harness resolver.
- `ride/bro.py`, `ride/bro_session.py` — the native harness implementation: typed bro options, native recipe resolution, shared bro-run container composition, provisioned host-worktree launch, workspace trail pointer, and exact-recipe continuation.
- `ride/flags.py` — common session, scope, and LLM flag registration shared with the compatibility wrappers.
- `ride/listing.py`, `ride/clean.py`, `ride/scope_report.py` — lifecycle implementations. Workspace subject and teardown operations resolve through the recorded harness, falling back to Claude for pre-ride records.
- `ride/claude/` — the Claude Code harness implementation; see `ride/claude/CLAUDE.md`.

## Invariants

- The runtime layer names no Claude detail in its serialized harness options. `SessionSpec.harness_options` belongs to the selected implementation and is validated there.
- The harness seam owns scope recipes, auth, LLM resolution, inner command, workspace state operations, host/container launch, and the in-place runner. Generic credential computation and broker-root process supervision remain in `bro.launch` for native launch and summon reuse.
- A bro resume reads the broker-published pointer beside the workspace's `resume.json` and continues that trail under the recipe recorded in the session spec. No pointer is synthesized when the broker or native trail recording is disabled.
- The compatibility inner command remains `cw ss --in-place` until the retirement stage. Do not switch it to `ride` early: host and workspace checkouts may be on opposite sides of this feature branch.
- A pinned mode-verb workspace is never auto-dropped. An unpinned `along` workspace is kept unless `--drop` is explicit; an unpinned `solo` workspace is dropped after a clean exit unless `--keep` is explicit.
- A solo resume becomes an along session and takes along's host-sensitive default hold; the unattended solo hold describes a run with no human channel.
- `ride` refuses nested launches while process-host mode is unavailable. `cw` alone retains the previous host-worktree fallback.
