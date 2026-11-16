# Bump Claude Code

The Claude Code release every managed Claude session runs, pinned in `harnesses/claude/harness/bro/claude/image/claude-code-version` (the `claude-harness` man page, "The pinned release").

## Step 1 — the pin

`harnesses/claude/harness/bro/claude/image/bump-claude-code.sh` pins the version npm's `latest` tag names for `@anthropic-ai/claude-code`;
with `to`, write the version into `claude-code-version` instead.
Versions order as semver.
Nothing syncs:
each launch reads the pin, and the live probes run the pinned release.

## Step 2 — the range

The range is the sections of Claude Code's changelog from `## <new>` down to, not including, `## <old>`:

```bash
curl -fsSL https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md -o <scratch>/CHANGELOG.md
```

The changelog runs past `<new>` once later releases ship;
those sections are not the range.

## Step 3 — the surfaces

The harness reaches Claude Code through, each with the modules that hold it (`harnesses/claude/harness/AGENTS.md`, "Modules"):

- the argv, print mode's stream-json, and the TUI it drives — `claude_argv.py`, `interrupt.py`
- the session's settings, `.claude.json` seed, and environment — `claude_config.py`, `runner.py`, `claude_auth.py`
- hook events and their input and output — `command_gate.py`, `read_gate.py`, `watch_waiter.py`, `session_end.py`, and where hooks load from, `competing_hooks.py`
- native tool and agent names — `native_tools.py`
- the transcript, its project-dir layout, and its records — `claude_config.py`, `transcripts.py`, `usage_publisher.py`, `recorder.py`, and the trail format in `harnesses/claude/trails/`
- MCP config and calls — `mcp.py`; the statusLine — `statusline.py`; the plugin API — `claude_plugin.py` and `plugin/`; `setup-token` — `setup_token.py`
- the models it serves and their names — the `bro-anthropic` recipes (`llms/anthropic/`)

The behavior sessions run under is the changelog's new features on by default, changed defaults, and new limits in print mode and the TUI.
An opt-out goes into the session settings or their `env` block (`claude_config.py`), with its line in the `claude-harness` man page's "Claude state".

## Step 4 — the docs

The changelog is the range's docs;
the abilities it adds are read from it.

## Step 5 — the gate

The gate adds `run-tests --only llm` and `run-tests --only conformance`:
the probes hold undocumented Claude Code behavior at the pin
— the model-input channels Claude Code records, the gates, the hooks, the mapped natives —
and the conformance cases hold the framework's contract on the Claude harness.
A stage that cannot run for want of a credential is named in the bump's body.

## Step 6 — the commit

The bump is `claude: bump claude-code to <new>`.
Its body names `<old> → <new>` and the stages that ran, then each adaptation and opt-out,
then a `Notable over <old>:` list:
one line per changelog entry the harness reaches, its version in parentheses, and what the harness does about it.
