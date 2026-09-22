# bro-native

`native/` is the `bro-native` uv workspace member.
It publishes the framework's native LLM engine and the `bro` command, and depends on the core `bro` distribution.
Core and `bro-ride` never import it.
The root repository owns formatting, lint, typing, packaging policy, and the test gate.
Build this member with `uv build --package bro-native`;
regenerate its scripts and committed `bro/native/_entrypoints.py` with `sync-scripts --project native`.

## Components

- `bro/native/` — the bro-native engine, the layer above the framework core:
  runner, live LLM contract, provider dispatch, and provider clients.
  It imports `bro`, never the reverse, so declaring and inspecting a persona costs nothing of the loop that runs one.
  `runner.py`'s `Runner(bro)` drives one declaration and owns the per-run LLM, observer, tracker, inbox, job registry, broker channel, and trail;
  it satisfies `bro.bro.LiveRun` and builds its toolset through `BaseBro.assemble(harness='bro', ...)`.
  OpenAI drains the inbox's notifications after tool batches or into an idle turn, delivering them as user-role input;
  interactive owners call `wake()` when the inbox reports news.
  The session text tells a run to arm `quest watch` as a watch-mode job when it can receive summon traffic and use `chill` as its idle wait.
  A one-shot run ends when a turn ends with nothing running and nothing in flight;
  otherwise the runner posts one notice naming the live jobs and every mission the session owns (`bro.mission.live_missions`) through the inbox and runs one more turn, and the registry closes only at the end (`bro/reference/ride.md`, "Bro harness").
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
