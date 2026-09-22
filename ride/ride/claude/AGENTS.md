# ride/claude/AGENTS.md

The Claude harness supplies `ride`'s first harness implementation:
Claude Code's own harness themed with the session's bro.

## Modules

- `harness.py`
  — `ClaudeHarness`, its private `ScopeRecipe`, auth preflight, Claude LLM resolution, the workspace session reads, and the launch hooks the neutral skeleton consumes:
  the runner, Claude state mounts and env for a container, the private state dir and auth for a host runner env.
- `assembly.py` — the Claude composition over core `BaseBro.assemble`:
  a session selects the Claude harness, mounting the bro's additions to Claude Code's native tools.
  It contributes the `persona:` resolver through `bro.mcp.targets`.
- `runner.py` — the Claude harness run under `ride/do_ride.py`:
  resume-id lookup, hold and kill wiring, session MCP server, launch context, recorder, readiness gate, and Claude process lifetime.
- `interrupt.py` — how a Claude process is ended so its in-flight turn reaches the transcript:
  SIGINT for print mode, and for a TUI the interrupt keypress on a runner-owned pty that proxies the session's terminal.
- `claude_argv.py`
  — the argv builder, including solo print mode, settings, status line, MCP config, the append prompt, blocked and narrowed native tools, model/effort/fast selection, prompt, and forwarded Claude arguments.
- `claude_auth.py` — the setup-token environment.
- `claude_config.py` — the `claude/` state dir under a workspace:
  settings, transcript paths, subject reads, provisioning, and the container mount and env that carry it in.
- `mcp.py` — session-local HTTP MCP server lifetime and Claude MCP config.
- `recorder.py` — Claude transcript recorder daemon lifetime;
  `trail_recorder.py` is the daemon itself and the `ride.claude.trail-recorder` console script.
- `session_context.py` — claude's own typed launch-context records (the system prompt, MCP servers, root instructions) exported through `RIDE_SESSION_CONTEXT`;
  the session's git state is the neutral `bro.trails.record.session` reader's, attached by the recorder.
- `system_prompt.py` — shared prompt and persona assembly.
  Prompt assets are loaded from the `bro` distribution, not relative to this package.
- `statusline.py` — the session-local projector process:
  it renders recording and every owned mission's state into an atomic file while its pid file is live, exits when its runner parent disappears, and holds a session-state lock that serializes resume;
  a runner-side monitor reaps it and clears only the live files that pid still owns, while Claude's refresh command only checks the pid and cats the projection.
- `watch_guard.py` and `stop_guard.py`
  — leaf modules invoked by Claude settings through the runner interpreter (`python -m ride.claude.<module>`);
  the watch guard applies a folded finite shell roster to both Bash and Monitor calls,
  and the stop guard is a solo session's `Stop` hook:
  print mode holds the process while a background task is pending, so it blocks a turn end once per turn only when missions are in flight with no running task or a task is running with nothing in flight,
  reading the running tasks off the hook input's undocumented `background_tasks` and naming quest or mission routes for the worker types present.

## Invariants

- `ride.claude.__init__` imports nothing.
  The repeated statusLine command stays shell-only;
  Python rendering runs once per session in the projector process.
- Imports point directly at leaf modules, never through the package hub.
- Broker machinery imports remain behind the framework's broker gate;
  a disabled broker must degrade before importing its implementation, while the constants-only environment module may load before the gate.
- A session scopes the bro through `harness='claude'` and requires `claude_code`.
- Settings commands that run Python use the runner's interpreter and `ride.claude` module paths;
  the statusLine command only reads its session-local projection.
- Machinery the runner spawns
  — the session MCP server, recorder daemon, and statusLine projector
  — is named by its path in the runtime the runner runs from (`bro.base.spawn.console_script`), never by bare name:
  the session PATH carries the pinned session commands, and machinery is not among them.
