# bro-native

`harnesses/native/harness/` is the `bro-native` uv workspace member.
It publishes the framework's native LLM engine, the registered `bro` harness, and the `bro` command.
It depends on core, `bro-ride` and `bro-native-trails`;
core never imports it, and ride reaches it only by loading its `bro.harnesses` entry.
The root repository owns formatting, lint, typing, packaging policy, and the test gate.
Build this member with `uv build --package bro-native`;
regenerate its scripts and committed `bro/native/_entrypoints.py` with `sync-scripts --project harnesses/native/harness`.

## Components

- `bro/native/` — the bro-native engine, the layer above the framework core:
  registered session harness, runner, live LLM contract, loop, and diagnostic echo provider and wire.
  `harness.py:BRO` owns native recipe resolution among providers whose wires have clients, session preparation and runtime checks, and terminal service-tool delivery by raising to the runner.
  Its `Harness.facts` contributes native prompt passages, `Harness.prompt_instructions` supplies the skill-loader contract, and `Harness.own_tools` contributes `skill` and the shell-gated job tools.
  Its `Harness.serve` mounts `dev_mcp.py`, the file and search toolset whose shared output rules are `dev_reference.md`, for the `files` group.
  Path-taking dev tools consult `bro.files.access`;
  the seam owns file opens and authorizes search roots.
  It also owns the `bro run|chat …` spawn on the session's admitted recipe, its continuation, and ride's launch hooks.
  It imports `bro`, never the reverse, so declaring and inspecting a persona costs nothing of the loop that runs one.
  `runner.py`'s `Runner(bro, recipe)` drives one declaration and owns the per-run LLM, observer, tracker, inbox, job registry, broker channel and trail;
  it also owns an in-process run's temporary watch store and, outside a managed session, its temporary result store;
  it satisfies core's trail-and-tool-position `bro.bro.LiveRun` and the bro harness's run contract, and injects the registered `BRO` object into `BaseBro.assemble` and `BaseBro.system_prompt_for`.
  `bro/jobs.py` and `bro/inbox.py` are native-owned modules under their public namespace paths:
  jobs supervise process groups through core's `bro.job_supervisor`, spool bounded output, and live in a lifetime-scoped registry, while the inbox wakes the model on job news and framework notices and drains their bounded notification slices.
  A job or watch line runs in brash under the policy `Runner` writes from the bro's reach into a directory its lifetime owns, where the command list is finite, and in bash otherwise.
  `Loop` drains the inbox's notifications after tool batches or into an idle turn, delivering them as user-role input;
  interactive owners call `wake()` when the inbox reports news.
  In a managed session, `do-ride` owns the shared watch store and arms its `mission watch` producer;
  an in-process `bro run|chat` keeps model-started watches in the temporary store its `Runner` tears down.
  A pump hands the inbox each watch batch as `watch_delivery` renders it.
  At each one-shot turn end, `Runner` applies the shared `bro.turn_end` rule:
  it reports running jobs as harness background work, posts notices through the inbox, ends by returning the last reply, and otherwise idles on the inbox until job or watch news, or a job's silence notice, wakes the next turn.
  `bro chat` uses the same inbox to wake on watch lines while accepting a human's next message between turns (`bro/reference/ride.md`, "Bro harness").
  `llm.py` owns the live `LLM` ABC and diagnostic CLI, which requires `--provider`.
  `loop.py` opens the recipe's wire through core's roster and owns the provider-neutral loop:
  tool execution, inbox drains, observer events, recorded steps, interrupted inputs and cumulative usage.
  Cancellation after a leg arrives completes its recording and answers every unanswered call with an interrupted error result.
  Results already obtained and inputs not yet exchanged remain pending ahead of the next turn.
  Capacity retries record and count every refused attempt with the same exchange indices and inputs, without advancing the wire.
  A non-completed leg is recorded with `stopped`, counted in usage, and refused with `LLMStopped` before observer events or tools run; its inputs remain pending.
  Its `restore` and `seed_chain` delegate to the wire;
  the Responses client chains on the server and Chat rebuilds history locally.
  Wire clients encode tool results; recording keeps the original object or string.
