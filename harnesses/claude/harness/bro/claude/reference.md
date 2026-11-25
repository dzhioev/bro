# Claude harness

`bro-claude` registers the `claude` harness.
A Claude session runs Claude Code with the natives the selected bro's tool groups map to, Claude's own skills and base prompt, and the bro's persona, spells, and MCP namespaces.
The harness serves the `anthropic` provider through Claude Code.
This page is the harness's side of a managed session;
the runtime around it
— workspaces, isolation, credential delivery, summons, watches, recording
— is `bro/reference/ride.md`.

## The pinned release

Every managed Claude session runs the version pinned in `harnesses/claude/harness/bro/claude/image/claude-code-version` by absolute path:
the runtime image's install in boxed isolation, and a verified standalone release in unboxed isolation.
The harness contributes that binary to the runtime image, so the image's hash covers the pin.
An ordinary unboxed runtime caches it under `<runtime-root>/claude-code/<version>/<platform>/`;
a materialized `--runtime-bundle` may instead carry `claude/claude` and its checksum record as part of its self-contained installation.
Each copy is checked against the release checksum recorded beside it and needs no network;
a changed copy fails the launch rather than being replaced.
A session holds its version against `ride clean` for its whole run, and concurrent first starts serialize the one download.
The runner sets `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT` and `MCP_TOOL_TIMEOUT` to a 24-hour backstop so each tool's own bound or the session supervisor decides first.

## The session runner

The outer runs the separate `do-ride solo|along` session executable.
Unboxed isolation takes it from the frozen host snapshot;
boxed isolation takes it from the same bundle materialized at `/var/ride/runtime`.

The harness's auth preflight is the `claude_code` setup token, so `ride resume` is gated like the launch that created the session.
A resume's existence check is a Claude transcript under the workspace's state dir;
the runner resolves the actual session id later, from its cwd's projects dir.

