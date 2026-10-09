# Framework core

`bro/` and `bros/bro/` are the `bro` distribution:
the persona declaration and what composes and runs one, the declaration vocabulary, the shared low-level utilities, and the minimal `bro` persona with the spells every bro inherits.
Core imports no other workspace member;
the engine that runs a declaration is `native/`, and the managed-workspace runtime is `ride/`.
Paths in this file are relative to `bro/`, except the top-level `bros/bro/`;
native-owned paths are spelled from the repository root and keep their public `bro.*` import names.
A subpackage with a map of its own is pointed at, not described here.

## Modules

- `bro.py` — `BaseBro` ABC (the framework) and the framework helpers (`BroRaised`).
  The class is the persona *declaration* and its composition;
  the engine that runs one is `native/bro/native/runner.py`.
  How to declare one: `bro/reference/extending.md`;
  what a run renders, mounts, and counts: "Running a declaration" below.
- `mcp.py` — declaration vocabulary for persona tool reach and toolset modules:
  surface facts and rendering, `MCPServerSpec`, `ToolLayer` and the keyed reach entries it carries, the harness-neutral `Reach` they fold to, and `Toolset`;
  and the `files` / `brash` / `web` / `delegation` group constructors beside `mount` / `cli` / `source` / `man` / `revoke`.
  Live tool and server objects stay in `llm/mcp.py` and are imported only when a declaration is built
- `spells.py` — the spell store:
  validation of the files a bro's `spells` declaration names and the reserved `spell` MCP namespace they are served under, with `bro::cast` and the native `bro::skill` loader (`bro/reference/ride.md`, "Bro spells and skills").
- `procedures.py` — `parse_frontmatter`, the flat frontmatter grammar a spell file opens with (`bro/reference/extending.md`, "Declaring a bro").
- `registry.py` — process-wide registry of bro classes:
  `register(cls)`, `get_class(name)`, `create_bro(name, llm_spec=None)`, `list_classes()`, `known_names()` (every resolvable name, read without importing any bro module — what `ride/ride/bro_worker.py` validates summon targets against).
  A name is 1 to `MAX_NAME_LENGTH` characters.
  `create_bro` returns a fresh instance every call.
  `lineage(name)` is the names a bro answers to — its own plus every registered bro on its MRO, which is what `#may_summon`'s is-a membership tests against.
  `declared_specs()` is the one name source:
  `name -> "module:ClassName"` read from every installed distribution's `bro` entry points, metadata only, importing nothing
  — two distributions claiming one name raise rather than letting import order decide.
  Lookup is lazy:
  a miss imports only the single module that declaration names, so resolving one bro never pulls in another's dependency graph;
  only `list_classes()` (i.e. `bro list`) imports them all.
  Tests flip the module-level `_autoload` off to isolate the registry to hand-registered bros
- `show.py` — the `bro show` card, rendered from a bro's metadata alone:
  identity, features, credential manifest and optional tier, LLM key, and roster.
- `summon.py` (`summon`) — the bro wrapper over `launch {type: bro, …}` (the manual variant included), the facts a summoned run reads off its environment, and the summoning surfaces: blocking, detached, and manual;
  common launch enforcement lives in `ride/ride/launch_control.py`, and bro authorization in `ride/ride/bro_worker.py`
- `mission.py` (`mission`) — the universal typed outcome and conversation reads, say, ask, live artifact sharing, caller-scoped listing, cancellation surfaces, and ordered journal stream for every worker mission.
  The stream arms at the journal head, replays retained chat, follows events, and re-arms across a gap without repeating an entry it already yielded.
- `quest.py` (`quest`) — the bro-only view over the mission surface and its shared stream, preserving the summon-shaped answers, text chat, verbs, functions, and service tools
- `watches.py` and `watch_run.py` (`watch-run`) — one session-local watch store and its producer.
  `take()` commits fair, bounded batches under one exclusive lock;
  each line names its command, and a cut batch carries a pending marker.
  A producer's quiet line waits for the next waking line unless its watch is set to wake on quiet lines (`bro/reference/ride.md`, "Session watches").
  The store also reports whether waking lines remain undelivered, carries each stream's journal head, and remembers the live sets already noticed.
  `watch-run` executes each command line under `job_supervisor`, in brash under the policy its starter names or else in bash, detached from its starter but held by an owner-liveness handle.
  Managed sessions are owned by `do-ride`, while an in-process native `Runner` owns a temporary store.
