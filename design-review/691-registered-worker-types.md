# broker: registered worker types over one launch kind, with the bro as one of them

> The `## Design` section of [#691](https://github.com/dzhioev/bro/issues/691), placed on a throwaway branch so it can be reviewed as a document on a pull request.
> The task page is the single source of truth;
> this file and its branch are discarded once the review is folded into the page.

## Design

Settled with the user on 2026-09-20 (trail `01m2y64m9k-8z5q4ybv-m04bzcez`), which also brought `## Goal`, `## Shape`, and `## Worth knowing` to the vocabulary decided here:
the unit is a **mission**, its two ends the **owner** and the **worker**, and the bro's party permits are spelled `:bro.party.*`.
Reviewed against the code the same day (trail `01m2zfsq0y-sm52tfdy-yjt1jjxd`);
what the review changed and why is in `## Design changelog`.

### Vocabulary

A *mission* is one unit of work an owner gives and exactly one worker undertakes.
A `launch` request opens it and its id is that request's id.
Its parent is the mission its owner undertakes, so a ride's missions form a tree under the root's host-anchored one.
It is accepted, started, chatted on, and ended exactly once, completed or failed with a reason.
A worker's death ends its mission if nothing else did, an owner's death orphans the missions it owns, and `cancel {id}` ends one for its owner.
A refused launch opens none;
the journal keeps the terminal record of the refusal.
The *owner* is the peer that gave the mission.
The *worker* is the peer undertaking it:
a bro session, a benchmark job, a webview.
The broker's words are mission, owner, worker, type, launch, and request.
It never says quest, summon, summoner, summoned, or bro.
The bro workflow keeps *quest* for a bro's mission on every surface it owns, and its people stay the summoner and the child.

### The substrate: `bro.broker`

**Wire (`brotocol.py`).**
`request {id, payload: {kind, args}}` is unchanged.
`mark`, `message`, and `result` carry the request they belong to in a `request` field (today `quest`);
`Message.quest` and `Message.quest_id` become `request` and `request_id`, and the builders take a `request_id`.
The chat ends are `owner` and `worker`.
The talk rights are `owner.say`, `owner.question`, `worker.say`, `worker.question`, and a journaled chat entry carries `from: owner|worker`.
`PROTOCOL_REVISION` goes to 5, so a client at the old revision is refused at attach with both values named.

**Environment (`environment.py`, new).**
The broker owns its names:
`BROKER_CHANNEL`, `BROKER_UPSTREAM`, `BROKER_MISSION`, and `BROKER_TALK`.
`BROKER_MISSION` is the mission this process undertakes, the value `BROKER_QUEST` carries today, and `self` still names it on every surface.
`bro/launch/broker_environment.py` goes.
`broxy_log_path` moves to `bro/launch/broxy.py`, which already logs the path when the proxy fails to launch, and `Client.from_env` reports a failed session proxy without naming it.
The other importers (`ride/ride/session.py`, `ride/ride/workspace/spawn.py`, `ride/ride/workspace/containers.py`, `bench/bro/bench/run.py`) read the names from the new module,
and the modules spelling the strings today (`ride/ride/root.py`, `bro/bro.py`'s broker-intent check, `ride/ride/claude/statusline.py`) read them there too.
The module holds constants only, and the pre-gate launch path may import it:
`broker_enabled` (`ride/ride/workspace/containers.py`) keeps the `BROKER_DISABLED` kill-switch and drops its `ImportError` degrade:
under the runtime bundle ride and bro are one installation, so an environment that cannot import the broker cannot exist.
E2e scenario D (`TestBrokerUnimportable`) goes with it, and the lazy-import invariant in `ride/ride/workspace/AGENTS.md` becomes one of cost:
nothing before the gate imports broker machinery (`ride/ride/workspace/spawn.py`, the dispatcher), while the constants module is free.

**Boundary.**
Every module under `bro/broker/` imports only the standard library, `bro.base`, and `bro.broker`;
a test module may also import `pytest`.
`local/bro/local/import_policy_test.py`, rostered in `run_tests.py`, walks the package's imports with the per-file reader `bro.dev.affected_tests` already has (made public for it) and fails on any other name.
Today's one violation is `client.py`'s `bro.launch` import, which the environment move removes.

**Journal (`journal.py`).**
`Record.quest_id` becomes `mission_id`, `Record.requester` becomes `owner`, `Event.quest` becomes `mission`, and `Journal.open(mission_id, kind, parent, owner, args, *, type, talk)`.
A record and its events carry the worker `type` as a field of their own beside `kind`, never read back out of the bounded args, whose trimming may drop any scalar.
`open` requires it, while `deny` takes it optionally:
a denial of a request that named no usable type journals with `type` unset, omitted from its views, which no type-filtered consumer matches.
Views follow:
`query {}` answers `{missions: […], cursor?}`, `query {id}` answers `{mission: view}`, and a record or event view carries `mission` and `type`.
Lineage and eviction are unchanged, so an evicted view carries kind and parent alone;
a consumer that filters by type checks `state == 'evicted'` first.

**Supervisors (`supervisor.py`, was `worker.py`).**
`Worker` and its shapes become `Supervisor`, `SpawnedSupervisor`, `JobSupervisor`, `ExpectedSupervisor`.
`WorkerListener` becomes `SupervisorListener` with `on_bound(supervisor, worker)`, `on_ready`, `on_message`, and `on_death`, and a supervisor holds its `mission`.
"Worker" then names only the peer undertaking a mission, and the dispatcher's `workers` table keeps mapping a worker peer to its mission.

**Dispatcher (`dispatcher.py`).**
The primitives stay `reply`, `deny`, `spawn`, `job`, `expect`, with three changes.
The spawner rides with the launch:
`spawn(launch, spawner, owner, *, type, talk, timeout)` and `run(root, spawner, *, type, end_on_sigterm)` take the `Spawner` that lowers that launch, `Runtime` holds the transport alone, and ride's `CompositeSpawner` goes.
The worker type is the caller's:
`spawn`, `job`, and `expect` take it as a required keyword for the record they open, `run` takes the root's, and `deny` takes the requested one when the request named it.
The deadline is the caller's:
`timeout` is a required keyword of `spawn` and `job`, `None` runs the worker unbounded until cancel, the owner's death, or teardown, and `DEFAULT_TIMEOUT` goes;
`expect` keeps no deadline.
`cancel {id}` ends a live mission for the peer that owns it.
The three routing rules, the orphan cascade, and the read kinds are otherwise unchanged.

**Broxy (`broxy.py`).**
Routes are keyed by request id as today.
One new verb, `broxy run [--log-file PATH] -- <command…>`:
it launches the proxy against `BROKER_UPSTREAM`, runs the command with `BROKER_CHANNEL` swapped in, forwards SIGTERM, exits with the command's status, and logs to stderr unless a file is named.
A worker container's command runs under it, so several channel clients inside one container (a driver and an `artifact mint`) share one attach instead of superseding each other.
`bro/launch/broxy.py` stays the in-process wrapper a session runs under (`session_broxy`), shelling out to `broxy launch`, since its log path reads `RIDE_SESSION_DIR`.

**Client and CLI (`client.py`, `cli.py`).**
The client reads `BROKER_MISSION` and `BROKER_TALK` for its own mission's moves and refuses a missing `worker` right before sending;
`broker message` and `broker listen` take a request id.
`RunLifecycle.from_env` reads `BROKER_MISSION`.

### Worker types: `bro/worker_types.py`

The contract a registered type is built against, in core so a third-party type imports no ride.
It replaces `bro/kinds.py` and `ride/ride/kinds.py` and keeps `tree_path`, `ArtifactDenied`, and `ArtifactResolver`.

**`WorkerType`.**
One abstract base every type implements.
Its entry point targets the class, so its declarations are readable without a host.
A type module keeps its heavy imports (broker machinery, lowerings) off module level the way `ride/ride/summon_control.py` does today, since the permit check on the launch path imports the module.
Its declarations:
`name`;
`permits`, the unqualified leaves it interprets (`party.start.boxed`), which every surface spells `:<name>.<leaf>`;
`default_timeout` (`None` for an unbounded worker);
`widens_talk`, whether a launch may name `talk`;
and `manual`, whether a launch may name `manual` and so expect a worker the user launches against a token.
An instance is `cls(host)`.
Its methods:
`talk(request) -> Talk`, the mission's effective rights:
the type's own default widened by what the request asked, or `LaunchDenied` when the widening is not one it grants;
`launch(request) -> Run`, validating the type's own arguments and authorizing against the owner's description, raising `LaunchDenied(reason)` to refuse;
`audit_fields(extension) -> Mapping[str, JSON]`, what the audit row adds for a worker of this type, empty by default, rendered once when the launch is accepted so a value that does not serialize fails the launch rather than the audit;
and `subscribers()`, optional journal projections.
A `LaunchRequest` carries `id`, `type`, `args` (the type's own, the common ones removed), `owner: PeerDescription`, `requested_talk` (the rights the launch named, empty when it named none), `timeout`, `share`, and `manual`.
A `PeerDescription` is what the host knows about a peer:
`mission`, `workspace`, `tree`, `type`, `bro` (a session's, else `None`), `permits`, `member`, `expected`, `artifact_view`, `published_ports`, `depth`, and `extension`, the type-owned row of the peer's own mission.
A `Run` is the one host primitive the worker runs through:
`Spawn(launch: LaunchSpec, spawner: Spawner, extension)`, `Job(command: CommandJob, extension)`, `Container(spec: WorkerContainer, extension)`, or `Expect(pending, extension)`.
`extension` is the row the host records for the new worker, an in-memory value the directory holds for the ride's life, `None` by default for a type that keeps none;
`pending` is the type's part of the token record, a JSON object written verbatim and decoded by the type's own launch command.
The accepted `share` list reaches the lowering, where the worker's workspace first exists and the refs are linked into its view:
a `Spawn` carries it inside the type's own launch, which the type fills from the request, and a `Container` gets it from the control, which hands the refs to the host's container lowering beside the spec.
There is no placement in the contract.

**`Host`.**
The ports a type receives:
`peers` (the peer directory, `describe(mission_id) -> PeerDescription`, raising `UnattributablePeer` for a mission with no complete row, such as an expected worker whose launch has not claimed its token yet);
`artifacts` (`resolve(ref, peer: PeerDescription) -> Path`, today's resolver, checked against the peer's reach);
and `credential_kinds`, the root launch's.
A type sees no dispatcher and no channel:
the control resolves the requesting channel to its mission and describes the owner before delegating, and a subscriber describes a mission by the id its event carries.
Ride's implementation carries its own machinery besides, and ride's own types downcast to it;
a third-party type sees the protocol.

**`WorkerContainer`.**
A core-typed container the host lowers, validated when it is built so a malformed declaration fails before Docker:
`files: Mapping[str, bytes]`, the build context the type ships, keyed by normalized relative POSIX paths (no `..`, no leading `/`, no empty segment) and holding a `Dockerfile` that opens `FROM ${RUNTIME_IMAGE}` (an `ARG`, like the project image);
`command: tuple[str, ...]`, non-empty, a session command from the runtime volume and its arguments;
`env: Mapping[str, str]`, refused when it names a variable the host owns (`BROKER_*`, `RIDE_*`, `BRO_*`, `HOME`, `PATH`);
and `published_ports: tuple[int, ...]`, distinct container ports in `1..65535` to publish on the launcher's loopback.
The image hash is over the runtime image tag and the files as sorted (path, bytes) pairs, so an independently packaged type and the host compute the same tag;
a type reads its packaged files itself (`importlib.resources`) and the host streams them to `docker build` as a tar, the way it assembles the runtime image.
The image extends the runtime image because the runtime volume's interpreter and session commands are materialized for that image, so a type that wants another base installs it on top.

**Registry.**
Types register through the `bro.worker_types` entry-point group, replacing `bro.broker_kinds`;
each entry names the type and targets its `WorkerType` subclass.
The entry-point name is the type's name, and the class's `name` must equal it, checked when the entry is loaded, so one type is loadable by name without importing the others.
A type name is one segment, `[a-z][a-z0-9-]*`:
it is the first segment of every permit it interprets, a component of its workers' workspace names, and the repository of its image, so the grammar is what all three accept.
`installed_types()` loads every entry's class, sorted, and a duplicate or mismatched name raises;
`installed_type(name)` loads one.
Ride registers `bro`, `bench` registers `benchmark`, and #638 adds `webview`;
ride's root composition names none of them.

### The `launch` kind and the host: ride

**`ride/ride/launch_control.py`.**
`LaunchControl` serves `launch {type, timeout?, share?, talk?, manual?, …}`:
it resolves the requesting channel to its mission and describes the owner (unattributable is denied), looks the type up (unknown is denied), and validates the common arguments.
Then it asks the type for the mission's talk (`talk(request)`), then for its run (`launch(request)`), records the facts row, and calls the dispatcher primitive the run names with that talk;
the same talk goes into the token record of a manual launch.
`timeout` is a positive number or the type's default.
`share` is a list of refs each reachable by the owner, honored by a `Spawn` or a `Container` run and denied for a `Job` or an `Expect`.
`talk` is a list of distinct rights, accepted only where the type widens.
`manual` needs a type that declares it and refuses `timeout` and `share` (no host-killable worker, no host-built workspace).
A manual launch requires an `Expect` run and every other launch refuses one, so a type cannot open an expected channel and a token nobody asked for;
the control's `ready` then materializes the host runtime and writes the token record, and the mission's end discards it.
Every denial reads `launch denied: <reason>`.
The control owns the audit:
one JSONL row per journal event in `launch_dir()/<ride>.jsonl` (`launch_dir()` replacing `summon_dir()` under the runtime root),
with the event view, the owner's attribution as `owner`, the `type`, the worker's `published_ports`, and the type's `audit_fields`.

**Peer directory (`ride/ride/peer_facts.py`).**
`PeerFacts` implements the port.
A row is generic, `WorkerFacts(type, workspace, tree, member, permits, expected, artifact_view, published_ports, extension)`,
written by the control when a launch is accepted and completed by the lowering with the workspace or member name and the ports it bound;
the root row is seeded from the root record as today, and an expected worker's workspace is read from the claimed token record, so nothing bro-specific remains in the directory.
The artifact store reads the row:
a peer with a view gets the container path, a host-launched peer without one gets a private copy under its workspace, an expected peer is denied.
The bro's extension is `BroFacts(bro, allow_list, grant, revoke, llm, harness, credential_scope, placement)`, what summon authorization reads off an owner's description;
`placement` is the resolved party and isolation (a manual start's isolation unresolved), which the type's `audit_fields` renders from the extension alone.

**Pending launches (`ride/ride/pending_launch.py`, from `pending_summon.py`).**
`PendingLaunch(token, runtime, port, channel_token, type, talk, owner_tree, env, extension)` under `launch_dir()/pending/`, with `write`, `peek`, `claim(token, workspace=…)`, `claimed_workspace`, and `discard` as today.
The bro's extension carries `target`, `prompt`, `may_summon`, `permits`, `grant`, `revoke`, `summoner`, `repo`, and `into`, read by `ride along --summoned`, which stays the bro's launch command.
The broker never converts an expected worker into anything:
the host holds its `ExpectedSupervisor` on the provisioned channel for the mission's life, treats the attach as the start and EOF as the death, and a cancel detaches rather than kills.
What a manual launch reuses is the type's lowering:
a type that declares `manual` owns a user-side launch command, and that command rebuilds the launch through the same lowering its spawner uses,
so a hand-launched worker differs from a spawned one only in who supervises it and where its channel came from.
The bro's does exactly that today:
`ride along --summoned` claims the record, builds the session spec from its extension plus the user's own flags (hold, llm, harness, isolation, the fields a manual request may not fix),
and goes through `started_party_launch` like `_lower_summon` before `run_manual_started_party` runs it attached to the summoner's channel.
The record's extension is defined as what that lowering needs beyond what the user's command supplies.
No generic user-side claim command is added:
only the bro has a launch command today, and it legitimately takes flags of its own;
a container type wanting a manual variant reuses the worker-container lowering the same way, and that is the moment to generalize the entry point.

**Root composition (`ride/ride/broker_root.py`, from `spawn.py`).**
`run_root_via_broker` builds the ride host:
the directory, the artifact control, the root's credential kinds, and ride's machinery
— workspace, runtime bundle, container runtime, the docker, process, and exec spawners, party members, the artifact store, summon depth and harness, session env.
It instantiates the installed types, registers `ping`, `launch`, `artifact.mint`, and `artifact.get`, subscribes the directory, the control's audit and token cleanup, and every type's subscribers,
and runs the root with the docker or process spawner.
The types mapping is a parameter defaulting to `installed_types`, so a test composes a root with a type of its own.

**The bro type (`ride/ride/bro_worker.py`).**
`BroType.launch` validates the bro's arguments (`target`, `prompt`, `into`, `hold`, `step_id`, `index`, `grant`, `revoke`, `llm`, `harness`, `party`, `isolation`).
It resolves the placement against the owner's `:bro.party.*` permits
— its own `Placement`: a boxed start, an unboxed start, or a join in the owner's isolation, the boxed start the default, a manual launch a start —
checks the depth cap and the target against the owner's allow-list, and computes the child's allow-list, permits, and credential widening.
It returns `Spawn(SummonLaunchSpec, summon spawner, BroFacts)` or, for `manual`, `Expect(pending extension, BroFacts)`.
An owner of another type has no allow-list and is denied.
`SummonLaunchSpec`, `SummonSpawner`, and the two lowerings move here from `ride/ride/spawn.py`, the accepted `share` riding on the spec as today.
Its talk is `{worker.say}` widened by the request, its default deadline `bro.summon.DEFAULT_TIMEOUT`, its `audit_fields` the placement read off `BroFacts`, its subscriber the lifecycle log lines.

**The benchmark type (`bench/bro/bench/job.py`).**
`BenchmarkType`:
no permits, a twelve-hour default deadline, no talk, no manual launch.
`launch` requires an owner at depth 0, a config file under the owner's tree, and a benchmark project in it, and returns `Job(CommandJob(…))` as today.
`benchmark-job start` sends `launch {type: benchmark, config, timeout?}`;
`check` reads a record as a benchmark job when it is not evicted, its kind is `launch`, and its `type` is `benchmark`.

**Worker containers (`ride/ride/worker_container.py`).**
The lowering of a `Container` run, off-loop like a summon start.
The image is `bro/<type>:<hash>`, the hash over the runtime image tag and the type's files, built from those files with `RUNTIME_IMAGE` passed in when the tag is missing, with the repository's superseded tags pruned after a build.
The workspace is a throwaway `<type>-<channel>`, detached, mounted at `/workspace`.
The container gets the runtime volume read-only, an artifact view with the launch's `share` linked in, an empty credential store, no session, party, or trails mount, the type's env, and the broker env the docker spawner adds.
`Launch` (`ride/ride/workspace/docker.py`) gains `published_ports`, host and container port pairs rendered `-p 127.0.0.1:<host>:<container>`;
a session launch has none.
The lowering binds each container port the type names to a free loopback port it picks before the container is created
— a port taken in between fails the `docker start`, and the mission with it, rather than silently moving —
records the pairs on the facts row, and passes them into the container as `RIDE_PUBLISHED_PORTS=<container>=<host>,…`, so the worker can report its address to its owner over the channel, the one path from a worker to its owner.
The command runs under `broxy run`.
Supervision is the docker spawner's:
the workspace is removed on a clean exit and kept, with the log tail in the death report, otherwise.
`WorkerContainerSpawner` is the `Spawner` the control passes to `spawn` for a container run.

**Permits.**
`bro/base/scope.py` accepts `:<type>.<leaf>`, the leaf one or more dot-separated segments, every segment `[a-z][a-z0-9-]*`, nothing reserved.
The bro's leaves are `party.start.boxed`, `party.start.unboxed`, and `party.join`, so its permits are `:bro.party.start.boxed`, `:bro.party.start.unboxed`, and `:bro.party.join`, replacing today's `:party.*`;
their constants live in `bro/summon.py`, the framework seed (`:bro.party.start.boxed`) in `ride/ride/scope.py`, and `bro.base.scope` keeps the grammar and `permit_choices`, which describes the shape.
`RIDE_PERMITS`, `encode_permits`, and `permits()` follow the grammar.
Ride's launch preflight and the bro type's child-permit computation check every permit a configuration layer or the request names against its type's class,
loading only the types named, so a launch naming none loads nothing and the framework seed is never checked;
a permit naming an uninstalled type or an undeclared leaf fails the launch or the summon, listing the installed types.
Config validation stays grammar-only, so a host config still saying `:party.join` fails at launch as an unknown worker type `party`, with `bro` among the types listed.
The respelling reaches `summon`'s help, the summon tool description, `environment.md`, `ask.md`, `bro/setup/AGENTS.md` (its host-config examples and the leaves sentence),
`bro/reference/ride.md`, and the benchmark project's Harbor agent launch (`benchmark/bro/benchmark/harbor_agent.py`).

### Surfaces

- `bro/quest.py`:
  `SUMMON` becomes `LAUNCH = 'launch'` and `BRO = 'bro'`.
  A quest is a record whose kind is `launch` and whose `type` is `bro`, checked after eviction, in `answer_of`, the listing filter, and the chat lines.
  `live_children` becomes `live_missions`:
  every live mission the session owns, each with its id, type, and label
  — the target for a bro, the type name otherwise.
  The watch prints lifecycle lines for every mission the session owns, a bro's worded as today and another type's as `launch <type> <transition> (mission <id>)`;
  its chat lines stay per mission as today.
  Cancellation is the broker's general `cancel {id}` for any mission the session owns:
  `quest cancel` and `bro::quest_cancel` accept every owned mission, bro or not, the one quest surface that does not filter by type, and the turn-end notices name that route.
  `own_quest` reads `BROKER_MISSION`, and `caller_end` answers `worker` on the own quest and `owner` on a child's.
- `bro/summon.py`:
  `summon` sends `launch {type: bro, …}`;
  `DEFAULT_SUMMON_TALK` is `{worker.say}`;
  `--talk` and the tool field take the new right names;
  the placement flags name `:bro.party.*`.
- `bro/bro.py`, `bro/mcp.py`, `bro/workspace/banner.py`:
  the tool descriptions, the `#talk` universe, and the banner's `talk:` and `permits:` lines use the new names;
  the watch admission is unchanged.
- `ride/ride/claude/statusline.py`:
  bro launches render as today;
  any other live launch renders as its type name, its end as `✓|✗ <type>: <outcome>`.
  `ride/ride/claude/stop_guard.py` and the native runner's one-shot end rule (`native/bro/native/runner.py`) hold the turn end for every live mission the session owns,
  naming each by type and label and pointing at `quest cancel` (`bro::quest_cancel`) for one the agent no longer wants, since every one of them dies with the session the same way.
- Prompts and spells (`summoned.md`, `summoner.md`, `environment.md`, `holds/unattended.md`, `ask.md`, `orchestrate.md`):
  the four right names and the permit spelling change;
  the prose keeps the summoner and the child.
- Docs:
  `bro/broker/AGENTS.md` (vocabulary, boundary, `broxy run`),
  root `AGENTS.md` (the entry-point group, `worker_types.py`, `run_lifecycle.py`, the `live_children` reference it gets wrong today),
  `ride/AGENTS.md`, `ride/ride/workspace/AGENTS.md` (the lazy-import invariant, the spawner map), `bench/AGENTS.md`, `bro/launch/AGENTS.md`, `bro/prompts/AGENTS.md`, `bro/setup/AGENTS.md`,
  and `bro/reference/ride.md` (the broker channel, summoning, manual summon, and forwarded env vars, where `BROKER_MISSION` gets a bullet of its own and `RIDE_PUBLISHED_PORTS` joins the worker-container ones).

### Rollout

As #637 settled:
every session process runs from the launcher's runtime bundle, so host and peers are one release, and the revision bump refuses a project venv's older `bro` at attach.
Persisted shapes:
the pending token record moves under `launch_dir()` and is read only by the minting runtime;
the audit JSONL gains `type`, `placement`, `owner`, and `published_ports` and has no in-repo reader;
`RIDE_PERMITS` and every configured grant respell `:party.*` as `:bro.party.*`, a stale host config failing loudly at launch;
`RIDE_PUBLISHED_PORTS` is read by the worker inside a container launched from the same runtime.
Configured permits are the one contract a running root reads after its launch:
`SummonControl.handle` reads the host and project scope layers per request,
so a root on the old bundle fails its next summon once `~/.bro.json` or a project `[tool.bro]` says `:bro.party.*`, and a new root fails at launch while they still say `:party.*`.
The order is to end the running rides, upgrade the launcher, respell the configuration, and launch;
either stale pairing fails loudly rather than silently widening or narrowing a permit.

### Rejected

- Keeping `bro.broker_kinds` beside `launch`:
  two ways to add work, one with ad hoc authorization and no permits or facts.
- The type in the kind name (`launch.bro`):
  one handler per kind would need prefix routing and every filter would parse names;
  one handler and one journal shape with `type` in the args is simpler.
- A spawner registry (`CompositeSpawner`) keyed by launch type:
  the type already knows its spawner, and carrying it with the launch removes a registry and its "no spawner registered" failure.
- Retaining `type` in lineage so evicted views keep it:
  an evicted record is reported as not retained before anything reads its type, which is what every consumer wants anyway.
- A direct channel attach for worker containers:
  a second client in the container would supersede the first;
  `broxy run` keeps the session's multiplexing shape.
- `requester`/`worker`, `parent`/`child`, `client`/`worker` for the ends:
  a client is the channel handle both ends use, and parent and child name the tree rather than the roles.
- A general placement concept:
  only the bro chooses where its worker runs;
  a benchmark is a host job and a webview a container, so placement is the bro's argument set and permit leaves.
- `manual` as a bro-only variant:
  the expected-worker mechanism, the token record, its claim, and the workspace resolution are the same for every type;
  only the record's extension and the user's launch command are the type's.
- A generic depth cap in the control:
  the cap guards summon fan-out, and a webview under a bro is a leaf.
- The worker-type contract inside `bro.broker`:
  the substrate stays messaging and supervision;
  types, permits, and containers are the launch layer above it.
- Keeping `broker_enabled`'s `ImportError` degrade, or moving the env names to `bro.base` to preserve it:
  the degrade guards a state the runtime bundle rules out, and `BROKER_DISABLED` is the escape valve that stays.
- Host ports named by the type:
  two workers of one type collide, and the owner learns nothing.
- Counting only bros toward a one-shot session's turn end:
  every mission the session owns dies with the session the same way.

### Verification the stages owe

- `brotocol_test.py`:
  the `request` field, revision 5, the owner/worker talk;
  `transports/tcp_test.py`:
  the mismatched-revision refusal.
- `journal_test.py`, `dispatcher_test.py`:
  the renamed records and views, the first-class `type` on records, events, and denials, `spawn` with its spawner, the required `timeout` and the unbounded `None`, the owner-scoped `cancel`.
- `broxy_test.py`:
  `broxy run` swaps the channel in, forwards SIGTERM, relays the exit status.
- `local/bro/local/import_policy_test.py`:
  the boundary, with a deliberate violation caught in the test's own fixture.
- `bro/worker_types_test.py`:
  the registry, duplicate and mismatched names, the type-name grammar, the permit grammar with multi-segment leaves, `WorkerContainer` validation and its stable hash.
  `ride/ride/launch_control_test.py`:
  attribution, common-argument denials, the talk resolution order, `share` on each run kind, `manual` iff `Expect`, a manual launch of a test type claimed and answered,
  the facts row, the audit row rendered at acceptance, the token cleanup, an unknown type's permit listing the installed types.
- `ride/ride/bro_worker_test.py` (from `summon_control_test.py`):
  every summon denial and acceptance through `launch`, placement resolution against `:bro.party.*`, the manual expectation, the projections.
  `ride/ride/pending_launch_test.py` from the pending-summon tests.
- `ride/ride/scope_test.py`:
  the permit existence check loading only the named types, an uninstalled type or undeclared leaf failing the preflight, the seed unchecked.
- `bench/bro/bench/job_test.py`:
  the type's authorization over the description and tree, `benchmark-job` on the launch kind.
- `ride/ride/worker_container_test.py`, `workspace/docker_test.py`:
  the image tag and build, pruning, the mounts and env, the port binding and `RIDE_PUBLISHED_PORTS`, `-p` rendering, workspace settlement.
- `ride/ride/workspace/containers_test.py`:
  the gate on `BROKER_DISABLED` alone.
- `quest_test.py`, `summon_test.py`, `bro_test.py`, `statusline_test.py`, `stop_guard_test.py`, `native/bro/native/runner_test.py`, `banner_test.py`, `host_config_test.py`:
  the launch-plus-type filters, live missions of every type at the turn end and on the watch, the right names, the permit spelling, the type parts.
- `ride/ride/e2e_test.py`:
  a summon through `launch`, a benchmark launch, a test type's worker container that pings the host through `broxy run`, mints an artifact, reports the port it was handed, and exits with its workspace removed, and the type-permit preflight;
  scenario D is removed.

## Design changelog

Review-and-plan, 2026-09-20 (trail `01m2zfsq0y-sm52tfdy-yjt1jjxd`), against the code the design names:

- **The unimportable-broker degrade goes, and `bro/broker/environment.py` stays where the design put it.**
  The pre-gate launch path (`ride/ride/workspace/containers.py`, `session.py`, `bro/launch/broxy.py`) read the env names, and `broker_enabled` had a second job beside `BROKER_DISABLED`:
  degrading to no channel when `bro.broker` could not be imported at all, with e2e scenario D shadowing the whole package.
  Under the runtime bundle ride and bro are one installation, so that state cannot exist;
  the `ImportError` branch and scenario D are removed, `BROKER_DISABLED` stays the escape valve, and the lazy-import invariant becomes one of cost.
  Settled with the user over moving the names to `bro.base` or reading them function-locally.
- **Worker-container ports are chosen by the host and reported by the worker.**
  The design let a type name host ports, which collide between two workers of one type, and nothing carried the bound port to the owner.
  A type names container ports only;
  the lowering binds each to a free loopback port, records the pairs on the facts row and in the audit, and passes them into the container as `RIDE_PUBLISHED_PORTS`, so the worker reports its address to its owner over the channel.
  Settled with the user.
- **Every live mission the session owns holds a one-shot session's turn end.**
  `live_children`, the Claude stop guard, the native runner's end rule, and the watch's lifecycle lines counted bros only, while a detached benchmark job or an open webview dies with the session the same way.
  `live_missions` replaces `live_children`, and every surface names each mission by type and label.
  Settled with the user over keeping bros only.
- **The `Host` ports are keyed by mission and description, never by dispatcher and channel.**
  `peers.describe(dispatcher, peer)` and `artifacts.resolve(ref, dispatcher, peer)` could not be called by a type, whose `launch(request)` holds neither;
  they became `describe(mission_id)` and `resolve(ref, peer: PeerDescription)`, with the control resolving the requesting channel to its mission before delegating.
- **Smaller settlements:**
  the permit existence check imports a type's module, so a type module keeps its broker and lowering imports off module level and the framework seed is never checked;
  the boundary rule exempts `pytest` for test modules;
  `bro/kinds.py`'s `ArtifactDenied` and `ArtifactResolver` move with `tree_path`;
  the accepted `share` list reaches the lowering, since the worker's workspace exists only there;
  a worker image extends the runtime image because the runtime volume is materialized for it;
  a permit naming an unknown type lists the installed types;
  `bro/workers.py` is `bro/worker_types.py`, matching the entry-point group and the class.
- **Verified rather than changed:**
  every session process runs from the launcher's runtime bundle and a manual launch re-executes from the token's runtime (`ride/ride/cli.py`), so host and peers are one release and the rollout section stands;
  the audit JSONL has no in-repo reader;
  `client.py` is the boundary's only violation;
  every `spawn` and `job` caller already passes a timeout and `expect` already refuses one;
  the container entrypoint runs a non-session command without an attached repository.

Design review round 1 (bro-eyebro on PR #692, 2026-09-20):

- **The worker `type` is a field of the journal record and its events, never read back out of the bounded args.**
  `bounded_args` may drop any top-level scalar over its budget, so a classifier reading `args['type']` could find a retained record unclassifiable;
  the dispatcher primitives take the type from the launch control, and `deny` takes the requested one.
- **The entry-point name is the type's name, checked against the class at load, and a type name is one lowercase segment.**
  Two spellings could drift, and dots or uppercase would break the permit split, the workspace name, and the image repository.
- **`requested_talk` on the request, the effective talk from `talk(request)`, and the control's order fixed:**
  talk, then run, then the facts row, then the primitive.
  The request carried a `talk` the type was also asked to compute.
- **Every `Run` variant carries `extension`, `pending` is a JSON object, and `audit_fields` are JSON values rendered at acceptance.**
  A job-backed type had no facts seam, and a type-owned object could neither be persisted nor audited generically.
- **`share` reaches a `Container` through the control, beside the spec.**
  Only a `Spawn` could carry it, inside the type's own launch.
- **`WorkerContainer` states its ABI:**
  file paths and bytes with a stable hash, a non-empty command, env names the host reserves, distinct valid ports.
- **`manual` iff `Expect`, checked by the control in both directions.**
- **`bro/bro.py` and `ride/ride/claude/statusline.py` join the env-name importers**, having spelled the strings.
- **Configured permits get a rollout order:**
  end the running rides, upgrade, respell the configuration, launch, since a running root reads the configuration per request.

Design review round 3 (bro-eyebro on PR #692, 2026-09-20):

- **A denial of a request that named no usable type journals with `type` unset**, omitted from its views;
  `open` requires the type and `deny` takes it optionally, so common validation has a representable denial.
- **`BroFacts` carries the resolved `Placement`**, so the bro's `audit_fields` renders it from the extension alone and the control keeps no placement side table.
- **`extension` defaults to `None` on every `Run` variant**, so `Job(CommandJob(…))` stands as written.
- **Cancellation is general:**
  `quest cancel` and `bro::quest_cancel` accept every mission the session owns, the one quest surface not filtered by type, and the turn-end notices name it.
