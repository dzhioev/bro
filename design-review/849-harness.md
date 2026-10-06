## Design

Settled with the user on 2026-10-05 (trail `01m45h783d-2jkg13yd-07dpdhqf`).

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
  An async hook's default timeout is 10 minutes;
  expiry SIGTERMs the hook and wakes nothing, leaving the session with no waiter.
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

### The harness interface

One object per harness implements the whole harness, and the `bro.harnesses` entry-point group registers it as `<name> = "<module>:<object>"`.
General code reaches a harness through that object, passed in by the engine that drives the session or loaded by name through the registry, and compares no harness names.

- **Core.**
  `bro/harness/__init__.py` holds `Harness`, the base class of the framework half, and the registry:
  the installed names, read from entry-point metadata without importing anything, and the harness a name loads.
  As with `bro.worker_types`, loading refuses an object that is not a `Harness` or whose `name` differs from its entry.
- **Ride.**
  `ride.harness.SessionHarness` is today's `Harness` protocol under a new name:
  scope recipe, auth preflight, LLM resolution, the session reads and run, and the boxed and unboxed extras.
  It gains `prepare_session`, which takes over `do_ride._prepare_claude_state`.
  Ride resolves a harness through the core registry and refuses one that does not implement it.
- **Claude.**
  `ride/ride/claude/harness.py:CLAUDE`, registered as `claude` by `bro-ride`.
- **Bro.**
  `native/bro/native/harness.py:BRO`, registered as `bro` by `bro-native`, which takes over `ride/ride/bro.py` and gains a dependency on `bro-ride`, as `bro-webview` has.
  Ride loads `bro-native` only through the group, never by import.
- **Names.**
  Every list of harness names reads the registry:
  `bro.mcp.Harness`, `ride.harness.HARNESS_NAMES` and `get_harness`'s if-chain, ride's `--harness` choices, `bro_worker`'s summon check, and `bro.summon`'s help;
  so do the `[tool.bro] harness` and `summon-harness` validation in `bro/workspace/project.py` and the Terminal-Bench adapter in `benchmark/`.
  The `#harness` fact's domain is the installed names.
  The defaults, `claude` for a launch and `bro` for a summon, are configuration values kept together in `bro.base.configs`.
- **No guessing.**
  A workspace with no resume spec records no harness, so `ride list` shows no subject for it rather than reading it as a Claude workspace.
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
  It ends a cut batch with a pending marker and commits the offsets past what it returns.
  A run with no session state dir, an in-process `bro run|chat` outside ride, keeps its watches in a temporary store its runner owns.
- **Producers.**
  `watch-run` runs its command under `bro.job_supervisor`, the process-group leader behind `bro::job`, detached from whatever process started the watch.
  Stopping a watch ends its whole process group, as `bro::kill` ends a job's.
  The supervisor itself exits once its owner is gone:
  for a watch the session that declared it, for a job the process that started it.
- **The session watch.**
  `do-ride` arms `watch-run quest watch` before the harness session starts and stops it after, wherever today's admission rule admits it:
  a session that may summon, or a summoned one whose talk carries `owner.say`, `owner.question`, or `worker.question`.
  The rule moves out of `_fold_tool_layers` into the module that arms the watch.
  When the session ends, `do-ride` stops every producer still live in its store.
- **Watches the model starts.**
  `bro::watch(command)` keeps an admitted command running for the rest of the session through the store, and `bro::unwatch(command)` stops it.
  Both mount wherever the persona declares shell reach, on every harness, and admit commands as `job` does;
  on the bro harness they replace `job`'s `watch` mode.
- **One stream.**
  `bro.mission` owns the stream behind both `quest watch` and `mission watch`:
  arming at the journal head, replaying retained chat, re-arming across a gap, and following the ordered events.
  `quest watch` is its view of the session's own quest and the bro missions the session owns.
- **One end-of-turn rule.**
  `bro/turn_end.py` settles a one-shot turn end from the live work:
  the missions the session owns, which of them the live session watch covers, the model's live watches, the harness's own background work, and whether a summoner may still speak or a question of the session's own awaits its reply.
  - **End** when nothing is live and no summoner may speak.
  - **Wait** silently on covered missions, live watches, or a question awaiting its reply.
  - **Notice, then wait** when the only live things are background work or a summoner who may speak:
    the notice names the work, says that ending the turn again keeps waiting, and, in a summoned session, that a finished result goes through `bro::answer`.
  - **Notice, then end** when the only live things are missions no watch covers:
    nothing would wake the session for them, so the notice names them and the routes (`bro::watch('mission watch')`, or cancelling them), and ending the turn again ends the run and orphans them.

  A notice comes once per distinct live set, from one text template both harnesses share.
  An interactive session has no rule:
  it idles, wakes on lines, and takes a human's message at any time.