- `turn_end.py` — `LineSink` and `TurnEnd`, the two harness ports for watch delivery and one-shot settlement, and the shared ordered verdict over missions, watches, undelivered waking lines, harness background work, and live session traffic.
  Settlement blocks on the store's journal signal until the session watch has emitted through the snapshot it reads.
  A distinct live set receives at most one shared notice;
  a second end with uncovered work ends the run, while covered work and work with a wake route keep it waiting.
- `artifact.py` (`artifact`) — peer-side artifact wire contract (the `artifact.mint`, `artifact.get`, and `artifact.share` kinds, the `sha256:` ref grammar, the canonical directory-manifest digest) plus the client and the CLI/session commands;
  `artifact_mcp.py` is the registered `artifact` toolset, reading and grepping reachable text refs in bounded windows;
  the host store and enforcement live in `ride/ride/artifacts.py`
- `job_supervisor.py` — the shared process-group supervisor behind core's `watch-run` and bro-native's jobs:
  it remains the live group leader until every command descendant exits, and exits when the owner-liveness handle closes.
  The native job registry, spool, and inbox keep their public `bro.jobs` and `bro.inbox` paths under `native/`.
- `brash_policy.py` — a session's brash policy file, written from a reach's finite command list and files level, and the argv a line starts as:
  brash under a policy, bash under `brash(ANY)`.
  A harness that runs lines in another process names the file there in `BRO_BRASH_POLICY`.
- `brash.py` (`brash`) — the standalone interpreter for a finite command list:
  quote-aware entry parsing, tree-sitter-bash validation, preflight and expanded-argv admission, the admitted shell language and builtins, word expansion, redirects classified by file reach, and status-126 refusals.
  It imports no other framework module, so each command-line start pays only for the interpreter.
- `shell.py` (`bro-shell-dir`) — validates the packaged shell helpers and prints their installed directory for shell consumers
- `worker_types.py` — the core contract for a worker type, its launch request and run shapes, peer descriptions, host ports, registry, and shared artifact/path helpers.
  `WorkerContainer` is the validated, host-neutral container declaration:
  packaged build-context bytes over the runtime image, a command and environment, and container ports mapped to requested-or-available loopback host ports.
  Installed types register through `bro.worker_types`, with ride contributing `bro`, bench contributing `benchmark`, and webview contributing `webview`.
- `run_lifecycle.py` — `RunLifecycle`, the worker-process emitter over `bro.broker.client.Client`:
  it undertakes the broker mission named in `BROKER_MISSION`, emits the run's set-once `trail` mark after recording opens, and sends the closing result.
  `Runner.run()` builds one through `_make_channel()`;
  a summoned interactive native run emits its trail on the first `send`, while an un-summoned conversation emits nothing.
  The shared answer bound is enforced at the `answer` tool while the run can react;
  terminal fallback output is truncated with a marker and its trail id.
  `close()` confirms delivery through `ClientTransport.close(confirm=True)`.
- `base/` — shared low-level utilities;
  see `base/AGENTS.md`
- `brog/` — the task-tracker facade behind the `brog` MCP namespace;
  see `brog/AGENTS.md`
- `broker/` — the consumer-neutral host↔peer messaging substrate;
  see `broker/AGENTS.md`
- `datasources/` — the `DataSource` ABC and the read-only connectors;
  see `datasources/AGENTS.md`
- `extra/github/` — the GitHub API client (`api.py`), the GitHub App authentication source (`app.py`), and pull-request reads (`pulls.py`)
- `harness/` — the framework half of a driving harness and what a consuming harness brings of its own.
  `__init__.py` holds the `Harness` interface and the `bro.harnesses` registry:
  Installed names come from metadata without imports, and one selected object loads lazily with its type and name checked.
  Its typed facts supply prompt capabilities and passages, and terminal service-tool results pass through its session-ending methods.
  `Harness.serve(reach)` returns a `Service`:
  the server specs the harness mounts for the reach's tool groups, and the groups it leaves unserved, which `bro show` marks.
  The tools a harness serves a group with on its own, Claude's natives or the bro harness's job tools, are its own code, built from the same reach.
  `watches.py` separately decides whether `do-ride` arms the runtime-owned session watch, which reaches the model through the harness's delivery port rather than a tool it can call
