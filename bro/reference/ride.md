# ride

`ride` is the managed-workspace runtime. It combines a harness implementation with a bro personality, prepares either an isolated container or a host worktree, hydrates the launch scope, supervises the root session and its summons, and records enough state to resume the workspace later.

The runtime is published by the `bro-ride` distribution and depends on the `bro` framework. The framework never imports `ride`: workspace mechanics, credential scoping, broker supervision, and bro composition remain reusable framework layers.

## Commands

### `ride solo <bro> <prompt>`

Runs one prompt without a TTY and writes Claude's reply to stdout. The default harness is Claude Code and the default hold is `unattended`. Claude runs in print mode with the same prompt, MCP, recording, model, and permission composition as an interactive session; the recorded Claude session remains available to `ride resume`.

A launch without `-w / --workspace` receives a fresh name and removes that workspace after a clean exit. `--keep` retains it, and a failed run always keeps it for inspection. `-w NAME` creates or reuses that exact workspace after checking its kind and always retains it.

### `ride along <bro> [prompt]`

Starts an interactive session. The default harness is Claude Code, the default hold is `attended`, and the workspace is kept after exit. `--host` switches to a same-machine worktree and changes the omitted hold to `guided`, because a non-guided host session skips permission prompts without the container boundary.

A launch without `-w / --workspace` receives a fresh name. `-w NAME` creates or reuses that exact workspace after checking its kind; a pinned workspace cannot be combined with `--drop`. For an automatically named workspace, `--drop` removes it only after a clean exit and keeps a failed session for inspection.

The workspace base defaults to the checkout's current `HEAD`. `--into REF` resolves a branch, tag, or commit, fetching an origin-only ref when needed, and affects only workspace creation. Uncommitted host changes never transfer.

The prompt occupies a positional slot on both mode verbs. Harness-native arguments therefore follow an explicit separator:

```console
ride solo dev 'inspect the launch path' -- --debug mcp
ride along dev 'continue the inspection' -- --debug mcp
```

Shared launch flags are `--host`, `--hold`, `--grant`, `--revoke`, `--into`, and the LLM selection set (`--provider`, `--model`, `--effort`, `--fast`, `--llm`). `--grant` and `--revoke` use the framework's unified grammar: credential names shape the scoped store and `@bro` names shape the summon allow-list.

### Lifecycle verbs

- `ride resume <workspace>` relaunches the recorded session recipe, with optional `--grant` / `--revoke` adjustments. Resuming a solo run opens an interactive conversation in the same workspace and re-resolves the hold to `along`'s default (`attended`, or `guided` with `--host`).
- `ride list` lists project workspaces and their activity state.
- `ride clean` removes inactive clean workspaces; `--force` permits dirty ones and `--dry-run` reports only.
- `ride exec <workspace> [command ...]` enters a running container workspace.
- `ride check-clean <workspace>` reports whether removal is safe.
- `ride scope [--bro BRO] [--harness HARNESS] [--raw]` prints the prospective credential tiers and selected credential instances.
- `ride banner [--llm]` renders the session facts.

## Harness selection

`--harness {claude,bro}` selects the driving loop. When omitted, `ride` reads `[tool.bro] harness`; a project that omits the key gets `claude`. The bro harness value is reserved in this stage and fails before workspace creation until its implementation lands.

LLM flags resolve within the selected harness. They never switch the harness implicitly. A recipe whose provider the harness cannot run errors with `--harness` as the remedy.

### Harness seam

`ride.harness.Harness` is the runtime boundary. A harness implementation supplies:

- its per-mode `ScopeRecipe`, auth preflight, and LLM resolution;
- the command run inside the prepared workspace and its in-place runner;
- harness-owned command options;
- session existence, subject, trail-pointer, and workspace teardown operations;
- the host/container launch implementation for machinery that runs next to the agent loop.

The generic scope computation and the bro-run recipe stay in `bro.launch.scope`, so native launch and summon lowering share the same policy without making `bro` depend on `bro-ride`. Claude's full/raw recipes remain private to `ride.claude`; raw is a Claude mode, not a harness value.

## Claude harness

Claude full mode retains Claude Code's built-ins, skills, and base prompt while adding the selected bro's persona, spells, filtered MCP namespaces, and blocked-tool declarations. `--raw` runs `claude --bare` under the bro's own composed prompt and MCP surface. Raw remains container-only and requires the `anthropic` secret; full mode requires the `claude_code` setup token.

The in-place command deliberately remains `cw ss --in-place` during the compatibility period. This lets a checkout on the feature branch drive a workspace based on the older branch, and the older launcher drive a workspace based on the feature branch.

## In-container launches

Until process-host mode is available, `ride` refuses to start from inside a managed container. Use `summon` for an isolated sibling child, or `bro run|chat --in-place` for a process using the current container and credential scope. The compatibility `cw` wrapper retains its previous silent host-worktree fallback during this period.

## Compatibility wrapper

`cw` is a delegating CLI shipped by `bro-ride`. Its parser, defaults, lifecycle verbs, nested-launch fallback, and `cw ss --in-place` contract remain unchanged while the unified runtime is built. The detailed compatibility behavior and the existing `CW_*` / `var/cw` state names are documented in the `cw` reference; those names are intentionally unchanged at this stage.
