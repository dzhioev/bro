# ride/claude/AGENTS.md

The Claude harness supplies `ride`'s first harness implementation. Its two internal modes are full Claude Code and raw (`--raw`); raw is not a harness value.

## Modules

- `harness.py` — `ClaudeHarness`, private full/raw `ScopeRecipe` values, typed `ClaudeOptions`, auth preflight, Claude LLM resolution, the `ride solo|along --in-place` inner command, and workspace state operations.
- `session.py` — Claude host/container launch data around the neutral outer lifecycle. Both paths spawn the workspace checkout's `ride solo|along --in-place`; host mode provisions the worktree and private Claude state, while container mode contributes Claude mounts and the trail pointer to the neutral Docker launch.
- `runner.py` — the in-place runner next to Claude: private host state, resume-id lookup, bro identity and commit provisioning, host broxy, session MCP server, launch context, recorder, readiness gate, and Claude process lifetime.
- `claude_argv.py` — one argv builder for full/raw mode, including solo print mode, settings, status line, API-key helper, MCP config, bro prompt composition, blocked tools, model/effort/fast selection, prompt, and forwarded Claude arguments.
- `claude_auth.py` — setup-token environment for full mode and the Anthropic API-key read used by raw mode.
- `claude_config.py` — per-workspace Claude state, settings, transcript paths, subject reads, trail pointer, host provisioning, container mounts, and teardown.
- `mcp.py` — session-local HTTP MCP server lifetime and Claude MCP config.
- `recorder.py` — Claude transcript recorder daemon lifetime.
- `session_context.py` — typed launch-context records exported through `CW_SESSION_CONTEXT`.
- `system_prompt.py` — shared prompt and persona assembly. Prompt assets are loaded from the `bro` distribution, not relative to this package.
- `statusline.py` and `print_anthropic_key.py` — leaf modules invoked by Claude settings through the runner interpreter (`python -m ride.claude.<module>`).

## Invariants

- `ride.claude.__init__` imports nothing. The status line runs on a repeated render clock and must retain a small import closure.
- Imports point directly at leaf modules, never through the package hub.
- Broker imports remain behind the framework's broker gate; a disabled or unavailable broker must degrade before importing its implementation.
- Full mode scopes the bro through `harness='claude'`, requires `claude_code`, and always grants the Docker socket. Raw mode scopes through `harness='bro'`, requires `anthropic`, and follows the bro's Docker declaration.
- Settings commands use the runner's interpreter and the `ride.claude` module paths.
