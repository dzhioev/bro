# ride/claude/AGENTS.md

The Claude harness supplies `ride`'s first harness implementation:
Claude Code's own harness themed with the session's bro.

## Modules

- `harness.py`
  — `ClaudeHarness`, its prompt facts, private `ScopeRecipe`, auth preflight, Claude LLM resolution, terminal service-tool delivery and managed-session termination, the workspace session reads, and the launch hooks the neutral skeleton consumes:
  the runner, Claude state mounts and env for a container, the private state dir and auth for a host runner env.
  Its `serve` mounts nothing, since Claude's own tools serve every group.
- `assembly.py` — the Claude composition over core `BaseBro.assemble`:
  a session selects the Claude harness, mounting the bro's servers and data sources beside Claude Code's own tools under the session's hold.
  It contributes the `persona:` resolver through `bro.mcp.targets`, which reads that hold off `BRO_HOLD` and refuses to resolve outside a managed session.
- `runner.py` — the Claude harness run under `ride/do_ride.py`:
  pinned absolute binary selection per isolation, the brash policy of a finite command list and the refusal of competing hooks before it,
  resume-id lookup, hold and kill wiring, session MCP server, recorder, readiness gate, the Bash tool's shell prefix, MCP backstops, and Claude process lifetime.
- `competing_hooks.py` — where the pinned Claude Code loads a session's own `PreToolUse` hooks, and which of them could rewrite a gated tool's call after its gate or turn every gate off.
- `interrupt.py` — the two ways the runner runs Claude, and how each is ended so its in-flight turn reaches the transcript.
  Print mode runs over stream-json as the harness's `bro.turn_end` port, settling each turn end and ended by SIGINT;
  a TUI runs on a runner-owned pty that proxies the session's terminal, ended by the interrupt keypress.
  Either stop stands the watch waiter down first.
- `claude_argv.py`
  — the argv builder, including solo print mode, settings, status line, MCP config under `--strict-mcp-config`, and the append prompt;
  the `--tools` allowlist, the command gate on a finite command list, and the read gate without files;
  and model/effort/fast selection, prompt, and the resumed session.
- `native_tools.py` — Claude Code's own tool names per reach group on the pinned release, the loop tools every session gets, the gated `Read` of a persona without files, and the command tools a finite command list gates;
  `native_tools_llm_test.py` holds them against that release (`bro/reference/ride.md`, "The claude argv").
- `claude_auth.py` — the setup-token environment.
- `claude_release.py` — the host-wide standalone-release cache:
  a pinned version and platform's binary, its recorded manifest checksum, lifetime/download/removal locks, offline verification, seeding from another checksum-recorded copy, and cleanup.
- `shell_prefix.py` — the shell claude's Bash commands run in, pinned, and the prefix script through which each of them gets the session's PATH.
- `claude_config.py` — the `claude/` state dir under a workspace:
  settings, transcript paths, subject reads, provisioning, and the container mount and env that carry it in.
- `mcp.py` — session-local HTTP MCP server lifetime and Claude MCP config.
- `recorder.py` — Claude transcript recorder daemon lifetime;
  `trail_recorder.py` is the daemon itself and the `ride.claude.trail-recorder` console script.
- `system_prompt.py` — shared prompt and persona assembly.
  Prompt assets are loaded from the `bro` distribution, not relative to this package.
- `statusline.py` — the session-local projector process:
  it renders recording and every owned mission's state into an atomic file while its pid file is live, exits when its runner parent disappears, and holds a session-state lock that serializes resume;
  a runner-side monitor reaps it and clears only the live files that pid still owns, while Claude's refresh command only checks the pid and cats the projection.
- `command_gate.py`, `read_gate.py`, and `watch_waiter.py`
  — leaf modules invoked by Claude settings through the runner interpreter (`spawn.module_argv('ride.claude.<module>')`);
  the command gate rewrites each Bash and Monitor call of a finite command list into a brash call,
  the read gate holds the `Read` of a persona without files to the session's own Claude folders,
  and the watch waiter is every session's `Stop` and `StopFailure` `asyncRewake` hook, waking the model with the watch store's next batch (`bro/reference/ride.md`, "Claude harness").
- `waiter_state.py` — what the waiters and the runner share under the session's `claude/` state dir:
  the current waiter's registration, the count of rewakes waiters began, the stand-down mark, the stdout mark that attributes a waiter's hook events,
  and the lock a waiter registers and takes a batch under and the runner settles a turn end under.

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
- The runner starts Claude by absolute path:
  `/opt/claude-code/claude` from the pinned runtime image when boxed;
  a checksum-recorded `claude/claude` carried by a materialized runtime when present;
  otherwise `claude_release.cached_binary` at the same pin when unboxed.
  A bare `claude` from the session PATH is never the harness executable.
