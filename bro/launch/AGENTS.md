# bro/launch

Framework launch support. Public `bro run` and `bro chat` execute the selected bro in the calling process with ambient credentials; managed workspaces belong to the `bro-ride` distribution. This package also owns the framework-side machinery `ride` and summon lowering share: native recipe resolution, scope computation, broker-root supervision, and bro-run launch descriptions. Nothing here imports `ride`.

## In-process CLIs

- `run.py` — parser and execution for `bro run <bro> <input>`. It accepts the native LLM flags, `--hold`, `--rich`, and a suppressed `--in-place` no-op. It creates the bro under the resolved native recipe, runs it under the ask display preset, and records with surface `ask`.
- `call.py` — parser and UI for `bro chat <bro> [what]`. An omitted message opens an empty REPL. `--text` selects the stream UI; auto mode uses the Textual UI when both terminal streams and its dependencies are available. `--fork [TRAIL_ID] [--at STEP_ID]` forks recorded history under the bro class's current recipe; omitting the id selects the bro's newest recorded call. The suppressed `--continue-trail` / `--continue-llm` pair is the managed bro harness's exact-recipe continuation contract. `--in-place` is a suppressed no-op.
- `call_tui.py` — Textual `ChatApp`, turn cancellation, message input, and the display-session integration.
- `resume.py` — recorded-history projection and `bro.fork.fork` orchestration used by public history forks and managed native continuation.
- `llm_flags.py` — shared `--provider` / `--model` / `--effort` / `--fast` / `--llm` registration, preset expansion, canonicalization, and per-harness resolution.
- `bro/run.py` stays the lightweight dispatcher. It imports `run.py` or `call.py` only after selecting a launch verb, so metadata commands do not pull in the launcher stack.

`--in-place` is accepted as the bro harness's inner-argv token. It changes nothing: the public verbs always run in the current process and use the current credential environment. They deliberately accept no runtime shaping flags (`--summon`, `--grant`, `--revoke`, `--into`, `--no-trails`) and have no in-container refusal.

### Display and holds

`bro run` uses the ask preset: live activity on stderr and one undecorated reply on stdout; `--rich` changes only the activity renderer. Its omitted hold is `unattended`.

`bro chat` uses the chat preset and defaults to `guided`. Both text and Textual modes can interrupt a running turn and render `call.INTERRUPTED_NOTICE`; the TUI prevents concurrent sends. The opening banner is a trusted surface notice rendered by `bro.workspace.banner.render_banner`, not conversation input. A recorded conversation's exit hint names `bro chat <bro> --fork <trail-id>`.

The human aliases are not part of this distribution: `bro-ride` declares `ask` as `ride solo` and `call` as `ride along`. They add no implied `--fast` or other defaults.

## Naming the LLM

Every native run and every managed mode registers one flag set from `llm_flags.py`. `--llm` takes `provider:model:effort` with an optional `+fast` suffix and any field left empty (`:fable5`, `::high`, `openai:sol:max+fast`), or a preset from the project's `[tool.bro.llm]` table / host `~/.bro.json`. A surface canonicalizes the selection once before forwarding or recording it.

`resolve_native` puts the selection over the bro's declared `llm_spec`; a provider driven by another harness errors and points at `ride --harness`. Harness flags choose a recipe within the selected harness and never silently switch execution shape.

## Shared host machinery

- `scope.py` — `ScopeRecipe`, `BRO_RUN_RECIPE`, project-bound credential selection, `scoped_secrets`, strict launch preflight, scope override splitting, and summoned-child scope computation. Managed launches and summon lowering use this layer; in-process `bro run` / `bro chat` do not create a scope.
- `bro_run.py` — the broker-free `Launch` description for a summoned native run: `bro run|chat … --in-place`, bro git identity, `RIDE_BRO`, stdio policy, and local-trails data where the scope records locally.
- `trails.py` — local-trails mounts for launch descriptions whose computed scope records locally.
- `root.py` — neutral container and host-process root supervision behind the broker availability gate.
- `spawn.py` — broker-root composition, root lifecycle handlers, native workspace trail-pointer publication, summon lowering, and per-root `SummonControl` wiring.
- `summon_control.py` — host authorization, allow-list resolution, audit/status bookkeeping, and request lifecycle. The peer wire and self-contained CLI are `bro/summon.py`.
- `identity.py` — managed-session git identity.
- `hold.py` — the session hold as the environment carries it, and the interactive-session predicate over it.
- `broxy.py` — host-session wrapper around `broxy launch`, giving an in-place runner a session-local channel proxy with context-managed teardown.
- `e2e_test.py` — live Docker launch coverage, outside the default test roster.

A managed native container or host worktree is always launched by `ride`; a summon child is always launched by `summon`. `bro_run.describe` is summon description machinery, not a public container hop for `bro run`.