- `bro/run.py` (`bro`) — lightweight CLI dispatcher shipped by `bro-native`:
  `bro run` and `bro chat` import the native launcher implementations only when selected;
  `bro list` and `bro show <name> [--system-prompt HOLD]` remain metadata paths (card renderer in core `show.py`);
  `bro --version` prints the `bro` distribution's version and the commit it runs (core `source_commit.py`)
- `bro/launch/{run,call,call_tui,resume}.py` — native one-shot and interactive launch surfaces
- `bro/fork.py` — fork of a recorded trail.
  `replay_prefix` gathers the steps through the selected fork point and follows `forked_from` ancestors when needed;
  `latest_fork_point` chooses the newest consistent point using the recorded wire’s decoded calls, skipping steps marked `stopped`.
  `fork(..., surface=<caller>)` requires the driving program's surface, returns a `Runner` preseeded with the replayed prefix, and opens the child trail with `ForkedFrom(trail_id, step_id)`;
  it replays bro-native records, so a trail from another harness is refused up front rather than partway into a header that never carried what replay needs.
  A fork runs under the hold it is given, guided when omitted, and recomposes the prompt under it when the recorded hold differs.
  Every replayed call must use the target recipe's wire, checked before building the runner.
  Same-provider/model forks at an `llm_call` under an unchanged prompt seed a chain only on a server-chained wire;
  every other case asks the wire to restore the prefix from the steps' own records.
  A step marked `stopped` cannot seed a chain and is omitted by restore; unmarked records, including accepted incomplete responses, replay whole.
  Consumed by `bro chat --fork` and managed bro-harness continuation (`--at <step_id>` overrides the fork point).
- `bro/trails/record/bro.py` — native tracker-to-trails recorder

## The run

What `Runner` owns around one run, beyond the loop itself.

### Observing

A run renders only through the `bro.llm.observer.Observer` its caller passes
— the default is `NullObserver`, so an embedding application never gets terminal output it did not ask for;
the launch surfaces pass a trails-display observer per their preset (`bro/launch/AGENTS.md`, "Display and holds").
Providers emit only model and tool activity (`bro/llm/AGENTS.md`, `observer.py`);
`Runner.run()` / `send()` own the turn boundaries and emit the exact returned completion once, including provider fallback extraction.
A missing-credential refusal is a failed one-shot run and a terminal interactive reply.

### Usage publishing

The runner's LLM construction passes `agent=bro.agent` (the `bro//<name>` surface identity), so the provider publishes its cumulative per-model usage under that identity to the env-pointed usage file (`bro.llm.usage`) after every LLM call.
Tool subprocesses inherit the pointer, which is how `bro.workflow.commit_footer` credits a native bro run's commits;
the file carries the agent identity itself because an in-process run's `RIDE_BRO` is the launcher's, not the bro's.

### Recording

Each `Runner.run()` or first `.send()` opens a trail through a `bro.llm.tracker.Tracker` and emits the opening `system_prompt` step;
the same tracker is plumbed into the LLM so provider implementations record the replayable native stream,
with each `llm_call` tagged by its wire,
each step through the base `LLM._track_step`, which also touches the activity file a managed session hands the `bro` command (`--activity-file`).
The runner's context-managed lifetime ends that trail once with clean → `ok`, `BroRaised` → `raised`, and other exceptions → `error`;
`run()` supplies its own lifetime, while interactive owners keep one around the conversation.
`run()`, `send()`, and `bro.fork.fork()` require the caller to name the driving `surface`.
The default factory builds a `bro.trails.record.bro.Recorder`;
a per-run `tracker=` argument or `set_default_tracker_factory` replaces it, and `TRAILS_DISABLED` makes the default a `NullTracker` (`bro/trails/AGENTS.md`, which owns the recording subsystem).

`bro`, `bro.launch`, `bro.trails`, and `bro.trails.record` are shared namespace package trees.
This member's source root contains only native-owned leaves, so its build can publish that `bro` portion whole without reaching another member's source tree.
