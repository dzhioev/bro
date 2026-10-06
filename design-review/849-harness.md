## Design

Settled with the user on 2026-10-05 (trail `01m45h783d-2jkg13yd-07dpdhqf`).
Reviewed against the code at `f7f1ba8b`, once #846 had landed, with the user on 2026-10-06 (trail `01m47h1ktn-8w232c6j-6ygzcm8d`).

### Probe: `asyncRewake` on the pinned Claude Code (2.1.280)

Run live from the design session in throwaway stream-json and TUI sessions, each with a `Stop` hook that waits for a trigger file:

- **Idle wake.**
  A `Stop` hook with `asyncRewake: true` that exits 2 starts a turn in a stream-json session idle between turns, and in an idle TUI.
  It backgrounds only in a TUI or in a print session with streaming input, which covers both `ride along` and `ride solo`.
- **The message.**
  The exit queues a task notification that the model reads as `<task-notification><summary>S</summary></task-notification>`, followed by a `<system-reminder>` holding `P B`.
  `B` is the hook's stderr, or its stdout when stderr is empty, delivered whole:
  57 KB arrived intact.
  `S` defaults to `Stop hook feedback` and `P` to `Stop hook blocking error from command "Stop":`, which the model read as a failing hook.
  `rewakeSummary` and `rewakeMessage`, marked internal in the settings schema, replace them from `--settings`.
  `UserPromptSubmit` fires for the notification, with its text as the prompt.
- **The bound.**
  An async hook's default timeout is 10 minutes, and `timeout` takes seconds with no maximum;
  an `asyncRewake` hook keeps its timer armed, and expiry SIGTERMs it (exit 143) and wakes nothing, leaving the session with no waiter.
- **Several waiters.**
  A wake from elsewhere (a user message, a task notification, a blocking `Stop` hook) leaves the earlier waiter running, and every turn end starts another;
  the waiters pile up and race for the same input.
- **Mid-turn.**
  A waiter that exits 2 while a turn runs is delivered inside that turn, after its next tool result, rather than as a turn of its own.
- **`stop_hook_active`** is true on the turn a rewake starts as well as on the continuation after a block, so it no longer marks a guard's second stop.
- **Invisible to task tracking.**
  An async hook appears neither in the `Stop` input's `background_tasks` nor in the `background_tasks_changed` stream event.
- **Exit.**
  Claude spawns hooks in their own process group.
  At the end of a print session it waits up to 30 s for a pending async hook, then SIGTERMs it;
  a TUI `/exit` SIGTERMs it at once.
- **Errored turns**, read off the binary in the review rather than probed:
  a turn that ends in an API error, a prompt-too-long, or a malformed tool call that failed again on retry fires `StopFailure` and returns before any `Stop` hook runs.
  `asyncRewake` is a field of every command hook's settings;
  that it backgrounds on `StopFailure` too is for the `llm` stage to confirm.

### The harness interface

One object per harness implements the whole harness, and the `bro.harnesses` entry-point group registers it as `<name> = "<module>:<object>"`.
General code reaches a harness through that object, passed in by the engine that drives the session or loaded by name through the registry, and compares no harness names.

- **Core.**
  `bro/harness/__init__.py` holds `Harness`, the base class of the framework half, and the registry:
  the installed names, read from entry-point metadata without importing anything, and the harness a name loads.
  The package already holds `claude.py`, the Claude tool names personas import until the persona slice moves them, so `__init__.py` stays cheap to import.
  Loading refuses an object that is not a `Harness` instance or whose `name` differs from its entry, as `bro.worker_types` refuses a class that is not a `WorkerType` or names another type;
  an uninstalled name raises `ValueError` listing the installed ones.
- **Ride.**
  `ride.harness.SessionHarness` is today's `Harness` protocol under a new name:
  scope recipe, auth preflight, LLM resolution, the session reads and run, and the boxed and unboxed extras.
  It gains `prepare_session`, which takes over `do_ride._prepare_claude_state`, and `check_runtime`, which proves the harness's runtime starts where the installation runs (for Claude, the pinned binary) and which `ride check-harness <name>` exposes.
  Ride resolves a harness through the core registry and refuses one that does not implement it.
- **Claude.**
  `ride/ride/claude/harness.py:CLAUDE`, registered as `claude` by `bro-ride`.
