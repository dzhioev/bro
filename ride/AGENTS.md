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
  It also owns the suppressed mode-verb inner-runner tokens.
- `ride/ask.py`, `ride/call.py` — pure option-preserving aliases of `ride solo` and `ride along`.
  Their scripts live in this distribution and add no implied fast mode or other flags.
- `ride/dive_in.py` — task utility wrapper:
  prefetch, task-derived workspace naming, `RIDE_TASK_ID`, fresh-origin base selection, hold defaults, and forwarding to `ride along` with the project-default bro.
- `ride/session.py` — harness-neutral session lifecycle:
  recorded `SessionSpec` including the optional repository attachment, base resolution, auth/scope preflight, workspace kind and lock,
  resume records, keep/drop finish behavior, and the shared launch skeleton for attached or detached runs in both modes.
- `ride/repository.py` — path/URL attachment resolution, normalized managed-mirror keys, flocked no-prune fetches, committed-tree reads, and mirror cleanup.
- `ride/inner.py` — the inner session every harness runs under inside the prepared workspace:
  the argv the outer spawns to re-enter there, the session environment (git identities, `RIDE_BRO`, the hold and this runner's pid), the persona's declared workspace provisioning, the session broxy, and SIGTERM-forwarded agent spawning.
- `ride/scope.py` — per-surface launch scoping:
  `ScopeRecipe`, `BRO_RUN_RECIPE`, attachment-bound credential selection, `scoped_secrets`, the strict launch preflight, scope override splitting, and summoned-child scope computation.
  In-process `bro run` / `bro chat` create no scope.
- `ride/root.py` — neutral container and host-process root supervision behind the broker availability gate.
- `ride/spawn.py` — broker-root composition, summon lowering
  — each child composed through its requested harness's seam hooks, with its recorded resume spec
  — per-root journal subscribers for audit and manual-token cleanup, and the bounded credential scope handed to contributed kinds.
  The channel listener's bind hosts are derived here:
  loopback, plus the docker bridge gateway when that is an address of this host.
- `ride/kinds.py` — the `bro.broker_kinds` entry-point group:
  broker request kinds contributed by installed distributions, each entry a factory `(context: bro.kinds.KindContext) -> RequestHandler`, loaded into every root broker beside the built-ins.
- `ride/peers.py` — peer → workspace attribution for one broker root:
  the summon records that name each peer's workspace and the chain of summoners above it, shared by summon and artifacts.
- `ride/artifacts.py` — the session artifact store, the `artifact.mint` / `artifact.get` kinds, and the broker's `JobOutput`:
  reflink-or-copy ingest into content-addressed objects, per-peer view directories behind the read-only `/var/ride/artifacts` mounts, the sharing rules with their uniform denial, the byte cap, and the JSONL audit beside the store.
  A broker job's run directory is staged in the store and collected through the same ingest, reaching the peer that requested the job and its summoners.
  The peer wire and CLI are the framework's `bro/artifact.py`.
- `ride/summon_control.py` — summon host authorization and allow-list resolution, plus the journal projection that maintains peer identity, audit attribution, root trail fallback, and manual-token cleanup;
  the manual variant registers as an expected external Worker with its pending record.
  The peer wire and self-contained CLI are the framework's `bro/summon.py`.
- `ride/pending_summon.py` — pending manual summons:
  the protocol-stamped record a launch token resolves to, written by the control and one-shot-claimed by the `--summoned` launch, whose claim records the child's workspace name — the attribution source for the manual peer.
- `ride/trails.py` — local-trails mounts for launch descriptions whose computed scope records locally.
- `ride/identity.py` — managed-session git identities:
  the bro a session commits as, and the launching human it credits, read from the attachment's own git configuration.
- `ride/harness.py`
  — the `Harness` protocol (flag registration and option packing, scope, auth, session reads, and the launch hooks: inner argv flags, the in-place run, container extras, host runner env), the harness roster, and the lazy harness resolver.
- `ride/bro.py` — native harness implementation:
  native recipe resolution, the in-place runner spawning `bro run|chat …` with exact-recipe continuation, and the launch hooks.
- `ride/flags.py` — common session, scope, and LLM flag registration, harness flag registration with the generic requires-`--harness` refusal and option packing, and the default an omitted `--hold` resolves to.
- `ride/runtime_bundle.py` — installation freeze, content-addressed bundle persistence and locking, shared host/container materialization, session-command shims, runtime-volume lifecycle, and bundle GC.
- `ride/runtime_state.py` — one-shot migration of historical checkout-keyed stores into the flat global root, including collision/liveness preflight and per-root workspace attachment recovery.
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
- The neutral layer owns both launch bodies;
  the harness seam supplies scope recipes, auth, LLM resolution, the inner command, session-state reads, and the per-harness launch extras.
  The in-place runner is the Claude harness's alone
  — bro workspaces run `bro run|chat`.
  A managed native container or host worktree is always launched by `ride`;
  a summon child is spawned by `summon`
  — except a manual one, which the user launches with `ride along --summoned <token>` against the summoner's provisioned channel.
- Every harness keeps its session state among the workspace's own records, so reclaiming a workspace is `Workspace.remove()` for all of them and no harness supplies a teardown of its own.
- Every outer root freezes the invoking installation into one locked runtime bundle for its full lifetime.
  Host workspaces run its absolute snapshot;
  containers mount its named runtime volume read-only, and summoned children reuse the root's image tag and bundle hash.
- A bro resume reads the session-published pointer from the workspace's `session/` dir and continues that trail under the recipe recorded in the session spec.
  No pointer is published when trail recording is disabled.
- Workspace state is global under `runtime_base()/workspaces/`, with each workspace's optional repository attachment recorded in metadata.
  Runtime bundles live under `runtime_base()/runtime/`;
  managed URL mirrors under `runtime_base()/repos/`.
  Historical checkout-keyed roots migrate under a global lock before an outer command proceeds;
  the migration preflights every collision and live workspace, and a partial run remains resumable.
  Their flocks serialize fetch/cleanup, mirrors never prune, and `ride clean` removes one only when no workspace references its URL.
  Container trails use a dedicated fixed absolute mount.
- A launch's credential instances follow its attachment identity and selected bro on every surface that resolves them
  — the session, `ride scope`, dive-in's prefetch, and the children it summons (`bro/reference/ride.md`, "Scoped credential hydration").
- Both modes install the scoped store's credential hooks through the one applier, into a session directory that bounds what a hook may write,
  so a session's git and `gh` act as the identity it was scoped with and never reach the operator's own configuration.
- Mode verbs are detached unless `--repo` explicitly attaches a resolved checkout or git URL.
  Detached trees are plain directories, skip repository and persona provisioning, and are clean only while empty.
  A URL attachment's user-facing identity stays the normalized URL while its git operations use the managed mirror.
- A pinned mode-verb workspace is never auto-dropped.
  An unpinned `along` workspace is kept unless `--drop` is explicit;
  an unpinned `solo` workspace is dropped after a clean exit unless `--keep` is explicit.
- A solo resume becomes an along session and takes along's host-sensitive default hold;
  the unattended solo hold describes a run with no human channel.
- Every reconstructed session argv restates the resolved `--hold`.
  The inner argv cannot carry `--host`, so a re-parse cannot be trusted to re-derive a hold that was resolved against it.
- `ride` refuses nested launches while process-host mode is unavailable, on the container probe rather than on any marker the environment carries.
- Every console script this distribution ships wraps its `main` in `ride.cli.reports_runtime_errors`, so unusable runtime locations and blocked state migrations fail as CLI errors.