#### The port

Each harness implements four operations for its sessions, behind two protocols in core:

- `LineSink.deliver(batch)` puts lines into the model's context and wakes the session if it idles;
  the general pump hands it each batch `take()` returns.
- `TurnEnd.background_work()` reports the harness's own live background work, and `TurnEnd.notify(text)` and `TurnEnd.end()` carry out a notice and an end;
  the harness calls `turn_end.settle(port)` at each one-shot turn end.

#### Claude (`ride/ride/claude/`)

- **The waiter.**
  `watch_waiter.py` is every session's `Stop` hook, with `asyncRewake`, `rewakeSummary: "watch lines"`, `rewakeMessage: "New lines from this session's watches:"`, and a `timeout` of 24 hours.
  It registers as the session's current waiter, and a waiter it supersedes exits 0 at its next poll.
  It also exits 0 once the session ends, on the runner's stand-down mark or when `RIDE_RUNNER_PID` is gone.
  It polls the store every 0.5 s, and on a batch writes it to stderr and exits 2.
  A minute before its bound it exits 2 with a line saying no watch line arrived for 24 hours and that ending the turn keeps waiting, so the bound shows rather than leaving the session without a waiter.
- **The one-shot runner.**
  `interrupt.run_streaming` settles each turn end at its `result` event.
  Background work is the task list of the last `background_tasks_changed` event, which the SDK's wire schema types.
  A notice goes in as a user message on stdin;
  an end stands the waiter down, then closes stdin.
  The runner also stands the waiter down before interrupting Claude, so a pending waiter adds no 30-second exit delay.
- **Removed.**
  `stop_guard.py` and `watch_delivery.py` go with their hooks, and `watch_commands.py` with them.

#### Bro (`native/`)

- A pump thread delivers each batch into the run's inbox, which already reaches the model after tool results and wakes an idle `bro chat`.
- `Runner` settles each one-shot turn end:
  background work is the running jobs, a notice goes through the inbox, an end returns the reply, and a wait idles on the inbox and wakes on its news, as `bro chat` does.
- `bro::chill` goes, and with it the turn it held, which is what kept a human from typing in `bro chat`.

#### Tools and prompts in the same landing

- **The quest tools return at once on every harness:**
  `summon` on acceptance, `quest_ask` with the question id, `quest_check` and `quest_history` after one read, and `quest_cancel` once the host accepts.
  The Claude variants and their transport cautions go.
  With `quest_ask`'s `wait` goes the `counter_question_id` view #846 adds to it;
  the watch line already names both ids.
  The `summon` and `quest` CLIs keep their blocking forms for shells and humans.
- The fold stops admitting quest-watch commands, so a persona that declares no shell has none on either harness.
- `watch-next` goes;
  `watch-run` stays, as the producer.
- `summoner.md`, `summoned.md`, `ask.md`, the `watch` spell, `orchestrate.md`, `run-pr.md`, and #846's `browse.md` owner contract state one model with no `#harness` fork:
  the session watch is kept for the model, lines arrive as notifications, waiting is ending the turn, and quest moves go through the `bro::` tools.
  `fragments/watch.md` folds into the `watch` spell.
- `bro/reference/ride.md`, `bro/AGENTS.md`, `bro/prompts/AGENTS.md`, `ride/ride/claude/AGENTS.md`, `native/AGENTS.md`, and the root map follow.

#### Edge cases

- A burst larger than one batch ends with the pending marker, and the waiter started at the next turn end delivers the rest at once.
- A line that arrives during a turn is delivered within it by the waiter still waiting from the previous turn end;
  one that arrives after that waiter has exited is picked up at once by the next.
- A session watch whose producer died, because the broker refused it for instance, covers nothing:
  its exit line is delivered, and the rule counts its missions as uncovered.
- A resume re-arms the session watch, whose replay re-delivers retained pending chat marked `before the watch:`.
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
   Trail formats register in a `bro.trails.formats` group, one module each, apart from the harness registry:
   a trail outlives the installation that recorded it, and the trails server loads no harness.
   The name comparisons become adapter members:
   whether a trail must name its bro, the list owner, the header fields the display shows, text bodies for rows spilled before `body_encoding`, and whether the format folds lineage.
   `native/bro/fork.py` refusing another harness's trail is the bro harness checking its own records, and stays.
