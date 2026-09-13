# bro-ride

`ride/` is the `bro-ride` uv workspace member.
It publishes the top-level `ride` package, depends on the framework's `bro` distribution, and declares its own runtime/UI dependencies directly;
`bro` never imports `ride`.
It does not depend on `bro-native`:
the native adapter spawns the `bro` command and reports a missing engine before spawn.
The root repository owns formatting, lint, typing, packaging policy, and the test gate.
Build this member with `uv build --package bro-ride`;
regenerate its scripts and committed `ride/_entrypoints.py` with `sync-scripts --project ride`.

## Runtime map

- `ride/cli.py` — the `ride` dispatcher.
  `solo` is the one-shot mode verb, `along` the interactive mode verb;
  `resume`, `list`, `clean`, `exec`, `check-clean`, `scope`, and `banner` are lifecycle verbs.
- `ride/ask.py`, `ride/call.py` — pure option-preserving aliases of `ride solo` and `ride along`.
  Their scripts live in this distribution and add no implied fast mode or other flags.
- `ride/dive_in.py` — task utility wrapper:
  prefetch, task-derived workspace naming, `RIDE_TASK_ID`, fresh-origin base selection, hold defaults, and forwarding to `ride along` with the project-default bro.
- `ride/session.py` — harness-neutral session lifecycle:
  recorded `SessionSpec` including isolation and the optional repository attachment, base resolution, auth/scope preflight, and the workspace lock,
  resume records, keep/drop finish behavior, and the one started-party launcher that prepares attached or detached workspaces in either isolation for roots and spawned children.
- `ride/repository.py` — path/URL attachment resolution, normalized managed-mirror keys, flocked no-prune fetches, committed-tree reads, and mirror cleanup.
  `attachment_identities` is the host-config identities an attachment matches project entries by, reading a checkout's `origin` for the URL one.
- `ride/do_ride.py` — the `do-ride` session executable every launcher runs inside a prepared workspace:
  its own parser and argv builder, the session environment and pid/start-time record, credential hooks, missing Claude state and plugin seed, persona provisioning, the session broxy, and SIGTERM-forwarded agent spawning.
- `ride/errors.py` — the runtime-path and migration error wrapper shared by the distribution's public scripts.
- `ride/scope.py` — per-surface launch scoping:
  `ScopeRecipe`, `BRO_RUN_RECIPE`, attachment-bound credential selection, the project/host grant layers, three-way scope override splitting, permit computation, and the strict launch preflight.
  In-process `bro run` / `bro chat` create no scope.
- `ride/root.py` — supervision of either neutral started-party launch for roots and manually launched children, behind the broker availability gate.
- `ride/spawn.py` — broker-root composition and summon lowering:
  started parties go through the common isolation-parameterized launcher and carry a resume spec;
  joined members run in the summoner’s existing tree with member-scoped records and no resume;
  per-root journal subscribers project audit and manual-token cleanup, and the bounded credential scope reaches contributed kinds.
  The channel listener's bind hosts are derived here:
  loopback, plus the docker bridge gateway when that is an address of this host.
- `ride/kinds.py` — the `bro.broker_kinds` entry-point group:
  broker request kinds contributed by installed distributions, each entry a factory `(context: bro.kinds.KindContext) -> RequestHandler`, loaded into every root broker beside the built-ins.
- `ride/peer_facts.py` — the quest-keyed facts table for one broker root:
  peer resolution through the journal worker binding, workspace/member/bro attribution, effective summon authority, scope inputs, ancestry, and per-member current-trail attribution shared by summon, artifacts, and audit.
- `ride/artifacts.py` — the ride's artifact store, the `artifact.mint` / `artifact.get` kinds, and the broker's `JobOutput`:
  reflink-or-copy ingest into content-addressed objects, per-peer view directories behind the read-only `/var/ride/artifacts` mounts, the sharing rules with their uniform denial, the byte cap, and the JSONL audit beside the store.
  A broker job's run directory is staged in the store and collected through the same ingest, reaching the peer that requested the job and its summoners.
  The peer wire and CLI are the framework's `bro/artifact.py`.
- `ride/summon_control.py` — summon host authorization, child authority resolution, and start/join placement, plus journal projections for audit, lifecycle logging, and manual-token cleanup;
  the manual variant registers as an expected external Worker with its pending record.
  The peer wire and self-contained CLI are the framework's `bro/summon.py`.
- `ride/pending_summon.py` — pending manual summons:
  the runtime-carrying record a launch token resolves to, written by the control and one-shot-claimed by the `--summoned` launch, whose claim records the child's workspace name — the attribution source for the manual peer.
- `ride/trails.py` — local-trails mounts for launch descriptions whose computed scope records locally.
- `ride/identity.py` — managed-session git identities:
  the bro a session commits as, and the launching human it credits, read from the attachment's own git configuration.
- `ride/harness.py`
  — the `Harness` protocol (flag registration and option packing, scope, auth, session reads, and the launch hooks: `do-ride` argv flags, the session run, boxed extras, unboxed runner env), the harness roster, and the lazy harness resolver.
- `ride/bro.py` — native harness implementation:
  native recipe resolution, the session runner spawning `bro run|chat …` with exact-recipe continuation, and the launch hooks.
