# bro/launch

The shared `bro.launch` namespace spans core and `bro-native`.
Core owns the launch vocabulary every harness uses
— the LLM flag set, session hold, and host-session broxy.
`bro-native` owns the public in-process `bro run` / `bro chat` surfaces under `harnesses/native/harness/bro/launch/`;
they execute the selected bro in the calling process with ambient credentials.
Managed workspaces belong to `bro-ride`.
Core imports no other workspace member.

## In-process CLIs

The following modules ship from `bro-native`:

- `run.py` — parser and execution for `bro run <bro> <input>`.
  It accepts the native LLM flags and `--hold`.
  It creates the bro under the resolved native recipe, runs it under the ask display preset, and records with surface `ask`.
- `call.py` — parser and UI for `bro chat <bro> [what]`.
  An omitted message opens an empty REPL.
  The stream UI owns stdin through one daemon reader and races its line queue against the run inbox without abandoning a read.
  The Textual UI awaits the same inbox in a worker;
  either calls `Runner.wake()` when background news arrives while idle.
  `--fork [TRAIL_ID] [--at STEP_ID]` forks recorded history under the bro class's current recipe;
  omitting the id selects the bro's newest recorded call.
  The suppressed `--resolved-llm` (here and on `bro run`) is the managed bro harness's exact-recipe contract:
  it carries the recipe the launch admitted and excludes the selection flags;
  the suppressed `--continue-trail` requires it and continues a recorded trail under it.
- `call_tui.py` — Textual `ChatApp`, turn cancellation, message input, and the display-session integration.
- `resume.py` — recorded-history projection and `bro.fork.fork` orchestration used by public history forks and managed native continuation.

Core's `llm_flags.py` provides shared `--provider` / `--model` / `--effort` / `--fast` / `--passthrough` / `--llm` registration, preset expansion, and canonicalization.
Recipe resolution belongs to each harness implementation.
`bro-native`'s `bro/run.py` is the lightweight dispatcher;
it imports `run.py` or `call.py` only after selecting a launch verb, so metadata commands do not pull in the launcher stack.

The public verbs deliberately accept no runtime shaping flags (`--summon`, `--cred`, `--grant`, `--revoke`, `--into`, `--no-trails`) and have no in-container refusal.

### Display and holds

`bro run` uses the ask preset:
live activity on stderr and one undecorated reply on stdout.
Its omitted hold is `unattended`.
Its one-shot process follows the ordered verdict in `bro.turn_end.settle` (`bro/reference/ride.md`, "Bro harness").

`bro chat` uses the chat preset and defaults to `guided`.
Between user turns, both text and Textual modes wait on the run inbox and call `Runner.wake()` when a watch reports news.
Both modes can interrupt a running turn and render `call.INTERRUPTED_NOTICE`;
the TUI prevents concurrent sends.
The opening banner is a trusted surface notice rendered by `bro.workspace.banner.render_banner`, not conversation input.
A recorded conversation's exit hint names `bro chat <bro> --fork <trail-id>`.

The human aliases are not part of this distribution:
`bro-ride` declares `ask` as `ride solo` and `call` as `ride along`.
They add no implied `--fast` or other defaults.

## Naming the LLM

Every native run and every managed mode registers one flag set from `llm_flags.py`.
`--llm` takes the recipe grammar in `bro/reference/ride.md`, “Recipe selection”, or a preset from the project's `[tool.bro.llm]` table / host `~/.bro.json`.
Managed launches carry recipe inputs to the host, which expands presets and resolves them under the root reading.
The admitted recipe reaches the inner session through its resolved environment.
Native in-process surfaces canonicalize their selection before launching.

Each harness resolves the selection through `bro.harness.Harness.resolve_recipe`, among the providers it declares in `llm_providers`;
`harnesses/native/harness/bro/native/harness.py:resolve_native` is that resolution over the bro's optional declared `llm_spec`.
In-process launches have no host recipe list; managed starts follow `bro/reference/ride.md`, “Recipe selection”.
Parsing validates grammar only; preset expansion recognizes a bare word that names no installed provider, with host presets overriding the project's.
A recipe for a provider the harness does not serve is refused, naming the installed harnesses that serve it.
Harness flags choose a recipe within the selected harness and never silently switch execution shape.

## Session support

- `hold.py` — the session hold as the environment carries it, and the interactive-session predicate over it.
- `broxy.py` — host-session wrapper around `broxy launch`, consuming `BROKER_UPSTREAM`, publishing the session-local `BROKER_CHANNEL` only after readiness, owning the session broxy-log path, and pairing setup with context-managed teardown.