- `launch/` + `native/bro/launch/` — core owns cross-harness launch primitives;
  `bro-native` owns the in-process `run` / `chat` launchers, chat UIs, and fork-resume flow.
  The directories are portions of the shared `bro.launch` namespace;
  see `launch/AGENTS.md`
- `llm/` — the provider-neutral declaration and shared-contract layer:
  `LLMSpec` recipes and provider selection, the live MCP tool/server seam engines consume, observers and trackers, token-usage accounting, and `mu`, the typed one-shot call helper an extension calls directly;
  see `llm/AGENTS.md`
- `monitor/` — session-local monitoring paths and signals shared across package boundaries:
  the recording health file (`health.py`) and a managed session's current-trail pointer (`trail_pointer.py`)
- `prompts/` — the prompt store;
  see `prompts/AGENTS.md`
- `reference/` — the reference docs that ship in the wheel (`extending.md`, `conditions.md`, `template.md`, `ride.md`, `dive_in.md`), served to bros on demand through `datasources/references.py`
- `runtime/` — fronts that compose lower-level packages into runnable services;
  see `runtime/AGENTS.md`
- `setup/` — bringing up a checkout, and the credential and host-config schemas;
  see `setup/AGENTS.md`
- `trails/` — the recording pipeline;
  see `trails/AGENTS.md`
- `workspace/` — the dependency-light workspace contracts core services read;
  see `workspace/AGENTS.md`
