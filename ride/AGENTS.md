# bro-ride

`ride/` is the `bro-ride` uv workspace member. It publishes the top-level `ride` package and depends on the framework's `bro` distribution; `bro` never imports `ride`. The root repository owns formatting, lint, typing, packaging policy, and the test gate. Build this member with `uv build --package bro-ride`; regenerate its scripts and committed `ride/_entrypoints.py` with `sync-scripts --project ride`.

## Runtime map

- `ride/cli.py` — the `ride` dispatcher. `solo` is the one-shot mode verb, `along` the interactive mode verb; `resume`, `list`, `clean`, `exec`, `check-clean`, `scope`, and `banner` are lifecycle verbs. It also owns the suppressed mode-verb inner-runner tokens.
- `ride/ask.py`, `ride/call.py` — pure option-preserving aliases of `ride solo` and `ride along`. Their scripts live in this distribution and add no implied fast mode or other flags.
- `ride/dive_in.py` — task utility wrapper: prefetch, task-derived workspace naming, `RIDE_TASK_ID`, fresh-origin base selection, hold defaults, and forwarding to `ride along` with the project-default bro.
- `ride/session.py` — harness-neutral session lifecycle: recorded `SessionSpec`, base resolution, auth/scope preflight, workspace kind and lock, resume records, keep/drop finish behavior, and the shared launch skeleton for both modes — active-container refusal, stale-pointer clear, resume gate, the trails opt-out, the container `Launch` composition, and the provisioned host-worktree body.
- `ride/scope.py` — per-surface launch scoping: `ScopeRecipe`, `BRO_RUN_RECIPE`, project-bound credential selection, `scoped_secrets`, the strict launch preflight, scope override splitting, and summoned-child scope computation. In-process `bro run` / `bro chat` create no scope.
- `ride/root.py` — neutral container and host-process root supervision behind the broker availability gate.
- `ride/spawn.py` — broker-root composition, root lifecycle handlers, native trail-pointer publication for the root and summoned children, summon lowering — each child composed through its requested harness's seam hooks, with its recorded resume spec — and per-root `SummonControl` wiring.
- `ride/summon_control.py` — summon host authorization, allow-list resolution, audit/status bookkeeping, and request lifecycle. The peer wire and self-contained CLI are the framework's `bro/summon.py`.
- `ride/trails.py` — local-trails mounts for launch descriptions whose computed scope records locally.
- `ride/identity.py` — managed-session git identity.
- `ride/harness.py` — the `Harness` protocol (flag registration and option packing, scope, auth, session reads, and the launch hooks: inner command, container extras, host runner env), the harness roster, and the lazy harness resolver.
- `ride/bro.py` — native harness implementation: native recipe resolution, the `bro run|chat …` inner command with exact-recipe continuation, and the launch hooks.
- `ride/flags.py` — common session, scope, and LLM flag registration, harness flag registration with the generic requires-`--harness` refusal and option packing, and the default an omitted `--hold` resolves to.
- `ride/listing.py`, `ride/clean.py`, `ride/scope_report.py` — lifecycle implementations.
- `ride/e2e_test.py` — live Docker launch coverage, outside the default test roster.
- `ride/claude/` — the Claude Code harness implementation; see `ride/claude/AGENTS.md`.

## Invariants

- The runtime layer names no Claude detail in its serialized harness options. `SessionSpec.harness_options` belongs to the selected implementation and is validated there.
- The neutral layer owns both launch bodies; the harness seam supplies scope recipes, auth, LLM resolution, the inner command, session-state reads, and the per-harness launch extras. The in-place runner is the Claude harness's alone — bro workspaces run `bro run|chat`. A managed native container or host worktree is always launched by `ride`; a summon child is always launched by `summon`.
- Every harness keeps its session state among the workspace's own records, so reclaiming a workspace is `Workspace.remove()` for all of them and no harness supplies a teardown of its own.
- Claude workspaces run their checkout's `ride solo|along --in-place`, a hidden inner contract that fails loudly when the workspace tree predates it. Native workspaces run plain `bro run|chat …`, which carries no contract marker — a tree based on a ref that predates the in-process verbs runs its older CLI unguarded, out of support rather than detected.
- A bro resume reads the broker-published pointer from the workspace's `session/` dir and continues that trail under the recipe recorded in the session spec. No pointer is synthesized when the broker or native trail recording is disabled.
- Host runtime state lives under the user's checkout-keyed runtime state root, which a launch creates before recording a workspace. Container trails and summon status use dedicated fixed absolute mounts.
- A pinned mode-verb workspace is never auto-dropped. An unpinned `along` workspace is kept unless `--drop` is explicit; an unpinned `solo` workspace is dropped after a clean exit unless `--keep` is explicit.
- A solo resume becomes an along session and takes along's host-sensitive default hold; the unattended solo hold describes a run with no human channel.
- Every reconstructed session argv restates the resolved `--hold`. The inner argv cannot carry `--host`, so a re-parse cannot be trusted to re-derive a hold that was resolved against it.
- `ride` refuses nested launches while process-host mode is unavailable, on the container probe rather than on any marker the environment carries.
- Every console script this distribution ships wraps its `main` in `ride.cli.reports_location_errors`, so an environment naming no runtime location fails as a CLI error.
