# bro-ride

`ride/` is the `bro-ride` uv workspace member. It publishes the top-level `ride` package and depends on the framework's `bro` distribution; `bro` never imports `ride`. The root repository owns formatting, lint, typing, packaging policy, and the test gate. Build this member with `uv build --package bro-ride`; regenerate its scripts and committed `ride/_entrypoints.py` with `sync-scripts --project ride`.

## Runtime map

- `ride/cli.py` — the `ride` dispatcher. `solo` is the one-shot mode verb, `along` the interactive mode verb; `resume`, `list`, `clean`, `exec`, `check-clean`, `scope`, and `banner` are lifecycle verbs. It also owns the suppressed mode-verb inner-runner tokens.
- `ride/ask.py`, `ride/call.py` — pure option-preserving aliases of `ride solo` and `ride along`. Their scripts live in this distribution and add no implied fast mode or other flags.
- `ride/dive_in.py` — task utility wrapper: prefetch, task-derived workspace naming, `RIDE_TASK_ID`, fresh-origin base selection, hold defaults, and forwarding to `ride along` with the project-default bro.
- `ride/session.py` — harness-neutral outer lifecycle: recorded `SessionSpec`, base resolution, auth/scope preflight, workspace kind and lock, resume records, launch dispatch, and keep/drop finish behavior.
- `ride/harness.py` — the `Harness` protocol and lazy harness resolver.
- `ride/bro.py`, `ride/bro_session.py` — native harness implementation: typed options, native recipe resolution, shared bro-run container composition, provisioned host-worktree launch, workspace trail pointer, and exact-recipe continuation.
- `ride/flags.py` — common session, scope, and LLM flag registration.
- `ride/listing.py`, `ride/clean.py`, `ride/scope_report.py` — lifecycle implementations.
- `ride/claude/` — the Claude Code harness implementation; see `ride/claude/CLAUDE.md`.

## Invariants

- The runtime layer names no Claude detail in its serialized harness options. `SessionSpec.harness_options` belongs to the selected implementation and is validated there.
- The harness seam owns scope recipes, auth, LLM resolution, inner command, workspace state operations, host/container launch, and the in-place runner. Generic credential computation and broker-root process supervision remain in `bro.launch` for native launch and summon reuse.
- Claude workspaces run their checkout's `ride solo|along --in-place`; native workspaces run `bro run|chat … --in-place`. These hidden inner contracts fail loudly when a workspace tree predates them.
- A bro resume reads the broker-published pointer beside the workspace's `resume.json` and continues that trail under the recipe recorded in the session spec. No pointer is synthesized when the broker or native trail recording is disabled.
- Host runtime state lives under the setup-provisioned `/var/ride/<project-key>` root; launches validate it before recording a workspace. Container trails and summon status use dedicated fixed absolute mounts.
- A pinned mode-verb workspace is never auto-dropped. An unpinned `along` workspace is kept unless `--drop` is explicit; an unpinned `solo` workspace is dropped after a clean exit unless `--keep` is explicit.
- A solo resume becomes an along session and takes along's host-sensitive default hold; the unattended solo hold describes a run with no human channel.
- `ride` refuses nested launches while process-host mode is unavailable.