- **Bro.**
  `native/bro/native/harness.py:BRO`, registered as `bro` by `bro-native`, which takes over `ride/ride/bro.py` and gains a dependency on `bro-ride`, as `bro-browser` has.
  Ride loads `bro-native` only through the group, never by import.
- **Names.**
  Every list of harness names reads the registry:
  `bro.mcp.Harness`, `ride.harness.HARNESS_NAMES` and `get_harness`'s if-chain, ride's `--harness` choices, `bro_worker`'s summon check, and `bro.summon`'s help.
  An installation may carry one harness without the other
  — `bro-ride` without `bro-native` is a Claude-only runtime (`native/README.md`), and the benchmark launcher's interpreter cannot hold `bro-native`'s openai major —
  so a name is checked against the installed ones where it is used, not where it is configured.
  `[tool.bro] harness` and `summon-harness` (`bro/workspace/project.py`) and the defaults are read as well-formed names, and a launch or summon that selects an uninstalled harness fails naming the distribution to install.
  The defaults, `claude` for a launch and `bro` for a summon, are configuration values kept together in `bro.base.configs`.
  The Terminal-Bench adapter in `benchmark/` names no harness:
  the relocatable bundle's manifest records the harness names its installation registers, under a new manifest format, and the launcher checks a trial's harness against them rather than against its own interpreter;
  `install()`'s Claude-only binary probe, keyed on `bro.harness.claude.HARNESS`, becomes the selected harness's own check, `ride check-harness <name>` run from the bundle in the task image.
- **The `#harness` fact** compares against any well-formed harness name rather than a closed domain, since a text may name a harness the installation does not carry;
  an `iff` chain still raises when no branch matches the running harness.
- **No guessing.**
  A workspace with no resume spec records no harness, so `ride list` shows no subject for it rather than reading it as a Claude workspace;
  neither does it for a workspace whose recorded harness this installation does not carry.
- **Injection.**
  The engine passes its harness object wherever code passes `harness='bro'` or `harness='claude'` today:
  `BaseBro.assemble`, the composed prompt and its session fragments, spell bodies, and the cast tool.

The framework half grows one slice at a time:

| Member | Replaces | Slice |
|---|---|---|
| `name` | the `Literal` and every list of names | #850 |
| `end_session`, `can_end_session` | `_claude_answer` and `_claude_raise`, the `RIDE_RUNNER_PID` check | service tools |
| `own_tools` | the job tools, `skill`, and their share of the `#tools` universe | service tools |
| `serve` | the Claude branches of `_fold_tool_layers`, the `bro`-harness special case in `_components_for` and `assemble` | tool fold, persona declarations |
| `facts` | the `#harness` forks left in prompts | prompts |

The watch port is not a member:
it is two protocols the harness's session runtime implements, below.

### Watching (#850)

#### General code

- **One store.**
  `bro.watches` keeps every watch's lines under the session state dir for both harnesses, read by one reader.
  `take()` reads, under an exclusive lock, the complete lines past each watch's offset, at most 100 lines and 30 KB (the `bro.base.text_window` bounds), and tags each with `[<command>]`.
  Watches take turns:
  a batch starts at the watch after the one the last batch cut, and takes each watch's lines in turn until a bound binds, so no watch's volume keeps another's lines out.
  A line wider than the byte bound goes out in bound-wide pieces, a batch each, the first naming the line's whole size, so an offset always advances, as `take_head` cuts a first line today.
  It ends a cut batch with a pending marker and commits the offsets past what it returns.
  A run with no session state dir, an in-process `bro run|chat` outside ride, keeps its watches in a temporary store its `Runner` owns.
- **Producers.**
  `watch-run` runs its command under `bro.job_supervisor`, the process-group leader behind `bro::job`, detached from whatever process started the watch.
  Stopping a watch ends its whole process group, as `bro::kill` ends a job's.
  A watch's owner is its session's:
  `do-ride` in a managed session, and the `Runner` in an in-process `bro run|chat`, whose lifetime can end while the process embedding it lives on.
  The owner stops its producers in its own teardown
  — `do-ride` when the session ends, the `Runner` in `__exit__` beside its job registry —
  and the supervisor also exits once a liveness handle the owner holds closes, so a killed owner leaves no producer behind;
  a job's supervisor does the same for the process that started the job.