4. **Persona declarations, with #857.**
   A persona declares its reach in neutral groups, opt-in on every harness:
   files (read-only or read-write), shell (per #754), web, and delegation.
   A harness serves a group with its own strongest tool where it has one and an equivalent elsewhere, and `bro show` lists a group a harness cannot serve as unserved there.
   Claude passes exactly the natives the groups map to through `--tools`, an allowlist, plus the loop tools the slice lists, such as `ToolSearch`, which deferred MCP tools need.
   Everything else is off, including the natives #857 lists, and natives a Claude Code bump adds stay off until mapped.
   The bro harness serves files with the `dev` toolset, which moves into `bro-native`, and leaves web and delegation unserved, delegation until #863.
   `dev`, `eyebro`, `analyst`, `terminal`, `devoops`, and the local bros declare what they use today, delegation included.
   `bro/harness/claude.py`'s tool names move into `ride/ride/claude/`.
   With no component conditioned on the harness, a bro has one selection, and the `bro`-harness special case in `_components_for` and `assemble` goes.
   #857 closes with this slice.
5. **Prompts.**
   What remains conditions on a fact the harness declares, or inserts a passage it supplies, through `Harness.facts()`:
   `tool_names.md` inserts the harness's tool-name rule, and `holds/attended.md` and `holds/detached.md` insert Claude's passage on skipped permission prompts, which is empty elsewhere.
   The gate-timeout lines in `run-pr.md` and `bump-bro.md` become one sentence true on both harnesses.
   `#harness` stays a fact for a case no fact covers, unused in the repository;
   `bro/prompts/AGENTS.md` and the template and conditions references state the rule.

### Risks

- `rewakeMessage` and `rewakeSummary` are marked internal.
  A pin bump re-probes them with the rest of the waiter's behavior;
  without them the model reads watch lines under a failing-hook label.
- Claude's turn-end handling moves from the `Stop` input's undocumented `background_tasks` to the `background_tasks_changed` stream event, which the SDK's wire schema types.
- Each session has one reader:
  a reader run beside the waiter or the inbox would take lines from it, and with `watch-next` gone none ships.
- `bro-native` depends on `bro-ride`, which adds certifi to a bare `bro run` install, and the root map's layering line changes to ride loading `bro-native` through the group.
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
- **Trail formats on the harness object:**
  a trail outlives the installation that recorded it.
- **Removing `#harness`:**
  a consumer may still need a fork no fact covers, and the rule is to avoid one, not to forbid it.

### Landings

1. **#850, on the interface**, through one integration branch cut once #846 has landed, in stages:
   1. the registry and the interface skeleton, with the bro harness moving into `bro-native` and no behavior change;
   2. the general watching code:
      the store's reader, producers under the job supervisor, the session watch in `do-ride`, the rule, one stream, and `bro::watch` and `bro::unwatch`;
   3. the bro harness's port;
   4. the Claude harness's port;
   5. the quest tools, the fold, `watch-next`, the prompts, and the docs.

   Stages 3 to 5 change what the model is told and how it waits, so the branch reaches master once.
2. Service tools.
3. Tool fold, after #754's design.
4. Trails.
5. Persona declarations, with #857.
6. Prompts.

#863 (delegation through summons) follows #850 on its own.

### Verification the #850 stages owe

- **Unit tests:**
  `take()` (the bounds, the pending marker, the offsets, two readers under the lock);
  the waiter's supersession and stand-down;
  each verdict of the rule, with its once-per-set memory;
  arming per admission rule, and the session's producers stopped at its end;
  a stopped watch's whole process group gone, and the supervisor exiting once its owner is gone;
  `bro::watch` admission;
  the native one-shot idle and wake;
  the Claude runner's verdicts over the fake claude.
- **`llm` stage, against the pinned Claude Code**, replacing `stop_guard_llm_test.py`:
  an idle wake in stream-json and in the TUI, a mid-turn delivery, `rewakeMessage` and `rewakeSummary` honored, `background_tasks_changed` reporting a background task, and an end with a waiter pending exiting without the 30-second delay.
- **e2e (`ride/ride/e2e_test.py`):**
  a summoned child's question and end reach a Claude summoner and a bro summoner through the session watch, with no watch command in either transcript.
