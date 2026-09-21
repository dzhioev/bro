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
- `mcp.py` — declaration vocabulary for persona tool layers and toolset modules:
  surface facts and rendering, `MCPServerSpec`, `ToolLayer`, `Toolset`, and the `mount` / `block` / `allow_commands` / `serve` / `cli` / `shell` constructors.
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
- `mission.py` (`mission`) — the universal typed outcome and conversation reads, say, ask, caller-scoped listing, ordered watch, and cancellation surfaces for every worker mission
- `quest.py` (`quest`) — the bro-only view over the mission surface, preserving the summon-shaped answers, text chat, verbs, functions, and service tools
- `artifact.py` (`artifact`) — peer-side artifact wire contract (the `artifact.mint` / `artifact.get` kinds, the `sha256:` ref grammar, the canonical directory-manifest digest) plus the client and the CLI/session command;
  the host store and enforcement live in `ride/ride/artifacts.py`
- `jobs.py`, `job_supervisor.py`, and `inbox.py` — process jobs and the per-run notification seam:
  a supervisor remains the live process-group leader until every command descendant exits;
  merged output drains into a memory-bounded temporary spool, head and tail consumers share one cursor, and the exit is consumed once;
  an inbox wait only observes the set of jobs with news and the framework notices posted to it, while its drain renders and consumes their bounded notification slices.
