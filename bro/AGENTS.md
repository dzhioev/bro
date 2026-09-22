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
  How a declaration composes and what a run mounts: "The bro declaration" below.
- `mcp.py` — declaration vocabulary for persona tool layers and toolset modules:
  surface facts and rendering, `MCPServerSpec`, `ToolLayer`, `Toolset`, and the `mount` / `block` / `allow_commands` / `serve` / `cli` / `shell` constructors.
  Live tool and server objects stay in `llm/mcp.py` and are imported only when a declaration is built
- `spells.py` — the spell store:
  validation of the files a bro's `spells` declaration names and the reserved `spell` MCP namespace they are served under, with `bro::cast` and the native `bro::skill` loader ("Spells and skills" below).
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
- `quest.py` (`quest`) — the outcome and conversation reads, say, ask, and caller-scoped listing over bro quests, plus the ordered watch and cancellation surfaces shared by every mission type
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
  protocol and enforcement live in `bro/summon.py`, `bro/quest.py`, `ride/ride/launch_control.py`, and `ride/ride/bro_worker.py`, not in the spell
  — and `spell::reflect` — the improving half of the loop over what a bro runs under:
  it reads recorded runs against the definition that drove them (the prompt texts, the bro's declaration, the launch scope) and writes its next version, each edit fixed in place or filed as a task.
  Development personas ship from `bro-dev`;
  `dev/AGENTS.md` maps them.
  Consumer personas register through the `bro` entry-point group and live in their contributing packages.

## The bro declaration

What `BaseBro` makes of a declaration:
how the class-level attributes compose along the MRO, which prompt flavor each surface renders, what a run mounts and gates on, and how credentials are counted.
How to declare one is `bro/reference/extending.md`.

### Declaration and composition

Subclasses set `name`, `description`, and class-level `system_prompt = "..."` plus optionally `data_sources = [...]`, `tools = [...]`, `spells = (...)`, and `provisioning = (...)`.
Every `tools` item is one frozen `bro.mcp.ToolLayer`:
`mount(toolset, *tool_names)` adds a toolset's full or selected roster,
`cli('<command>', *argument_names)` generates one from an installed CLI command (`bro/llm/cli_tool.py`),
`block(*tool_names)` names harness-native tools to remove,
`allow_commands(tool_name, *commands)` serves a blocked native tool whose argument is a command line, reaching only the named commands,
and `serve(*tool_names)` serves blocked native tools whole, for those with no command line to narrow on.
Tool-pack modules export a typed `toolset`;
the module itself is not a declaration.
Entries in `tools` and `data_sources` may be wrapped with `bro.base.condition.when(...)` to gate them on the assembling surface's facts, e.g. `when(harness == 'bro', mount(dev_mcp.toolset))` because Claude has built-in file and search tools,
or `when(harness == 'claude', block('Read', 'Write'))` to restrict those built-ins.
A block selected for the `bro` harness is a declaration error because bro-native and raw Claude surfaces already serve exactly the declared roster.
Conditions are decided where composition happens (`bro.mcp.select`, harness `bro`), so an unmatched layer is never applied (conditioning reference: `bro/reference/conditions.md`).
Live servers are built lazily, once, when the bro first runs tools (`_live_mcp_servers()`), so metadata surfaces (credential scoping, prompt composition, `bro show`) never construct them and a live server's constructor may hold real resources
— released through `BaseBro.close()` when the run's lifetime ends, so a session leaves no process of its own behind.

`BaseBro.__init__` walks the MRO from base to most-derived class and concatenates each class's own `tools`, `data_sources`, and `system_prompt`.
A `ReviewDev(Dev)` subclass declares only what it adds;
Dev's own entries flow through automatically, and the `features` map merges along the same walk with derived classes overriding per name.
The legacy `super().__init__(system_prompt=...)` escape hatch remains for callers that need a dynamic prompt computed at instantiation;
a bro that wants the current time can instead pull it through the `CurrentTime()` data source.

### Features

`features` is the class-level map of named optional capabilities
— feature name → the gate deciding whether it's on:
a `Condition` over the environment's resolvable credentials (`creds.contains('brog')`) or a plain bool constant (`True` pins it on; `False` disables it terminally — a descendant re-enabling a disabled feature fails construction).
The declaration feeds a `#features` vocabulary (`BaseBro.vocabulary()`) into every surface the bro renders or assembles, so `when(feature('brog'), mount(brog_mcp.toolset))` entries and `{{iff #features contains brog}}` text switch together,
and a gated component joins `needed_secrets()` only where its gates resolve.
The credential a gate probes is the feature's own, tiered with it:
in `optional_secrets()` while the feature is gated, in `needed_secrets()` once a descendant pins the feature on.
`bro-dev`'s Dev persona is the built-in example:
the tracker toolset, task-workflow spell branches, and tracker prompt fragment all ride it.
Semantics:
`bro/reference/conditions.md` "Bro features".

### Prompt flavors

`BaseBro` also auto-prepends every `bro/prompts/shared/*.md` to the system prompt and appends a `## Data sources` block describing each declared `DataSource`.
When the bro has any namespaced tools or spells it also appends the tool-name rule (`bro/prompts/tool_names.md`, templated on the `#wire` scheme),
and closes the prompt with the tool-grounding fragment (`bro/prompts/grounding.md`), whose directives render its body only into the claude-bare flavor (see `bro/prompts/AGENTS.md`).
Each composed flavor renders once with its surface facts (`bro.mcp.render_text`: harness `bro`, the flavor's wire, the environment's credentials and summon allow-list)
— the wire is where the flavors diverge:
`system_prompt` (bro-native LLM runs) renders `bare` (`namespace::tool` resolves to the wire name `namespace__tool` the bro's tool list carries),
while `claude_system_prompt` (what `ride solo|along --raw` passes as claude's `--system-prompt`) renders `mcp`, because there each namespace is mounted as an MCP server and the wire names are `mcp__namespace__tool`.
The bro's own class prompts (MRO-concatenated, before those additions) are kept as `persona`;
`ride/ride/claude/system_prompt.py:session_append_prompt` injects it plus the shared spell instructions into every managed Claude session (the mode verb's bro positional)
so it carries that bro's policies and canonical spell invocation contract without running under `--raw`.

### LLM spec

`llm_spec` is a class-level attribute holding the bro's `NativeLLMSpec`
— a provider-specific frozen dataclass (e.g. `bro.llm.llms.openai.LLMSpec` with typed `model`, `reasoning_effort`, `service_tier`) that carries the knobs the LLM accepts and validates them in `__post_init__`.
Default is `DEFAULT_LLM_SPEC`, an `openai.LLMSpec`;
each bro overrides the whole spec at class level.
Construction-time overrides go through `BaseBro.create(spec)`
— it instantiates the class then replaces `llm_spec`, so subclass constructors never have to forward anything.
Specs also expose `dump()` → dict / `LLMSpec.from_dict(data)` for serialization round-trip;
`from_dict` eagerly imports the known providers so dispatch works in processes that haven't pulled the provider module in themselves.

The declaration is the bro-native recipe
— the one an LLM process runs the bro under, which is what `NativeLLMSpec` narrows the attribute to.
A Claude Code session is the other kind of recipe (`bro.llm.llms.claude_code`), carrying the model and knobs the session itself runs under;
the claude surfaces resolve their own rather than reading one off the bro.
Either kind is named at launch by `--provider` / `--model` / `--llm`
— roster and grammar in `bro/llm/AGENTS.md` (`providers.py`), the per-surface flags in `bro/launch/AGENTS.md` ("Naming the LLM").

### Spells and skills

A bro's `spells` declaration names its procedure files under `<bro_pkg>/spells/`, and the roster composes by MRO with derived overrides.
A spell's flat frontmatter may declare a one-line JSON parameter map (`parameters: {"task": "task ref", "notes?": "optional context"}`);
the `?` suffix marks an optional string parameter.
`bro/spells.py` validates the store and builds the reserved `spell` MCP namespace on every surface with spells;
each spell is one canonical `spell::<name>` tool, and its full frontmatter description stays on that tool.
When OpenAI resolves, the `bro` service server also mounts `bro::cast`:
a prompt-backed structured `mu` call interprets free text against the spell roster and returns the resolved spell's rendered instructions with the interpreted arguments, or an expected error.
The composed `## Spells` block always carries the `[[…]]` marker contract
— the enclosed text names a spell in whatever form its sentence takes, run only where the sentence asks for it
— and forks on the run path alone:
`bro::cast` where it is mounted, the spell's own tool otherwise.
Bro-native and `ride --raw` builds additionally mount `bro::skill(name)` because those harnesses have no native third-party skill loader;
its current skill set is empty, so valid calls return an empty body.
Their composed `## Skills` block maps third-party `/<name>` requests to that loader.
An ordinary managed Claude persona session keeps Claude's own skill mechanism and mounts neither the framework loader nor spell-to-`SKILL.md` adapters.
`bro show` lists only the bro's spell roster.

### Run-start credential gate

A run refuses to start when any name in `missing_secrets()`
— the manifest plus the LLM key
— doesn't resolve, listing all missing names:
`Runner.run()` raises `BroRaised`, interactive surfaces reply with the report.
The optional tier stays best-effort.

### Interactive vs non-interactive paths

Runs at the unattended hold
— `Runner.run()`'s default, `bro run`, and summoned children
— expose a built-in `raise` service tool
— the agent calls it to abort with a reason when the request cannot be fulfilled (missing credentials, no appropriate tool, contradictory constraints, unclear/uninterpretable input);
the call raises `BroRaised(reason)` out of `Runner.run()`.
The Claude adapter builds (`ride.claude.assembly.bro_servers` / `persona_servers`) mount `raise` too when the session is unattended and killable (`BRO_HOLD=unattended` + `RIDE_RUNNER_PID` in the environment
— `do-ride` exports them), in the tool's mcp-wire flavor:
no exception can abort the consuming claude session,
so the call emits the run's `result{failed, reason: raised}` over the broker channel where one exists,
then terminates the session via `bro.workspace.session.terminate_session` (SIGTERM to `do-ride`,
which ends claude the way a user's own interrupt would,
so the closing turn reaches the transcript before it goes
— `ride/ride/claude/interrupt.py`
— same delegation shape as `banner` → `bro.workspace.banner.render_banner`), naming `RAISE_EXIT_STATUS` as the status the session reports, so an abort looks the same to whoever launched it whichever harness ran the bro.
The abort stays machine-readable after the fact through the recorded trail
— the raise call with its reason is the transcript's last record, and `ride.claude.trail_recorder` ends the session's trail as `raised` with that reason as `end.detail`.
`BaseBro.system_prompt_for` appends the matching session fragments, passing `bro.summon.talk()` so the summoned contract renders only the quest's permitted chat moves.
It then appends the hold fragment via `bro.prompts.hold_fragment` (`run()` defaults to unattended, `send()` to guided, and every launch surface overrides per its `--hold`
— defaults in `bro/launch/AGENTS.md`, "Display and holds";
the level files are documented in `bro/prompts/AGENTS.md`, "Hold text"), so the hold is injected at run start, never runtime-detected.
Every other hold mounts no `raise`
— the injected fragment tells the agent how to involve the human instead, down to guided's ask-clarifying-questions convention on the interactive paths (`Runner.send()` and the `bro chat` CLI).
`answer` is `raise`'s twin for a *summoned* run's clean end
— mounted when the run is a summoned child (`RIDE_SUMMONED`) with broker intent (`BROKER_CHANNEL`, or `BROKER_UPSTREAM` left by a failed proxy launch; mcp flavor additionally killable):
the bare flavor ends the run by raising `AnswerDelivered` (the runner or chat surface turns it into the run's ok result), the mcp flavor emits that result over the channel then terminates the session;
unlike raise, an undeliverable answer errors back to the agent instead of killing the session.
Both service builds always mount a `banner` tool
— the session environment facts of `ride banner --llm` rendered in-process (`bro.workspace.banner.render_banner`, with the bro's name and the run's trail id passed explicitly
— an in-process run's environment carries the launcher's `RIDE_BRO`, or none, and its own trail is published by no session recorder), so every bro detects its environment without a shell;
the playbook is `bro/prompts/environment.md`.
On the bro harness, a declared `shell` roster mounts `job`, `poll`, `kill`, and `jobs` on both wires, plus `chill` on the bare wire.
Bare tools use the run's registry and inbox;
the MCP service server owns and closes its registry, offers only foreground/background jobs, and has no notification wake.
With no shell declaration, automatic `quest watch` admission mounts the same bare tools narrowed to that command alone.
Both service builds also mount `summon` and the quest verbs `quest_check`, `quest_history`, `quest_say`, `quest_ask`, `quest_list`, and `quest_cancel` when the process has broker intent (`BROKER_CHANNEL` or `BROKER_UPSTREAM` set),
forwarding to `bro.summon` and `bro.quest` off-loop so interactive surfaces stay responsive.
The bare-wire shapes do not wait:
`summon` returns after host acceptance and has no `detach`, `quest_ask` mints a question id without waiting, `quest_check` and `quest_history` are one journal read each, and `quest_cancel` returns when the host accepts the cancellation.
Their later chat and lifecycle transitions arrive through `quest watch`, and the host-retained outcome and conversation remain readable by id.
`quest_list` walks the journal's paginated caller-scoped listing and returns its summon records live-first on both wires.
The MCP-wire shapes retain the blocking controls:
`summon` may wait for an answer or question, `quest_ask` may wait for the reply, `quest_check(wait=true)` long-polls to the end or a child question, `quest_history(wait=true)` to the next message, and `quest_cancel` may wait for the quest to end.
Those blocking modes own their per-call channel client and close it on cancellation, which unblocks the current short broker wait.
Their descriptions carry a `{{when #wire = mcp}}` transport-caution block, rendered at service-server build
— service tools are harness features, the one tool surface whose rendering vocabulary gets the system `#wire` fact injected next to the `#tools` roster.
The MCP-served builds (`wire == 'mcp'`: persona and `--raw` claude sessions, consumed over streamable HTTP with a client-side call budget) steer long runs to detach plus repeatable polling;
their lost-id recovery wording retains the `{{iff #tools contains quest_list}}` roster fork.

### Credential manifest

`bro.needed_secrets()` is the bro's component credential manifest
— the union of each declared MCP server spec's and data source's `needed_secrets`, the bro's MRO-collected `extra_secrets`, and the credentials of its pinned-on features.
It is harness-aware:
`needed_secrets(harness='claude')` counts only components whose conditions hold on that harness, matching what `ride.claude.assembly.persona_servers()` mounts.
It deliberately excludes the LLM key (`llm_spec.needed_secrets()`):
surfaces that run the bro as an LLM process add it, while a claude-code session uses its own authentication.
Components declare credentials through pure metadata (`WebSearch` → `brave`; `openai.LLMSpec` → `openai`);
`extra_secrets` is the escape hatch for environment needs no component expresses.
The host hydrates the per-surface set into a scoped credential store before a container starts, and missing required secrets fail on the host.
Full mechanics:
`bro/reference/ride.md` ("Scoped credential hydration").
`bro show` renders the features, manifest, optional tier, LLM key, and per-surface baseline note.

### Optional credential tier

`bro.optional_secrets()` is the best-effort sibling of the manifest
— the union of each declared component's `optional_secrets`, the credentials of the bro's gated features, and OpenAI when the bro has spells, minus `needed_secrets()` so a hard requirement is never downgraded.
It is for credentials a capability uses when present but degrades without, such as the LLM key behind a `SearchableDataSource`'s query-focused summary or spell casting.
The host hydrates it via `build_scoped_store(optional=...)`;
resolvable names are materialized and absent optional names are skipped.
`bro.base.credentials.available(name)` is the presence predicate behind runtime gates and feature directives in static text.