- **The session watch.**
  `do-ride` arms `watch-run quest watch` before the harness session starts and stops it after, wherever today's admission rule admits it:
  a session that may summon, or a summoned one whose talk carries `owner.say`, `owner.question`, or `worker.question`.
  The rule moves out of `_fold_tool_layers` into the module that arms the watch.
  When the session ends, `do-ride` stops every producer still live in its store.
- **Watches the model starts.**
  `bro::watch(command)` keeps an admitted command running for the rest of the session through the store, and `bro::unwatch(command)` stops it.
  Both mount wherever the persona declares shell reach, on every harness, and admit commands against the persona's shell roster, as `job` does;
  on the bro harness they replace `job`'s `watch` mode.
  `bro::unwatch` refuses the session watch, which the runtime owns.
  A persona that declares no shell has no watches on either harness, although Claude still serves it an undeclared `Bash` until the persona slice.
- **One stream.**
  `bro.mission` owns the stream behind both `quest watch` and `mission watch`:
  arming at the journal head, replaying retained chat, re-arming across a gap, and following the ordered events.
  A re-arm after a gap replays only entries past the last one the stream yielded, where `bro/quest.py:_arm_replay` today repeats every retained owner entry.
  `quest watch` is its view of the session's own quest and the bro missions the session owns.
- **One end-of-turn rule.**
  `bro/turn_end.py` settles a one-shot turn end from the live work:
  the missions the session owns and which of them the live session watch covers;
  the model's live watches and the lines in the store not yet delivered;
  the harness's own background work;
  and whether a summoner may still speak or a question of the session's own awaits its reply.
  The session's own quest reaches it only through the session watch, so the last two count only while that watch's producer runs;
  with it down, they count as nothing to wait on, and the bro missions it covered as uncovered.
  The first verdict whose condition holds applies:

  1. **Wait** silently while a covered mission, a live watch of the model's, an undelivered line, or a question awaiting its reply exists:
     each of those wakes the session when it moves, and an end never strands lines a watch printed before it exited.
  2. **Notice, then wait** while harness background work runs or a summoner may speak:
     the notice names that work and any mission no watch covers, says that ending the turn again keeps waiting, and, in a summoned session, that a finished result goes through `bro::answer`.
     The browser in an owner-led `[[browse]]` thread waits here, its webview mission live with no watch on it.
  3. **Notice, then end** while only missions no watch covers remain, or the session watch is down:
     nothing would wake the session for them, so the notice names them, the dead watch's exit line, and the routes the session has (`bro::watch('mission watch')` where it is mounted, or cancelling them);
     ending the turn again ends the run and orphans them.
  4. **End** otherwise.

  A notice comes once per distinct live set, from one text template both harnesses share.
  An interactive session has no rule:
  it idles, wakes on lines, and takes a human's message at any time.

#### The port

Each harness implements four operations for its sessions, behind two protocols in core:

- `LineSink.deliver(batch)` puts lines into the model's context and wakes the session if it idles;
  the general pump hands it each batch `take()` returns, and takes the next only once the model has the last, so a burst reaches the model one bounded batch at a time.
- `TurnEnd.background_work()` reports the harness's own live background work, and `TurnEnd.notify(text)` and `TurnEnd.end()` carry out a notice and an end;
  the harness calls `turn_end.settle(port)` at each one-shot turn end.

#### Claude (`ride/ride/claude/`)

- **The waiter.**
  `watch_waiter.py` is every session's `Stop` hook and its `StopFailure` hook, with `asyncRewake`, `rewakeSummary: "watch lines"`, `rewakeMessage: "New lines from this session's watches:"`, and a `timeout` of 24 hours;
  `StopFailure` keeps a turn that ends in an API error, which runs no `Stop` hook, from leaving the session without a waiter.
  It registers as the session's current waiter, and a waiter it supersedes exits 0 at its next poll.
  It also exits 0 once the session ends, on the runner's stand-down mark or when `RIDE_RUNNER_PID` is gone.
  It polls the store every 0.5 s, and on a batch writes it to stderr and exits 2;
  the next waiter starts at the turn end that batch leads to, which is how a burst arrives one batch at a time.
  A minute before its bound it exits 2 with a line saying no watch line arrived for 24 hours and that ending the turn keeps waiting, so the bound shows rather than leaving the session without a waiter.
