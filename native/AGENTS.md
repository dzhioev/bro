# bro-native

`native/` is the `bro-native` uv workspace member.
It publishes the framework's native LLM engine, the registered `bro` harness, and the `bro` command.
It depends on core and `bro-ride`;
core never imports it, and ride reaches it only by loading its `bro.harnesses` entry.
The root repository owns formatting, lint, typing, packaging policy, and the test gate.
Build this member with `uv build --package bro-native`;
regenerate its scripts and committed `bro/native/_entrypoints.py` with `sync-scripts --project native`.

## Components

- `bro/native/` — the bro-native engine, the layer above the framework core:
  registered session harness, runner, live LLM contract, provider dispatch, and provider clients.
  `harness.py:BRO` owns native recipe resolution, session preparation and runtime checks, and terminal service-tool delivery by raising to the runner.
  Its `Harness.facts` contributes native prompt passages, and `Harness.own_tools` contributes `skill` and the shell-gated job tools.
  It also owns the `bro run|chat …` spawn with exact-recipe continuation and ride's launch hooks.
  It imports `bro`, never the reverse, so declaring and inspecting a persona costs nothing of the loop that runs one.
  `runner.py`'s `Runner(bro)` drives one declaration and owns the per-run LLM, observer, tracker, inbox, job registry, broker channel, trail, and an in-process run's temporary watch store;
  it satisfies core's trail-and-tool-position `bro.bro.LiveRun` and the bro harness's run contract, and injects the registered `BRO` object into `BaseBro.assemble` and prompt composition.
  `bro/jobs.py` and `bro/inbox.py` are native-owned modules under their public namespace paths:
  jobs supervise process groups through core's `bro.job_supervisor`, spool bounded output, and live in a lifetime-scoped registry, while the inbox wakes the model on job news and framework notices and drains their bounded notification slices.
  OpenAI drains the inbox's notifications after tool batches or into an idle turn, delivering them as user-role input;
  interactive owners call `wake()` when the inbox reports news.
  In a managed session, `do-ride` owns the shared watch store and arms its `quest watch` producer;
  an in-process `bro run|chat` keeps model-started watches in the temporary store its `Runner` tears down.
  A pump hands the inbox one bounded watch batch at a time and takes the next only after the model drains the last.
  At each one-shot turn end, `Runner` applies the shared `bro.turn_end` rule:
  it reports running jobs as harness background work, posts notices through the inbox, ends by returning the last reply, and otherwise idles on the inbox until job or watch news wakes the next turn.
  `bro chat` uses the same inbox to wake on watch lines while accepting a human's next message between turns (`bro/reference/ride.md`, "Bro harness").
  `llm.py` owns the live `LLM` ABC and diagnostic CLI, `providers.py` maps core `NativeLLMSpec` recipes to engine clients, and `llms/{openai,echo}.py` contain those clients.
- `bro/run.py` (`bro`) — lightweight CLI dispatcher shipped by `bro-native`:
  `bro run` and `bro chat` import the native launcher implementations only when selected;
  `bro list` and `bro show <name> [--system-prompt]` remain metadata paths (card renderer in core `show.py`)
- `bro/launch/{run,call,call_tui,resume}.py` — native one-shot and interactive launch surfaces
- `bro/fork.py` — fork of a recorded trail.
  `replay_messages` rebuilds the provider input through the selected fork point and follows `forked_from` ancestors when needed;
  `latest_fork_point` chooses the newest consistent point.
  `fork(..., surface=<caller>)` requires the driving program's surface, returns a `Runner` preseeded with the replayed prefix, and opens the child trail with `ForkedFrom(trail_id, step_id)`;
  it replays bro-native records, so a trail from another harness is refused up front rather than partway into a header that never carried what replay needs.
  Same-provider/model forks at an `llm_call` use the provider's `previous_response_id`;
  every other case replays the prefix client-side.
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
the same tracker is plumbed into the LLM so provider implementations record the replayable native stream.
The runner's context-managed lifetime ends that trail once with clean → `ok`, `BroRaised` → `raised`, and other exceptions → `error`;
`run()` supplies its own lifetime, while interactive owners keep one around the conversation.
`run()`, `send()`, and `bro.fork.fork()` require the caller to name the driving `surface`.
The default factory builds a `bro.trails.record.bro.Recorder`;
a per-run `tracker=` argument or `set_default_tracker_factory` replaces it, and `TRAILS_DISABLED` makes the default a `NullTracker` (`bro/trails/AGENTS.md`, which owns the recording subsystem).

`bro`, `bro.launch`, `bro.trails`, and `bro.trails.record` are shared namespace package trees.
This member's source root contains only native-owned leaves, so its build can publish that `bro` portion whole without reaching another member's source tree.