- `ride/flags.py` — common session, scope, and LLM flag registration, harness flag registration with the generic requires-`--harness` refusal and option packing, and the default an omitted `--hold` resolves to.
- `ride/runtime_bundle.py` — installation freeze, content-addressed bundle persistence and locking, supplied-runtime validation/re-exec, shared host/container materialization, session-command shims, runtime-volume lifecycle, and bundle GC.
- `ride/runtime_state.py` — one-shot migration of historical checkout-keyed stores and pre-isolation workspace records, including collision/liveness preflight and per-root workspace attachment recovery.
- `ride/listing.py`, `ride/clean.py`, `ride/scope_report.py` — lifecycle implementations.
- `ride/e2e_test.py` — live Docker launch coverage, outside the default test roster.
- `ride/workspace/` — managed workspace creation, provisioning, container execution, credential hydration, broker spawners, and teardown;
  see `ride/workspace/AGENTS.md`.
- `ride/setup/` — packaged runtime/project image and managed-session entrypoint assets;
  see `ride/setup/AGENTS.md`.
- `ride/claude/` — the Claude Code harness implementation;
  see `ride/claude/AGENTS.md`.

## Invariants

- The runtime layer names no Claude detail in its serialized harness options.
  `SessionSpec.harness_options` belongs to the selected implementation and is validated there.
- The neutral layer owns one started-party launcher parameterized by isolation;
  the harness seam supplies scope recipes, auth, LLM resolution, the `do-ride` command, session-state reads, and the per-harness launch extras.
  `do-ride` owns every session's common setup before calling the selected harness runner.
  A managed boxed or unboxed workspace is always launched by `ride`;
  a summon child is spawned by `summon`
  — except a manual one, which the user launches with `ride along --summoned <token>` against the summoner's provisioned channel.
- Every harness keeps its session state among the workspace's own records, so reclaiming a workspace is `Workspace.remove()` for all of them and no harness supplies a teardown of its own.
- Every outer root runs one runtime bundle for its full lifetime:
  either a locked freeze of the invoking installation, or the materialized `venv/` + `bin/` layout named by `--runtime-bundle` after re-executing its `ride`.
  Unboxed workspaces run its absolute host materialization;
  boxed workspaces require a frozen manifest, mount its named runtime volume read-only, and reuse the root's image tag and bundle hash for started children.
  A manual token carries the frozen hash or given path, and its launch command names that runtime's own `ride`.
- A bro resume reads the session-published pointer from the workspace's `session/` dir and continues that trail under the recipe recorded in the session spec.
  No pointer is published when trail recording is disabled.
- Workspace state is global under `runtime_base()/workspaces/`, with each workspace's optional repository attachment recorded in metadata.
  Runtime bundles live under `runtime_base()/runtime/`;
  managed URL mirrors under `runtime_base()/repos/`.
  Historical checkout-keyed roots and pre-isolation workspace records migrate under a global lock before an outer command proceeds;
  the migration preflights every collision and live workspace, and a partial run remains resumable.
  Their flocks serialize fetch/cleanup, mirrors never prune, and `ride clean` removes one only when no workspace references its URL.
  Container trails use a dedicated fixed absolute mount.
- Every attached workspace tree is an independent clone on its recorded branch.
  A detached unboxed workspace may instead record one existing external tree outside the runtime root;
  one workspace records that path at a time, resume requires it to remain present, and workspace removal never removes it.
  A legacy linked worktree is launch-refused but remains removable through `ride clean`.
- A ride preflights its Docker daemon once before its first boxed launch, by reading a nonce through a bind of the runtime root.
  Every boxed bind source must resolve under that root.
- A launch's credential instances follow its attachment identity and selected bro on every surface that resolves them
  — the session, `ride scope`, dive-in's prefetch, and the children it summons (`bro/reference/ride.md`, "Scoped credential hydration").
- Both isolations pass `BRO_STORE` and `BRO_INSTALL_KINDS` to `do-ride`, which installs the hooks through one applier into the named session environment directory,
  so a session's git and `gh` act as the identity it was scoped with and never reach the operator's own configuration.
  Every unboxed session's store and install-hook output share a private temporary root removed by its supervisor;
  retained workspace records therefore carry no credential material.
- Mode verbs are detached unless `--repo` explicitly attaches a resolved checkout or git URL.
  Managed detached trees are plain directories, skip repository and persona provisioning, and are clean only while empty;
  an external `--tree` is clean when its last session ended cleanly.
  A URL attachment's user-facing identity stays the normalized URL while its git operations use the managed mirror.
- A pinned mode-verb workspace is never auto-dropped.
  An unpinned `along` workspace is kept unless `--drop` is explicit;
  an unpinned `solo` workspace is dropped after a clean exit unless `--keep` is explicit.
- A solo resume becomes an along session and takes along's isolation-sensitive default hold;
  the unattended solo hold describes a run with no human channel.
- Every reconstructed session argv and `do-ride` command restates the resolved `--hold`.
  The session executable has no placement flag from which to re-derive it.
- Every launcher exports `RIDE_ISOLATION` from the workspace record;
  session placement and the banner never infer it from the surrounding process.
- A joined member inherits its summoner’s workspace and isolation, runs no workspace provisioning, and keeps its session and Claude records under `party/<member>/`.
  It has no resume record;
  clean exit removes its records, failure or kill keeps them, and the private unboxed credential root is always removed.
  The party ends with its first session, so broker teardown kills any members still running.
- The framework seed permits boxed party starts.
  Project and host configuration layers apply idempotently before strict launch/request overrides, and every launch exports the effective set through `RIDE_PERMITS` while summon control enforces its own peer-facts copy.
- Every console script this distribution ships wraps its `main` in `ride.cli.reports_runtime_errors`, so unusable runtime locations and blocked state migrations fail as CLI errors.