- **The one-shot runner.**
  `interrupt.run_streaming` settles each turn end at its `result` event.
  Background work is the task list of the last `background_tasks_changed` event, which Claude Code's stream-json schema declares, minus the entries flagged `ambient`:
  Claude's own housekeeping (`skip_transcript` tasks and ambient websocket monitors), which it tells hosts not to count as activity, and which today's runner counts.
  A notice goes in as a user message on stdin;
  an end stands the waiter down, then closes stdin.
  The runner also stands the waiter down before interrupting Claude, so a pending waiter adds no 30-second exit delay.
- **Removed.**
  `stop_guard.py` and `watch_delivery.py` go with their hooks, and `watch_commands.py` with them.

#### Bro (`native/`)

- A pump thread delivers each batch into the run's inbox, which already reaches the model after tool results and wakes an idle `bro chat`;
  it takes the next batch only once the inbox has drained the last.
- `Runner` settles each one-shot turn end:
  background work is the running jobs, a notice goes through the inbox, an end returns the reply, and a wait idles on the inbox and wakes on its news, as `bro chat` does.
- `bro::chill` goes, and with it the turn it held, which is what kept a human from typing in `bro chat`.

#### Tools and prompts in the same landing

- **The quest tools return at once on every harness:**
  `summon` on acceptance, `quest_ask` with the question id, `quest_check` and `quest_history` after one read, and `quest_cancel` once the host accepts.
  The Claude variants and their transport cautions go.
  With `quest_ask`'s `wait` goes the `counter_question_id` view #846 adds to it;
  the quest watch line already names both ids (`summon asks … (…, question <id>, to <id>)`).
  `quest_share`, which #846 also adds, already returns at once and stays as it is.
  The `summon` and `quest` CLIs keep their blocking forms for shells and humans.
- The fold stops admitting quest-watch commands, so a persona that declares no shell has none on either harness.
- `watch-next` goes;
  `watch-run` stays, as the producer.
- `summoner.md`, `summoned.md`, `ask.md`, the `watch` spell, `orchestrate.md`, `run-pr.md`, and #846's `browse.md` owner contract state one model with no `#harness` fork:
  the session watch is kept for the model, lines arrive as notifications, waiting is ending the turn, and quest moves go through the `bro::` tools.
  `run-pr.md` thereby also loses the branches that keep the `summon ended` route from the bro harness, where it is just as true.
  `fragments/watch.md` folds into the `watch` spell.
- `bro/reference/ride.md`, `bro/AGENTS.md`, `bro/prompts/AGENTS.md`, `ride/ride/claude/AGENTS.md`, `native/AGENTS.md`, and the root map follow.

#### Edge cases

- A burst larger than one batch ends with the pending marker, and the waiter started at the next turn end delivers the rest at once.
- A line that arrives during a turn is delivered within it by the waiter still waiting from the previous turn end;
  one that arrives after that waiter has exited is picked up at once by the next.
- Lines that arrive before a session's first turn end wait for it:
  a one-shot session starts with a turn, and an interactive one without a prompt has a human about to type.
- Whether `Stop` fires on a turn the human interrupts in the TUI is unprobed;
  if it does not, a waiter still waiting from an earlier turn end delivers, and otherwise lines wait for the next turn end, with the human present.
- A session watch whose producer died, because the broker refused it for instance, covers nothing:
  its exit line is delivered, and the rule counts its missions as uncovered and its own quest's traffic as unable to arrive.
- A resume starts a new root on a new quest, since `ride resume` launches no summoned child, so the session watch it re-arms replays nothing of the run before, and the store's committed offsets keep that run's delivered lines from returning.
- A joined member keeps its own store under its own session state dir.
- Lines whose offsets were committed are lost only if Claude dies before showing them, when the session is gone anyway.

### The slices after #850

1. **Service tools.**
   `answer` and `raise` end the session through `Harness.end_session`:
   the bro harness raises for its runner, and Claude emits the result and terminates the session.
   `answer` mounts where `Harness.can_end_session()` holds, and both descriptions state the end without a condition, since it holds on both harnesses.
   `Harness.own_tools` contributes the tools a harness serves itself:
   the bro harness's `job`, `poll`, `kill`, `jobs`, and `skill`, which move into `bro-native` with `bro/jobs.py` and `bro/inbox.py`.
   `bro/job_supervisor.py` stays in core, shared with `watch-run`.
   Core's `LiveRun` keeps only the trail id and the tool position, and the `#tools` universe is core's tools plus the harness's own.