- `bros/bro/` — the core distribution's sole concrete persona, inside the shared PEP 420 `bros` namespace.
  It defines `Bro(BaseBro)`, the minimal bro registered as `bro`, with a minimal go-to system prompt and no MCP servers.
  Bros normally inherit from this `Bro`, so they pick up the shared defaults via the MRO walk;
  inherit from `BaseBro` only when opting out of those defaults is the persona's point.
  It owns the shared spells inherited by the concrete-Bro family (`bros/bro/spells/`):
  `spell::ask` — the summon UX:
  phrasing → target + self-contained prompt, least-authority talk rights, client pick (the `summon` and `quest` CLIs vs the `summon` and `quest_*` service tools), foreground-vs-background, the question/reply/check loop, and failure relay;
  protocol and enforcement live in `bro/summon.py`, `bro/mission.py`, `bro/quest.py`, `ride/ride/launch_control.py`, and `ride/ride/bro_worker.py`, not in the spell
  — `spell::reflect` — the improving half of the loop over what a bro runs under:
  it reads recorded runs against the definition that drove them (the prompt texts, the bro's declaration, the launch scope) and writes its next version, each edit fixed in place or filed as a task
  — and `spell::watch` — keeping an admitted command's lines in view through `bro::watch`, ending turns while idle, and stopping it through `bro::unwatch`.
  Development personas ship from `bro-dev`;
  `dev/AGENTS.md` maps them.
  Consumer personas register through the `bro` entry-point group and live in their contributing packages.

## Running a declaration

How to declare a bro is `bro/reference/extending.md`;
this section is what `BaseBro` renders, mounts, and counts when one runs.

### Tool reach

`BaseBro.reach()` folds the class's `tools` into one harness-neutral `Reach` (`bro/reference/extending.md`, "Declaring a bro").
A harness mounts, for a bro, the servers its `serve` returns for that reach ahead of the reach's own servers and data sources, built once per harness on the bro instance and released by `close()`.

### Prompt composition

`BaseBro.__init__` keeps the MRO-concatenated class prompts as `persona`, under a `# Persona: <name>` heading.
`composed_prompt(harness)` composes a system prompt around that persona using the explicitly selected harness.
The composition starts with every `bro/prompts/shared/*.md`, then the persona and the tool-name rule (`bro/prompts/tool_names.md`).
It adds a `## Data sources` block describing each declared `DataSource`, the `## Spells` contract when the bro has spells, and any instructions the selected harness supplies through `prompt_instructions()`.
The composition renders with the registered harness object's own facts, the environment's credentials and `launch.bro.bros` members, and the `#features` vocabulary.
Construction loads no harness and renders no composed prompt.
A managed Claude session runs under a prompt of its own;
its append prompt injects `persona` beside the shared prompts with the registered Claude harness (`bro/reference/ride.md`, "Auto-injected system prompt").
`system_prompt_for(hold=…, harness=…)` adds the hold and session fragments to the selected harness's composed prompt:
the fragments and hold text are described in `bro/prompts/AGENTS.md`, "Session fragments".

### Service tools

Every assembly (`assemble(harness, …)`) receives the engine's registered `Harness` object and appends the `bro` service server.
`_build_service_server` decides core's roster from the surface and the process environment, then appends `Harness.own_tools` and renders the combined roster as the `#tools` universe:

- `banner`, always:
  the session facts of `ride banner --llm` rendered in-process by `bro.workspace.banner.render_banner`, with the bro's name and the run's trail id passed explicitly since an in-process run's environment carries the launcher's;
  the playbook is `bro/prompts/environment.md`.
- `raise`, at the unattended hold alone
  — `Runner.run()`'s default, `bro run`, summoned children, and the Claude builds when `do-ride` exports `BRO_HOLD=unattended` with `RIDE_RUNNER_PID`:
  the agent aborts with a reason when the request cannot be fulfilled, through the selected harness's `end_session`.
  The bro harness raises `BroRaised(reason)` out of `Runner.run()`;
  the Claude harness emits the run's failed result over the broker channel where one exists, then terminates the session through `bro.workspace.session.terminate_session` with `RAISE_EXIT_STATUS` as its status,
  since no exception can abort the consuming Claude session.
  Every other hold mounts no `raise`;
  its hold text tells the agent how to involve the human instead.
- `answer`, `raise`'s twin for a summoned run's clean end, mounted when the run is summoned (`RIDE_SUMMONED`) with broker intent and the selected harness's `can_end_session()` holds:
  it validates the answer and passes the result to `end_session`;
  the bro harness raises `AnswerDelivered`, which the runner or chat surface turns into the run's ok result;
  the Claude harness emits that result over the channel then terminates the session, and unlike `raise` an undeliverable answer errors back to the agent.
- `cast` when the bro has spells and its key resolves (`bro/reference/ride.md`, "Bro spells and skills").
  The bro harness contributes `skill` through its own tools because its model has no native skill loader.
- `watch` and `unwatch` with a declared `brash` command list on every harness;
  `watch` starts its line in brash under the live run's policy, or the published one where the process runs none, or in bash under `brash(ANY)`;
  `unwatch` stops the named producer without checking the command list and refuses the runtime-owned session watch.
- the bro harness's own tools:
  `job`, `poll`, `kill`, and `jobs` over its run's registry and inbox wherever the declaration carries a `brash` command list, plus `skill` on every bro-harness run.
- `summon` and the quest verbs (`quest_check`, `quest_history`, `quest_say`, `quest_ask`, `quest_share`, `quest_list`, `quest_cancel`) when the process has broker intent (`BROKER_CHANNEL`, or `BROKER_UPSTREAM` left by a failed proxy launch),
  forwarding to `bro.summon` and `bro.quest` off-loop.

The quest tools have one shape on every harness and return at once:
`summon` on host acceptance, `quest_ask` with a minted question id, `quest_check` and `quest_history` after one journal read, `quest_share` when the host accepts the ref, and `quest_cancel` when the host accepts.
Later transitions arrive through the runtime-owned session watch.

### Credential manifest

`bro.needed_secrets(harness)` is the bro's component credential manifest:
the union of `needed_secrets` over the components that harness mounts for the bro,
which are the servers it serves the bro's tool groups with and the bro's own declared servers and data sources (matching what `ride.claude.assembly.persona_servers()` mounts for `claude`);
the MRO-collected `extra_secrets`;
and the credentials of its pinned-on features.
It excludes the LLM key (`llm_spec.needed_secrets()`):
surfaces that run the bro as an LLM process add it, while a claude-code session authenticates on its own.
`missing_secrets()` is the manifest plus the LLM key, checked against the process's store;
`Runner.run()` refuses to start on any missing name, raising `BroRaised` with them all listed, and interactive surfaces reply with the report.
The host hydrates the per-surface set into a scoped store before a session starts (`bro/reference/ride.md`, "Session permissions and credentials").

### Optional credential tier

`bro.optional_secrets(harness)` is the manifest's best-effort sibling:
each declared component's `optional_secrets`, the credentials of the bro's gated features, and the cast key when the bro has spells, minus `needed_secrets()` so a hard requirement is never downgraded.
It holds credentials a capability uses when present and degrades without, such as the LLM key behind a `SearchableDataSource`'s query-focused summary.
Hydration skips an optional kind only when no layer picked it and its empty instance is absent;
a present or explicitly picked name must load, and `bro.base.credentials.available(name)` is the resolution predicate behind runtime gates and feature directives in static text.