The runner (`harnesses/claude/harness/bro/claude/runner.py`) then, in order:
refuses a session whose own Claude configuration could take a call around the gate of a tool it is served behind one, and under a finite command list writes the session's brash policy (both in "The claude argv" below);
resolves a resume's Claude session id;
reads the session's `launch-facts.json`, failing on a malformed file;
starts the session-local MCP server (see "Session-local MCP serving");
builds the Claude argv (see "The claude argv");
starts the session recorder daemon (see "Session recording");
gates on the server's `/health`;
then runs the isolation's pinned Claude binary by absolute path and waits.
A SIGTERM aimed at `do-ride` (`docker stop`, kill) reaches it as the interrupt a user issues rather than as a signal, so the turn in flight reaches the transcript before Claude goes;
one a terminating service tool sent waits instead for the turn the session-end hook stopped (see "Turn ends and watch delivery").
`harnesses/claude/harness/bro/claude/interrupt.py` owns that per-flavor split.
After Claude exits the runner stops the server and recorder (the stop is the recorder's final append and trail end).

Every session's `SessionStart` hook, `bro.claude.session_start`, registers the lifetime's launch facts (`bro/reference/ride.md`, "The session executable")
for each transcript Claude Code starts writing, at startup, on a resume, `/branch` and `/clear`, at its line count then.
On a resume it returns the notice as its additional context, which Claude Code writes as a `hook_additional_context` attachment ahead of the next user line and sends with it.
The baseline is the transcript's newest entry below its last line carrying a `uuid`, since Claude Code appends lines without one even to a session closed before any input;
an entry this lifetime registered gives no notice.

The runner exports `claude/usage.json` in the session state dir as the session's usage file (`BRO_USAGE_FILE`, `bro.llm.usage`),
which a runner thread keeps current from the session's newest transcript segment and the transcripts of the subagents it spawned,
reading on every transcript write and billing each response once.

Claude Code runs a Bash command in a login shell whenever its startup shell snapshot is missing, and a login profile that resets PATH there, Debian's `/etc/profile` or the user's own, drops the session commands.
The runner therefore pins the shell through `CLAUDE_CODE_SHELL` and routes every command through a `CLAUDE_CODE_SHELL_PREFIX` script (`bro.claude.shell_prefix`)
that restores the session's PATH first and sources the current credential-hook environment file;
`harnesses/claude/harness/bro/claude/shell_prefix_llm_test.py` holds that fallback live against the pinned release.

## Turn ends and watch delivery

Every session's settings wire `bro.claude.watch_waiter` as its `Stop` and `StopFailure` hook, with `asyncRewake`:
each turn end, one that ends in an API error included, starts a waiter in the background, which registers as the session's current one and polls the watch store.
On a batch it exits 2 with the lines on stderr,
which Claude hands the model under the hook's `rewakeSummary` and `rewakeMessage`, after the next tool result of a running turn or as a turn of its own.
It also records the batch in the session's Claude state dir, where ride's own Claude Code plugin reads it.
An interactive session loads that plugin from a copy laid in its state dir at launch,
and the plugin draws a dim transcript row for each event of the batch as Claude delivers it:
the first line's arrival time on the session's Claude clock, its watch's command shortened and marked `*` when the line woke the session, then the line,
with the lines its watch marked as continuing it joined on by ` · ` and their empty lines dropped.
A waiter exits 0 once a later waiter supersedes it, the runner stands it down, or `do-ride` is gone;
shortly before its hook timeout it wakes the session with a line saying no watch line arrived and that ending the turn keeps waiting.

A solo session runs Claude in print mode over stream-json, its prompt delivered as the first user message and its stdin held open:
Claude then stays alive as long as stdin is open, and a finished background task or a waiter's rewake starts a turn of its own.
Each turn's reply lands on the session's stdout and Claude's diagnostics on stderr.
At each turn's `result` event the runner applies the shared end-of-turn rule (`bro.turn_end`),
with Claude's background work read off the stream's last `background_tasks_changed` event, less the entries flagged `ambient`, which Claude marks as housekeeping rather than activity.
A claude task's stream names no output, so its silence is its age.
A timer thread of the stream session sends the silence notice, and the notice of a beat that stalls, as user messages while the session awaits input, re-reading the watches' beats every few seconds;
a failure of that thread ends the stream and fails the run.
A TUI session gets no timed notices.
A notice goes in as a user message on stdin;
an end stands the waiter down and closes stdin, which ends the session without the up to 30 seconds Claude holds a closing print session for a pending async hook.
A stop stands the waiter down before it interrupts Claude, which kills a pending waiter, so that kill never reads as the waiter failing;
the first `result` after a stop closes stdin rather than being settled.
Claude drops a rewake that arrives after stdin closes, so the runner reads each waiter's exit off the stream's hook events
and leaves a turn end unsettled while a batch a waiter took has not yet reached Claude's queue.
The waiter's hook command prints a mark before the interpreter starts,
which tells the waiter's hook events, a failed start's among them, from those of the hooks Claude merges in from the session's other settings;
a waiter that fails fails the solo run.
`harnesses/claude/harness/bro/claude/watch_waiter_llm_test.py` probes the hooks, the events, and the exit live against the Claude Code release every managed session runs.

Every session's settings also wire `bro.claude.session_end` as the `PostToolUse` hook of the `answer` and `raise` service tools.
Such a call signals the session's stop while it is in flight, leaving the session's exit status first;
the hook then answers it with `continue: false`, so Claude keeps the call's own result, makes no further model call, and writes the stopped turn with a `hook_stopped_continuation` record.
The stop waits for that turn rather than interrupting Claude, which would record the call as one the user rejected:
a print session ends at the stopped turn's `result`, and a TUI quits once the record of the stopped call is in its transcript.
A turn that does not stop within a bound is interrupted after all.
`harnesses/claude/harness/bro/claude/session_end_llm_test.py` holds that live against the pinned release.

## The claude argv

The builder (`harnesses/claude/harness/bro/claude/claude_argv.py:build_claude_launch`) assembles the merged `--settings`
(fastMode, the statusLine, the attribution opt-out, the hooks, kept on over a repository's `disableAllHooks`, and the permission settings below),
a resume's `--resume <session id>`, prompt seeding, the `--model` / `--effort` / fastMode it reads off the session's anthropic recipe in `RIDE_RESOLVED_RECIPE`, `--model` always passed and `--effort` passed only when set,
the ride-injected `--append-system-prompt` (see "The append prompt"), the `--permission-mode` below,
the `--mcp-config` mounting the persona's namespaces from the session-local server below,
`--strict-mcp-config`, so that config is the session's only MCP source and a repository's `.mcp.json` loads nothing,
and `--disallowed-tools mcp__claude_ai_*` to keep account-level claude.ai MCP integrations out of the managed session.

The session's hold and isolation select its permission mode.
`guided` starts in manual mode, which asks before each call, under either isolation.
Every other hold starts a boxed session in bypass mode, whose acceptance dialog the boxed settings pre-answer,
and an unboxed one in auto mode, whose classifier reviews each call in place of a prompt.
An unboxed session's settings turn bypass mode off (`permissions.disableBypassPermissionsMode`),
so no flag, Shift+Tab, repository `defaultMode`, or agent `permissionMode` reaches it.
`harnesses/claude/harness/bro/claude/permission_mode_llm_test.py` holds the modes and the unboxed settings against the pinned release.

`--tools` passes exactly the Claude natives the persona's tool groups map to, an allowlist, plus the loop tools every session gets;
`harnesses/claude/harness/bro/claude/native_tools.py` names them per group on the pinned release.
Every other native is off, a tool a Claude Code release adds included, until it is mapped.
Without `files` a persona gets `Read` alone, behind a `PreToolUse` hook (`bro.claude.read_gate`) holding it to the two folders where Claude Code keeps what it hands the session back as files:
`claude-<uid>/<project>/<session id>/` under the runner's `CLAUDE_CODE_TMPDIR`, where a backgrounded command writes its output,
and the `<session id>/` folder beside the transcript, whose `tool-results/` holds the whole of a result too large to inline.
The gate names both from its hook input, with the transcript's folder as the project, and admits only an absolute path that resolves, symlinks followed, inside one of them;
it denies every other path, and blocks the call when it cannot read its input.
Such a persona thus reads its own output whole and no workspace file through a tool, while the repository's `CLAUDE.md`, or its `AGENTS.md` where it has none, and project skills still load as its instructions.
`harnesses/claude/harness/bro/claude/read_gate_llm_test.py` holds both folders and the denials against the pinned release.
No group serves `Monitor`: `bro::watch` follows a command's output on every harness.
Under a finite command list a `PreToolUse` hook (`bro.claude.command_gate`) on each served native that runs a command line rewrites each call's command through `updatedInput` into one argv:
the runtime's `brash` by path with the session's brash policy, `-c`, and the model's line untouched, quoted so that Claude's shell reads only single-quoted words.
It keeps the call's other fields, decides no permission, so a `guided` session still prompts, and denies a call that carries no command, `Monitor`'s WebSocket form.
The runner writes the policy from the bro's reach into the session's `claude/` state dir and names it to the session-local MCP server, whose `bro::watch` starts lines under it.
Before it starts anything, the runner refuses to start a session whose own Claude configuration could take a call around either gate, naming each place (`bro.claude.competing_hooks`):
a `PreToolUse` hook matching a tool the session is served behind a gate, as Claude's matcher reads it,
in the working directory's project or local settings,
in the frontmatter of a skill, command, or agent in the session's Claude folder or in a `.claude` folder at, above, or below the working directory,
or in an installed plugin the settings enable;
or a candidate it cannot read or parse.
Managed settings stay outside the check (`bro/reference/extending.md`, "Brash command-list policy").
`harnesses/claude/harness/bro/claude/command_gate_llm_test.py` holds the rewrite, those places, and the managed override against the pinned release.
`delegation()` brings Claude's own agents, and a delegated agent gets at most the session's natives, since the allowlist withholds the rest from the whole session.
`--tools` drops a name the pinned release does not serve without a word,
so `harnesses/claude/harness/bro/claude/native_tools_llm_test.py` holds each registered persona's mapped natives against the pinned release's `init` event and a delegated agent's tools against the allowlist;
a Claude Code bump reruns it.

The statusLine is ride's, not the operated project's.
The runner starts one session-local projector process from its frozen runtime;
it queries the journal, reads recording health, renders the clock-relative line at the statusLine refresh cadence, and atomically writes it under `session/claude/`.
Claude's settings command only checks that projector's pid and cats the projection file, so each event-driven or interval render starts no Python process and performs no broker call.
The projector holds a session-state lock and exits immediately when its parent-owned liveness pipe reaches EOF.
A resumed runner waits for that lock before clearing the prior files and starting its writer, so abrupt host-parent death cannot leave two projectors racing one workspace.
During a live parent, a runner-side monitor waits and reaps the projector, removing only files still owned by its pid;
the pid guard covers that exit-to-cleanup window, so an unexpected child death renders blank rather than stale.
The remaining settings commands use the `do-ride` interpreter, so neither leans on PATH resolution nor on a file mode the wheel format does not guarantee;
flagSettings outranking project settings means a consuming project neither declares a statusLine nor can break the session's with a stale one.
The corollary is that a bare `claude` launched outside ride gets claude's default bar.

Claude's own attribution is off in every managed session
— its commit trailer, its pull-request line, and the appended session link:
the framework carries commit attribution of its own, and the same flagSettings rank keeps a consuming project's settings from turning claude's back on.

## Session-local MCP serving

Every session gets its MCP tools from a session-local HTTP server the runner owns
— one mechanism for both execution modes, dying with the session.
The runner starts `mcp-server persona:<name> --http`, by its absolute path in the runtime the runner runs from, on an OS-assigned port (a fixed port would collide between concurrent sessions sharing a netns) with a per-session bearer token.
`mcp-server --http --port 0 --port-file <path>` binds the socket *before* its heavy imports and publishes the real port through the port file, which the runner polls (milliseconds) before building the `--mcp-config` and launching claude;
a claude connect that lands mid-import sits in the TCP backlog until uvicorn accepts on the pre-bound socket.
The server is terminated when claude exits;
a SIGKILLed runner orphans it.
Its output lands in a `ride-mcp-*` temp dir alongside the port file.

`bro-claude` contributes the `persona:` target prefix through `bro.mcp.targets`;
the core `mcp-server` discovers the matching resolver without knowing the target.
`bro.claude.assembly` resolves `persona:<name>` through `persona_servers()`
— `BaseBro.assemble(harness=CLAUDE, ...)`, with the registered object injected by the Claude engine, yielding the bro's declared servers and data sources plus the `spell` and bro service servers;
Claude serves the tool groups with its own natives, so the harness mounts no server of its own for them.
`bro::cast` joins the service server when the bro has spells and `mu` resolves.
Persona sessions rely on Claude's native third-party skill mechanism instead of mounting `bro::skill` or generated spell adapters.
The server reads the session's hold off `BRO_HOLD` and assembles the tools under it:
the spell bodies render under that hold,
and the assembly mounts the `raise` service tool when the session is unattended (`BRO_HOLD=unattended` + `RIDE_RUNNER_PID` in the server's inherited environment — `bro/reference/ride.md`, "Forwarded env vars"),
in its terminate-the-session flavor (semantics in `bro/AGENTS.md`, "Service tools");
every other level gets no `raise`
— a human exists to report to.
There is one streamable-HTTP endpoint per tool namespace;
the argv builder mounts each endpoint under its namespace as the claude server key, so tools surface as `mcp__<namespace>__<tool>`
— `/tasks` → `mcp__tasks__list_tasks`, `/bro` → `mcp__bro__banner`, `/<name>-source` → `mcp__<name>-source__<tool>` (e.g. `mcp__current-time-source__get_time`)
— matching the `bro/prompts/tool_names.md` convention.
Every server entry in a ride-generated `--mcp-config` carries `alwaysLoad: true`:
a headless (`-p`) run then holds its first request until the server is connected, but an interactive session's argv-seeded first prompt does not wait
— claude's MCP connects stay async, so that first turn can reach the model with no tools attached, bridged by Claude's own still-connecting reminder and ToolSearch.
The runner polls the server's `/health` until ready *before* launching claude, so the configured bro and tool graph's potentially heavy imports is paid off claude's critical path instead of inside that startup block and its connect timeout;
the runner's own argv build overlaps the server's import, so much of the wait is already paid when the gate is reached.

In boxed isolation the server runs inside the container, so the scoped credential store carries the served tools' own secrets (the persona's claude-harness manifest)
— no deployed-server token is involved.

## The append prompt

For every `ride solo|along` session, `harnesses/claude/harness/bro/claude/system_prompt.py:session_append_prompt` builds the `--append-system-prompt` text:
the base prompts from `bro/prompts/shared/*` and the top-level reference docs the loader registers (see `bro/prompts/AGENTS.md` for the inventory), plus the session bro's own persona prompts (`BaseBro.persona` — the selected bro),
so the session carries the bro's policies under Claude's own harness.
`shared/` is also injected into every bro;
the top-level reference docs are Claude-Code-specific and reach no other harness.
The text also carries the hold fragment, rendered at launch by `bro.prompts.hold_fragment` from the session's `--hold` level (the level files live in `bro/prompts/holds/` — see `bro/prompts/AGENTS.md`, "Hold text"),
and the Spells contract when the bro has spells.
Claude's native third-party skill discovery stays on;
bro spells are not copied into `.claude/skills/` and have no slash-command aliases.

## Claude state

A Claude session keeps its Claude Code state in the workspace's `claude/` state dir, the config root `CLAUDE_CONFIG_DIR` names (`harnesses/claude/harness/bro/claude/claude_config.py`),
and its runtime artifacts under the session records' `claude/`
— recorder and projector logs, the live statusLine projection, the usage file and its publisher lock, and the persistent Claude temp root.
The session records' `launch-facts.json` names each transcript a lifetime began to write.
A member that joined a party keeps its private Claude state under `party/<member>/claude/`, which a boxed join reaches through the party mount as `CLAUDE_CONFIG_DIR=/var/ride/party/<member>/claude`.
Removing the workspace reclaims all of it.
No Claude Code hooks are wired for clone creation or workspace provisioning, which `ride` owns.

### Unboxed Claude-state isolation

An unboxed Claude session points `CLAUDE_CONFIG_DIR` at the workspace's `claude/` directory.
The directory carries only the constructed session settings, the setup-token auth, the workspace tree's trust entry, and the launcher's account identity when its host state provides one.
The launcher's settings, hooks, permissions, custom agents, and OAuth credentials file do not enter it.

### Container credential isolation

The container does **not** bind-mount `~/.claude.json` from the host, nor does the host's OAuth credentials file ever enter the container.
Instead, the launch provisions a container-private `.claude.json` in the workspace's `claude/` dir,
which reaches claude inside the mounted dir that `CLAUDE_CONFIG_DIR` names (`harnesses/claude/harness/bro/claude/claude_config.py:container_claude_state`
— claude state is launch data the harness's boxed extras put on the neutral `Launch`, so a bro-harness container, which runs no Claude, mounts no claude state at all), while session auth comes from the `claude_code` token (below):

- `claude/.claude.json` — constructed once per workspace from an explicit config (`installMethod: global` to match the image's `npm i -g` install, `hasCompletedOnboarding`,
  `hasTrustDialogAccepted: true` on the project entry for the session's start directory so guided sessions skip the folder-trust prompt,
  `officialMarketplaceAutoInstallAttempted: true` and `officialMarketplaceAutoInstalled: true` so claude doesn't fetch the official plugin marketplace at startup,
  and `lspRecommendationDisabled: true` so it doesn't offer a language-server plugin for the files a session opens
  — sessions install no marketplace plugins, only Claude Code's built-in ones load) plus the host's account-identity fields (`oauthAccount`, `userID`) so the session starts logged in.
  Host machine state (project paths, trust history, usage counters, feature caches) is **not** copied.
  Missing identity is fatal
  — `ride` aborts asking you to log in on the launcher first.
  Subsequent sessions keep whatever the container last wrote.
  Stops per-project mutations (mcpServers, allowedTools, hasTrustDialogAccepted) from being usable to escalate into the next unboxed session.
- **Session auth (`CLAUDE_CODE_OAUTH_TOKEN`)** — Claude sessions authenticate with this env var, which the **required** `claude_code` secret (a long-lived token that `claude-setup-token` mints) exports via its registry install hook.
  Claude Code reads it above any credentials file, and one stable bearer is shared by every session
  — so no OAuth credentials file is mounted or synced, and none of the cross-session refresh-token rotation that forced the periodic `/login`.
  Being required, a missing token fails loudly on the launcher at scoped-store hydration, before the container starts (not as a turn-1 401 inside it).
  Bro-run containers request no token;
  only the Claude path does.
  Unboxed sessions get the same var injected into the claude subprocess env directly (`bro.claude.claude_auth.apply_claude_auth`, applied idempotently by both the outer unboxed launch and the `do-ride` session executable next to claude).
  The outer launch reads it from the launch's scoped store rather than the launcher's ambient credential selection.
  The token is equally required there:
  the launch aborts up front when the selected name doesn't load, since the session's private config dir carries no OAuth file to fall back on (see "Unboxed Claude-state isolation").
  The same transform scrubs inherited `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` from the session env:
  both outrank `CLAUDE_CODE_OAUTH_TOKEN` in claude's credential precedence, so a value leaking in from the launching shell would silently hijack the session's auth.
  Full sessions also carry `CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK`, since claude resolves fast-mode availability from the credentials file they don't have
  — left to guess, it reports fast mode as disabled by an organization.
- `claude/settings.json` — constructed fresh each launch (not mounted from the host), holding only UX prefs (spinner verbs, reduced motion, feedback-survey opt-out),
  a `cleanupPeriodDays` pin keeping transcripts forever (they back the session recording), an `autoMemoryEnabled: false` opt-out of claude's default-on auto-memory,
  an `idleCompaction: false` opt-out of claude's default-on compaction of a long conversation left idle, which would summarize a session's context away while it waits on a watch or a summon,
  an `env` block turning claude's auto-updater off (`DISABLE_AUTOUPDATER`, so no session replaces the pinned binary it runs)
  and its telemetry (`DISABLE_TELEMETRY`, which also leaves the feature flags Claude Code's server serves, fetched or cached, without effect, so a session runs the pinned release's own defaults;
  `harnesses/claude/harness/bro/claude/remote_flags_llm_test.py` holds that against the pinned release),
  and setting the text of the reminder Claude Code adds after a run of silent turns (`CLAUDE_CODE_SILENT_TURN_REMINDER_TEXT`, pointing what the answer needs at the turn's final message rather than a thinking note;
  `harnesses/claude/harness/bro/claude/silent_turn_reminder_llm_test.py` holds it against the pinned release).
  Host permissions, hooks, plugins, and model/effort pins do not leak in, and the session's own LLM flags plus the merged `--settings` (see "The claude argv") own session config.
- `<runtime-root>/workspaces/<name>/claude/` (host) → `/home/ride/.claude` (container).
  Per-workspace overlay of everything else.
  It is mounted from the host rather than living in the container because the container is `--rm`'d at exit while the workspace outlives it:
  the transcripts are what `ride resume` continues and what `ride list` reads a subject from, and summon control reads the trail pointer beside them on the launcher while the session runs.
- `claude/projects/<encoded start directory>/` — where Claude Code stores the session JSONL for the start directory.

This means each boxed session has its own private `~/.claude.json` (so MCP server allow-lists are per-workspace) and its own session log directory, while authenticating with the shared,
non-rotating `claude_code` token (so no session's refresh can blow away another's).

## Summoned Claude sessions

A claude-harness child is scoped through the claude recipe
— `claude_code` required, no LLM key
— and the seam's auth preflight runs in the lowering, so an unresolvable setup token fails the spawn with the preflight's remedy as the correlated launch failure.
Its lifecycle comes from the `do-ride` session executable rather than a native runner:
the runner echoes the last turn's reply, announces the trail mark once the session recorder publishes the workspace's current-trail pointer, and on a clean exit no `answer` ended sends the quest's `result{failed, reason: unanswered}`
— a non-zero exit emits no result and surfaces as the synthesized `result{failed, reason: exit}` (the echoed reply lands in its output tail), while the `answer` and `raise` service tools send their own ok and `result{failed, reason: raised}`.
A manual child's runner announces the trail mark from its trail watch the same way.
The recorder stamps the child trail's `summoned_by` from the summoner attribution, and the child's recorded resume spec is a claude spec, so a kept workspace resumes into the claude conversation.

A Claude session mounts the summon and quest service tools beside the blocking `summon` and `quest` CLIs;
a long blocking summon runs via the harness's background Bash, and `rewind show <trail-id>` peeks at it mid-run.

## Session recording

The runner under `do-ride` starts a `bro.claude.trail-recorder` daemon (via `harnesses/claude/harness/bro/claude/recorder.py`) before launching Claude and stops it after Claude exits
— the stop is the recorder's final append and trail end.
`--no-trails` starts no recorder daemon.
The recorder records each stretch of the transcript the session writes to, the lines one lifetime adds to it before switching to another, into a claude-harness *trail* of its own,
appending newly completed lines on every transcript write and at its polling interval without one.
`harnesses/claude/harness/bro/claude/trail_recorder.py` owns transcript acquisition and decides each trail's edge from the session's recording map (`bro.trails`) and the transcripts alone.
Nothing reopens a trail:
the pinned release's `--resume` and `/resume` append to the transcript in place rather than copying its history, and the lifetime's trail gets a `resume` edge;
`/branch` and `--fork-session` copy a transcript's records into a new one, whose trail takes its own lines with a `branch` edge;
`/clear` opens a root.
A whole conversation is the fork chain `rewind show` walks.
The recorder republishes the session's trail pointer as it turns from one transcript to another.
The daemon's stderr goes to `claude/session-recorder.log` in the session state dir, the log its recording health beats name (`bro/reference/ride.md`, "Session recording");
the statusLine and `ride banner` surface that signal.
A daemon that cannot be started at all never reaches that signal:
the runner ends the launch, since a session that believes it is recording and is not is worse than one that refuses to start.
A session whose transcript ends in a `raise` service-tool call gets its trail ended as `raised` with the reason as `end.detail` (a later real user message — a resume moving past the abort — clears it),
keeping an unattended session's abort queryable without parsing the jsonl.