2. **Tool fold, with #754.**
   The fold produces a harness-neutral reach, and `Harness.serve(reach)` maps it:
   Claude onto natives it blocks, narrows behind the command gate, or serves, and the bro harness onto its own tools, refusing native names.
   #754's shell syntax and its matcher are general code that both the Claude gate and the bro job tool call, and Claude's `Bash` stays blocked unless a `shell(...)` declaration hands it back.
   #754's pattern syntax and enforcement mechanism are settled in #754's own design, resumed before this slice starts.
3. **Trails.**
   Trail formats register through an entry-point group of their own, one module each, apart from the harness registry:
   a trail outlives the installation that recorded it, and the trails server loads no harness.
   Both of today's formats stay in core, since the trails server's image installs only the core `bro` wheel;
   a harness distribution that ships a new format needs that image to install it, in the order the rollout section gives.
   The server refuses a trail whose format no installed module registers, at blaze and on import, naming the format;
   the slice's tests hold that refusal on a server without a format and the same trail served by one with it.
   The group needs a name other than `bro.trails.formats`, which `bro/trails/formats.py` (the schema versions) already holds as a module path.
   The name comparisons become format members:
   whether a trail must name its bro, enforced on import as well as at blaze, where today only blaze checks it;
   the list owner;
   and the header fields the display shows.
   Lineage folding already keys off the format, through the adapter's `resolve_lineage`.
   The `body_encoding` fallback (`parse_json=harness != 'claude'` in `bro/trails/server/dynamo.py`) serves rows spilled before that field existed, which may all be gone:
   the stage counts rows with `body_s3` and no `body_encoding` in the live table, through a session with AWS access, and when there are none deletes the branch, so such a row fails loudly, or else makes it a format member.
   `native/bro/fork.py` refusing another harness's trail is the bro harness checking its own records, and stays.
