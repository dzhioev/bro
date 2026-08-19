# ride/claude/AGENTS.md

The Claude harness supplies `ride`'s first harness implementation. Its two internal modes are full Claude Code and raw (`--raw`); raw is not a harness value.

## Modules

- `harness.py` — `ClaudeHarness`, private full/raw `ScopeRecipe` values, typed `ClaudeOptions`, auth preflight, Claude LLM resolution, the `ride solo|along --in-place` inner command, the workspace session reads, and the launch hooks the neutral skeleton consumes: Claude state mounts and env for a container, the private state dir and auth for a host runner env.
- `assembly.py` — the two Claude compositions over core `BaseBro.assemble`: raw sessions select the bro harness over MCP wire, full sessions select the Claude harness over MCP wire. It contributes their `bro:` / `persona:` resolvers through `bro.mcp.targets`.
- `runner.py` — the Claude session's own in-place run, under `ride/inner.py`'s neutral one: private host state, resume-id lookup, hold and kill wiring, session MCP server, launch context, recorder, readiness gate, and Claude process lifetime.
- `claude_argv.py` — one argv builder for full/raw mode, including solo print mode, settings, status line, API-key helper, MCP config, bro prompt composition, blocked and narrowed native tools, model/effort/fast selection, prompt, and forwarded Claude arguments.
- `claude_auth.py` — setup-token environment for full mode and the Anthropic API-key read used by raw mode.
- `claude_config.py` — the `claude/` state dir under a workspace: settings, transcript paths, subject reads, the one provisioning both session modes apply, and the container mount and env that carry it in.
- `mcp.py` — session-local HTTP MCP server lifetime and Claude MCP config.
- `recorder.py` — Claude transcript recorder daemon lifetime; `trail_recorder.py` is the daemon itself and the `ride.claude.trail-recorder` console script.
- `session_context.py` — typed launch-context records exported through `RIDE_SESSION_CONTEXT`.
- `system_prompt.py` — shared prompt and persona assembly. Prompt assets are loaded from the `bro` distribution, not relative to this package.
- `statusline.py`, `print_anthropic_key.py`, and `watch_guard.py` — leaf modules invoked by Claude settings through the runner interpreter (`python -m ride.claude.<module>`).

## Invariants

- `ride.claude.__init__` imports nothing. The status line runs on a repeated render clock and must retain a small import closure.
- Imports point directly at leaf modules, never through the package hub.
- Broker imports remain behind the framework's broker gate; a disabled or unavailable broker must degrade before importing its implementation.
- Full mode scopes the bro through `harness='claude'`, requires `claude_code`, and always grants the Docker socket. Raw mode scopes through `harness='bro'`, requires `anthropic`, and follows the bro's Docker declaration.
- Settings commands use the runner's interpreter and the `ride.claude` module paths.