- `shell.py` (`bro-shell-dir`) — validates the packaged shell helpers and prints their installed directory for shell consumers
- `worker_types.py` — the core contract for a worker type, its launch request and run shapes, peer descriptions, host ports, registry, and shared artifact/path helpers.
  `WorkerContainer` is the validated, host-neutral container declaration:
  packaged build-context bytes over the runtime image, a command and environment, and container ports the host publishes on loopback.
  Installed types register through `bro.worker_types`, with ride contributing `bro` and bench contributing `benchmark`.
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
- `harness/` — what a consuming harness brings of its own, named where a persona can declare against it.
  `claude.py` holds Claude Code's tool names in capability groups (`FILES`, `SHELL`, `DELEGATION`) plus `claude.block(*names)`, conditioned on the Claude harness.
  A finite `shell(...)` roster over a blocked shell hands back `Bash` and `Monitor` behind the command gate plus their job controls;
  `shell(ANY)` leaves an unblocked Claude shell unrestricted.
  `quest watch` needs no declaring:
  for a run that may summon, or a summoned run whose talk lets its summoner say or question or lets the run itself ask, the fold admits it through `Monitor` over any block or narrowing of that tool.
  A persona names another product's tool surface when it withholds or narrows one, so the names live here rather than in each persona that forgoes them
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
  — and `spell::reflect` — the improving half of the loop over what a bro runs under:
  it reads recorded runs against the definition that drove them (the prompt texts, the bro's declaration, the launch scope) and writes its next version, each edit fixed in place or filed as a task.
  Development personas ship from `bro-dev`;
  `dev/AGENTS.md` maps them.
  Consumer personas register through the `bro` entry-point group and live in their contributing packages.

## Running a declaration

How to declare a bro is `bro/reference/extending.md`;
this section is what `BaseBro` renders, mounts, and counts when one runs.

### Prompt flavors

`BaseBro.__init__` keeps the MRO-concatenated class prompts as `persona`, under a `# Persona: <name>` heading, and composes two full flavors around it:
every `bro/prompts/shared/*.md` first, then the persona, the tool-name rule (`bro/prompts/tool_names.md`), a `## Data sources` block describing each declared `DataSource`,
the `## Spells` contract when the bro has spells, the `## Skills` block mapping `/<name>` requests to `bro::skill`,
and last the grounding fragment (`bro/prompts/grounding.md`), whose own directives render it only for the claude-bare surface.
Each flavor renders once with its surface facts (`bro.mcp.render_text`: harness `bro`, the flavor's wire, the environment's credentials and summon allow-list, the `#features` vocabulary), and the wire is where they diverge:
`system_prompt` renders `bare`, the wire of bro-native LLM runs;
`claude_system_prompt` renders `mcp`, what `ride solo|along --raw` passes as claude's `--system-prompt`, where each namespace is mounted as an MCP server.
A managed Claude session runs under neither flavor;
its append prompt injects `persona` beside the shared prompts (`bro/reference/ride.md`, "Auto-injected system prompt").
`system_prompt_for(hold=…)` is the text a bro-native run starts under:
the `bare` flavor plus the session fragments and the hold text (`bro/prompts/AGENTS.md`, "Session fragments").

### Service tools

Every assembly (`assemble(harness, wire, …)`) appends the `bro` service server;
`_build_service_server` decides its roster from the surface and the process environment:

- `banner`, always:
  the session facts of `ride banner --llm` rendered in-process by `bro.workspace.banner.render_banner`, with the bro's name and the run's trail id passed explicitly since an in-process run's environment carries the launcher's;
  the playbook is `bro/prompts/environment.md`.
- `raise`, at the unattended hold alone
  — `Runner.run()`'s default, `bro run`, summoned children, and the Claude builds when `do-ride` exports `BRO_HOLD=unattended` with `RIDE_RUNNER_PID`:
  the agent aborts with a reason when the request cannot be fulfilled.
  The bare flavor raises `BroRaised(reason)` out of `Runner.run()`;
  the mcp flavor emits the run's failed result over the broker channel where one exists, then terminates the session through `bro.workspace.session.terminate_session` with `RAISE_EXIT_STATUS` as its status,
  since no exception can abort the consuming claude session.
  Every other hold mounts no `raise`;
  its hold text tells the agent how to involve the human instead.
- `answer`, `raise`'s twin for a summoned run's clean end, mounted when the run is summoned (`RIDE_SUMMONED`) with broker intent, and on the mcp wire only where `RIDE_RUNNER_PID` makes the session killable:
  bare raises `AnswerDelivered`, which the runner or chat surface turns into the run's ok result;
  mcp emits that result over the channel then terminates the session, and unlike `raise` an undeliverable answer errors back to the agent.
- `cast` when the bro has spells and its key resolves, and `skill` on the harnesses without a native skill loader (`bro/reference/ride.md`, "Bro spells and skills").
- the job tools of a declared `shell` roster, on the bro harness alone
  — `job`, `poll`, `kill`, and `jobs` on both wires, `chill` on bare;
  with no `shell` declared, automatic `quest watch` admission mounts the same tools narrowed to that command.
  Bare tools use the run's registry and inbox;
  the MCP service server owns and closes its registry, offers only foreground and background jobs, and has no notification wake.
- `summon` and the quest verbs (`quest_check`, `quest_history`, `quest_say`, `quest_ask`, `quest_list`, `quest_cancel`) when the process has broker intent (`BROKER_CHANNEL`, or `BROKER_UPSTREAM` left by a failed proxy launch),
  forwarding to `bro.summon` and `bro.quest` off-loop.

The wires differ in waiting.
The bare shapes return at once
— `summon` on host acceptance with no `detach`, `quest_ask` with a minted question id, `quest_check` and `quest_history` after one journal read, `quest_cancel` when the host accepts
— and later transitions arrive through `quest watch`.
The MCP shapes keep the blocking controls
— `summon` waits for an answer or question, `quest_ask` for the reply, `quest_check(wait=true)` to the end or a child question, `quest_history(wait=true)` to the next message, `quest_cancel` to the end
— each owning a per-call channel client closed on cancellation;
their descriptions carry the `{{when #wire = mcp}}` transport caution that steers a long run to detach plus polling.

### Credential manifest

`bro.needed_secrets(harness)` is the bro's component credential manifest:
the union of each declared MCP server spec's and data source's `needed_secrets` over the components whose conditions hold on that harness (matching what `ride.claude.assembly.persona_servers()` mounts for `claude`),
the MRO-collected `extra_secrets`, and the credentials of its pinned-on features.
It excludes the LLM key (`llm_spec.needed_secrets()`):
surfaces that run the bro as an LLM process add it, while a claude-code session authenticates on its own.
`missing_secrets()` is the manifest plus the LLM key, checked against the process's store;
`Runner.run()` refuses to start on any missing name, raising `BroRaised` with them all listed, and interactive surfaces reply with the report.
The host hydrates the per-surface set into a scoped store before a session starts (`bro/reference/ride.md`, "Scoped credential hydration").

### Optional credential tier

`bro.optional_secrets(harness)` is the manifest's best-effort sibling:
each declared component's `optional_secrets`, the credentials of the bro's gated features, and the cast key when the bro has spells, minus `needed_secrets()` so a hard requirement is never downgraded.
It holds credentials a capability uses when present and degrades without, such as the LLM key behind a `SearchableDataSource`'s query-focused summary;
absent optional names are skipped at hydration, and `bro.base.credentials.available(name)` is the presence predicate behind runtime gates and feature directives in static text.