4. **Persona declarations, with #857.**
   A persona declares its reach in neutral groups, opt-in on every harness:
   files (read-only or read-write), shell (per #754), web, and delegation.
   Groups are inherited like `tools`, so the base `bro` persona declares none
   — a group on `Bro` would reach `browser` and `lead` —
   and the bare `bro` persona keeps only the loop tools on Claude.
   A harness serves a group with its own strongest tool where it has one and an equivalent elsewhere, and `bro show` lists a group a harness cannot serve as unserved there.
   Claude passes exactly the natives the groups map to through `--tools`, an allowlist, plus the loop tools the slice lists:
   among them `Skill`, since sessions rely on Claude's own skill loader, and the `LSP` tool the pyright plugin every session enables needs.
   `ToolSearch` is not what keeps ride's MCP tools reachable, since they load eagerly (`alwaysLoad`);
   leaving it out only stops Claude deferring its own tools.
   Everything else is off, including the natives #857 lists, and natives a Claude Code bump adds stay off until mapped.
   The bro harness serves files with the `dev` toolset, which moves into `bro-native`, and leaves web and delegation unserved, delegation until #863.
   Every registered persona declares the groups it reaches today, settled by the slice's inventory of the natives each one uses:
   `terminal` without delegation, which it blocks, and `browser` with none of the four.
   `bro/harness/claude.py`'s tool names move into `ride/ride/claude/`.
   With no component conditioned on the harness, a bro has one selection, and the `bro`-harness special case in `_components_for` and `assemble` goes.
   #857 closes with this slice.
5. **Prompts.**
   What remains conditions on a fact the harness declares, or inserts a passage it supplies, through `Harness.facts()`:
   `tool_names.md` inserts the harness's tool-name rule, and `holds/attended.md` and `holds/detached.md` insert Claude's passage on skipped permission prompts, which is empty elsewhere.
   The gate-timeout lines in `run-pr.md` and `bump-bro.md` become one sentence true on both harnesses.
   `#harness` stays a fact for a case no fact covers, unused in the repository;
   `bro/prompts/AGENTS.md` and the template and conditions references state the rule.

### Rollout and mixed versions

No landing changes a wire, store, or record format that two separately deployed processes share:

- One installation runs one revision:
  its distributions ship from one commit (`BOOTSTRAP.md`), and `ride` freezes the whole installation into a runtime bundle.
- Every party tree runs its root's bundle, spawned and manual children included, and a resume runs the bundle its workspace recorded, so old and new code never meet in one session tree;
  roots started before an install keep their bundle.
- The watch store, the waiter's registration, and the stand-down mark live in one session's state dir and are read only by that session's own processes.
  Hooks travel in each launch's `--settings`, never in the workspace's Claude state, so a resumed workspace gets the hooks of the bundle it runs.
- The trails server is the one separately deployed reader:
  an ECS service whose image installs only the core `bro` wheel, deployed through the `trails-server` target in `oops/deploy_targets.py`.
  No landing changes what a trail records or what the server serves, so either side may run the older revision;
  the rewake reaches a Claude transcript as records the projection already handles, an unknown attachment as a notification and a user record as input, which the `llm` stage's input-channel probe holds.
  The trails landing changes the server's own code, so the server redeploys after it merges.
- A trail format a harness distribution adds later reaches the trails server first:
  the distribution goes into the server's image and deploys before any installation records in that format, since the server refuses an unknown format at blaze and a session whose recording fails stops.
  The format stays installed while any retained trail uses it, so retiring it waits until no recorder writes it and its trails are gone.
- The benchmark launcher reads harness names off the manifest of the bundle it builds rather than its own interpreter.
  A bundle built before the registry carries the old manifest format, which the launcher refuses as stale, naming `benchmark bundle` to rebuild it, so a new launcher never reads an old bundle as carrying no harness.
- A repository that pins the framework adopts each landing through its own bump, which adapts its persona declarations and prompts, and runs the revision it pins until then.

Each landing goes live by installing the launcher from master and starting a fresh root;
the trails landing deploys the trails server between the two.

### Risks

- `rewakeMessage` and `rewakeSummary` are marked internal.
  A pin bump re-probes them with the rest of the waiter's behavior;
  without them the model reads watch lines under a failing-hook label.
- The waiter on `StopFailure` is read off the binary, not probed:
  should `asyncRewake` not background there, a one-shot session whose turn errors is left without a waiter until its timeout, and the `llm` stage records the fallback it takes in the Design changelog.
- Claude's turn-end handling moves from the `Stop` input's `background_tasks` to the `background_tasks_changed` stream event;
  both are declared in Claude Code's schemas, and the `llm` stage holds the event against the pin.
- Each session has one reader:
  a reader run beside the waiter or the inbox would take lines from it, and with `watch-next` gone none ships.
- `bro-native` depends on `bro-ride`, which adds certifi to a bare `bro run` install, and the root map's layering line changes to ride loading `bro-native` through the group.
- A misspelled single-branch `{{when #harness = …}}` renders as false instead of raising;
  the prompts slice leaves the repository no such condition.
- A one-shot session that keeps a background task alive after its notice now waits for it on both harnesses, where a `bro run` used to end;
  a summoned session stays bounded by its timeout.
- Opt-in natives can withhold a tool a persona relied on without naming it, such as LSP or plan mode;
  the persona slice inventories the natives before switching, and `bro show` lists each persona's reach per harness.

### Rejected

- **Blocking waits on Claude**, possible once #846 lifts the MCP bounds to 24 hours:
  a blocking call waits on one quest while the session's other events go unread, holds the turn until the TUI backgrounds it after 120 s, and keeps a Claude-only tool shape the bro harness cannot share.
- **The runner writing watch lines into stdin:**
  one-shot only, since a TUI has no such channel, so two mechanisms.
- **The end-of-turn rule in a synchronous `Stop` hook, as today:**
  its verdict cannot reach the runner's stdin decision without shared state, and `stop_hook_active` is true on every turn a rewake starts.
- **A waiter per watch, or a reader per consumer:**
  readers race on offsets, and a session has one model to wake.
- **A session watch over every owned mission (`mission watch`):**
  a session driving a worker through its own tools, as the browser drives a webview through `webview::command`, would get each reply twice.
- **Keeping the model's watch commands behind a better guard:**
  the model keeps paying turns and shell admissions for plumbing (#793, #677).
- **Two entry-point groups, one per half, or ride's session types moved into core:**
  settled with the user for one group and one object per harness.
- **Trail formats on the harness object, or in the harness distributions:**
  a trail outlives the installation that recorded it, and the trails server installs only core.
- **The installed names as the `#harness` domain:**
  a text naming a harness the installation does not carry would raise, breaking a Claude-only install;
  settled with the user for checking names where they are used.
- **Removing `#harness`:**
  a consumer may still need a fork no fact covers, and the rule is to avoid one, not to forbid it.

### Landings

1. **#850, on the interface**, through one integration branch cut from master now that #846 has landed, in stages, each leaving the branch's gate green:
   1. the registry and the interface skeleton, with the bro harness moving into `bro-native` and no behavior change;
   2. the store and its producers:
      `take()`, `watch-run` under the job supervisor, the session watch armed and stopped by `do-ride`, and `bro::watch` and `bro::unwatch`;
   3. the end-of-turn rule with its notice template, and one stream behind `quest watch` and `mission watch`;
   4. the bro harness's port;
   5. the Claude harness's port;
   6. the quest tools, the fold, `watch-next`, the prompts, and the docs.

   Stages 4 to 6 change what the model is told and how it waits, so the branch reaches master once.
2. Service tools.
3. Tool fold, after #754's design.
4. Trails.
5. Persona declarations, with #857, after the tool fold.
6. Prompts.

#863 (delegation through summons) follows #850 on its own.

### Verification the #850 stages owe

- **The registry and the names (stage 1):**
  the installed names read from entry-point metadata without importing a harness, and a name loading its object lazily;
  loading refusing an object that is not a `Harness` and one whose `name` differs from its entry;
  ride's `--harness` choices and the summon check reading the registry;
  a launch or summon selecting an uninstalled harness failing with the distribution to install;
  `ride list` showing no subject for a workspace with no resume spec or an uninstalled harness;
  a Claude-only installation
  — the `bro` and `bro-ride` wheels alone in a fresh venv, as `ride/ride/runtime_bundle_test.py` builds one —
  composing the core `bro` persona and a Claude session's prompt;
  and the benchmark launcher checking a trial's harness against its bundle's manifest, refusing a manifest of the old format, and running `ride check-harness` in its setup.
- **Unit tests:**
  `take()` (the bounds, the pending marker, the offsets, two readers under the lock, the turns watches take, a line wider than the byte bound);
  the waiter's supersession and stand-down;
  each verdict of the rule and their order over mixed live sets, the owner-led browser's wait among them, with the once-per-set memory, and a dead session watch turning an awaited reply and a speaking summoner into a notice and an end;
  arming per admission rule, and the session's producers stopped at its end;
  a stopped watch's whole process group gone, the supervisor exiting once its owner's handle closes, and a `Runner` that exits stopping its watches while the process embedding it lives on;
  `bro::watch` admission, and `bro::unwatch` refusing the session watch;
  a re-arm after a gap replaying nothing it already yielded;
  a resumed session's re-armed watch delivering no line its previous run delivered, and a joined member's watches kept in its own store, out of its summoner's;
  the native pump's one batch in flight, the native one-shot idle and wake, and watch lines waking an idle `bro chat` while a human's message between turns still goes through;
  the Claude runner's verdicts over the fake claude, `ambient` tasks excluded.
- **`llm` stage, against the pinned Claude Code**, replacing `stop_guard_llm_test.py`:
  an idle wake in stream-json and in the TUI;
  a mid-turn delivery;
  `rewakeMessage` and `rewakeSummary` honored;
  `background_tasks_changed` reporting a background task;
  an end with a waiter pending exiting without the 30-second delay;
  a waiter started by `StopFailure` after a turn that ends in an API error;
  and the rewake's lines recorded in the trail, in `input_channels_llm_test.py`, which holds one representative of every model-input channel.
  It needs a `claude_code` credential.
- **e2e (`ride/ride/e2e_test.py`):**
  a summoned child's question and end reach a Claude summoner and a bro summoner through the session watch, with no watch command in either transcript.
  The Claude summoner is the e2e's fake `claude`, which runs the `Stop` hook command from its `--settings` at each turn end and records what the hook delivers, so the waiter's take and exit run in CI's Docker stages;
  Claude's own rewake is the `llm` stage's.
