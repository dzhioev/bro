# bro/launch

Framework launch support: the public in-process `bro run` / `bro chat` surfaces and the launch vocabulary other surfaces share — the LLM flag set, the session hold, the in-place session broxy. The public verbs execute the selected bro in the calling process with ambient credentials; managed workspaces belong to the `bro-ride` distribution. Nothing here imports `ride`.

## In-process CLIs

- `run.py` — parser and execution for `bro run <bro> <input>`. It accepts the native LLM flags, `--hold`, and a suppressed `--in-place` no-op. It creates the bro under the resolved native recipe, runs it under the ask display preset, and records with surface `ask`.
- `call.py` — parser and UI for `bro chat <bro> [what]`. An omitted message opens an empty REPL. The Textual UI is used when both terminal streams and its dependencies are available, the stream UI otherwise. `--fork [TRAIL_ID] [--at STEP_ID]` forks recorded history under the bro class's current recipe; omitting the id selects the bro's newest recorded call. The suppressed `--continue-trail` / `--continue-llm` pair is the managed bro harness's exact-recipe continuation contract. `--in-place` is a suppressed no-op.
- `call_tui.py` — Textual `ChatApp`, turn cancellation, message input, and the display-session integration.
- `resume.py` — recorded-history projection and `bro.fork.fork` orchestration used by public history forks and managed native continuation.
- `llm_flags.py` — shared `--provider` / `--model` / `--effort` / `--fast` / `--llm` registration, preset expansion, canonicalization, and per-harness resolution.
- `bro/run.py` stays the lightweight dispatcher. It imports `run.py` or `call.py` only after selecting a launch verb, so metadata commands do not pull in the launcher stack.

`--in-place` is accepted as the bro harness's inner-argv token. It changes nothing: the public verbs always run in the current process and use the current credential environment. They deliberately accept no runtime shaping flags (`--summon`, `--grant`, `--revoke`, `--into`, `--no-trails`) and have no in-container refusal.

### Display and holds

`bro run` uses the ask preset: live activity on stderr and one undecorated reply on stdout. Its omitted hold is `unattended`.

`bro chat` uses the chat preset and defaults to `guided`. Both text and Textual modes can interrupt a running turn and render `call.INTERRUPTED_NOTICE`; the TUI prevents concurrent sends. The opening banner is a trusted surface notice rendered by `bro.workspace.banner.render_banner`, not conversation input. A recorded conversation's exit hint names `bro chat <bro> --fork <trail-id>`.

The human aliases are not part of this distribution: `bro-ride` declares `ask` as `ride solo` and `call` as `ride along`. They add no implied `--fast` or other defaults.

## Naming the LLM

Every native run and every managed mode registers one flag set from `llm_flags.py`. `--llm` takes `provider:model:effort` with an optional `+fast` suffix and any field left empty (`:fable5`, `::high`, `openai:sol:max+fast`), or a preset from the project's `[tool.bro.llm]` table / host `~/.bro.json`. A surface canonicalizes the selection once before forwarding or recording it.

`resolve_native` puts the selection over the bro's declared `llm_spec`; a provider driven by another harness errors and points at `ride --harness`. Harness flags choose a recipe within the selected harness and never silently switch execution shape.

## Session support

- `hold.py` — the session hold as the environment carries it, and the interactive-session predicate over it.
- `broxy.py` — host-session wrapper around `broxy launch`, giving an in-place runner a session-local channel proxy with context-managed teardown.
