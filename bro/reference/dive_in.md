# dive-in

`dive-in` is a thin wrapper around `cw ss` that turns "I want to work on this task" into a ready-to-go Claude Code session: it picks the workspace name from the task, seeds `@:fix <task-ref>:@` as the first user message (so the session dispatches to the configured persona's parameterized `fix` script), and forwards into `cw ss` with the right flags.

This document explains the modes, workspace naming, the dispatcher-command seeding rules, and the rules around `--host`. The source of truth is `bro/workflow/dive_in.py` (which also has unit tests in `bro/workflow/dive_in_test.py`).

## Modes

`dive-in` is always in exactly one mode. The mode is selected by which flags are present.

### Bare mode (default — no task flag)

`dive-in` with no task-selecting flags falls through to "open a clean session, unattached to any task". The positional `command` (if any) becomes the entire initial prompt; otherwise the session starts with no prompt at all. This is the same as `cw ss <slug>` and is handy when you have a request to make but no task to attach to.

### Task mode (`-t / --task <ref>`)

`-t` accepts any task ref the repository's configured brog backend takes natively (see `bro/brog/CLAUDE.md`). The built-in GitHub backend accepts an issue number, `#N`, or an issue URL; contributed backends define their own refs. The ref is resolved host-side by a brog backend bound to the launch's own credential scope (`_task_system`: the session bro's scope under the launch's `--grant`/`--revoke` and the project's instance mapping, read through `bro.launch.scope.launch_view_store`) — so the prefetch reads the same `brog` config the session's store hydrates. The backend normalizes the ref to its canonical id — the value `CW_TASK_ID` carries. A scope whose `brog` was revoked without a replacement, or a bad override, fails here with a clean error before anything is created.

The first user message becomes `@:fix <original-task-ref>:@` — the ref exactly as the user typed it — followed by the pre-fetched task block (see "Initial-prompt composition").

### New mode (`--new`)

`--new` starts a session that will *create* the task and then dive into it. The first user message becomes `@:fix --new "":@` (or `@:fix --new <seed>:@` if a positional `command` is present); the explicit empty string preserves new-task intent without inventing seed text. The dispatcher maps that command to the `fix` script's optional `new` argument, whose body tells Claude to:

1. Collect any missing properties (name, tags, body) from the user.
2. Call `brog::create_task` — the task is born open (workable) — and treat the returned id as the target.
3. Continue the normal flow on that new id.

`-t` and `--new` are mutually exclusive (argparse-enforced).

## Workspace naming

Every launch gets a **fresh workspace**: the name is always `base-<8 hex>` — `bro.workspace.paths.fresh_workspace_name` appends a `secrets.token_hex(4)` suffix (e.g. `my-task-a3f9c2b1`), retrying until neither `var/cw/worktrees/<name>` (host) nor `var/cw/containers/<name>` (container) exists. The base is derived from whatever the session is *about*:

- **Task mode** — `_slugify(task_name)`. `_slugify` lowercases, replaces any run of non-alphanumerics with `-`, trims leading/trailing `-`, and truncates to 40 chars (re-trimming a trailing `-` if the truncation produced one). If the slug ends up empty (e.g. all-CJK task name), it falls back to `dive-in`.
- **`--new` mode** — `_slugify(command)` if a seed command is present, otherwise `dive-in-new`.
- **Bare mode** — always `dive-in`.

A fresh name means a fresh clone/worktree on the intended base (fresh origin `HEAD` by default, or `--into` — see "Base ref") by construction. In particular, task mode never reuses a workspace an earlier session on the same task created — silent reuse would ignore `--into` and could land the session on a tree predating the work it is meant to build on. Two accepted side effects: concurrent sessions on one task are possible (there is no implicit one-live-session-per-task lock), and workspaces accumulate per launch — containers are cheap (shared objects + baked venv), and `cw clean` reclaims cleanly-finished workspaces in both modes.

The suffix also makes each session's `worktree-<slug>` branch **unique by construction**, which is what prevents the remote-branch collision: local cleanup (`cw clean` / `--drop`) deletes only the *local* `worktree-<slug>` branch, so an un-merged session leaves `origin/worktree-<slug>` behind — but the next session picks a different suffix, so it never reuses a slug whose pushed branch still holds unmerged work. Because uniqueness is structural, the remote is never consulted (no `git ls-remote`, no network); the two local `.exists()` checks only guard against the vanishingly rare clash with a live workspace, regenerating the suffix if one hits. `bro/workspace/paths_test.py` covers the collision cases.

`dive-in` logs `workspace: <name>` after picking, so the generated name is visible — you need it to reattach via `cw exec <name>` or to resume via `cw resume c:<name>` (see "Resuming").

## Host vs container

Like `cw ss` itself, `dive-in` runs in container mode by default; `--host` (a forwarded `cw ss` flag — see "Forwarded flags") opts out into a same-machine git worktree. Reach for it when the session needs the host itself — e.g. the host's docker daemon directly instead of the bind-mounted socket, or filesystem access to your dotfiles.

## Base ref (`--into`)

With `--into` omitted, `dive-in` resolves the base itself: it fetches origin's `HEAD` (the remote default branch — master) and forwards the resulting sha as `--into`, so a new session builds on fresh master no matter how stale the host checkout is. When origin is unreachable it logs a warning and forwards no `--into`, falling back to `cw ss`'s own default — the host checkout's current `HEAD` — so offline launches still work; `--into HEAD` picks that base explicitly. The base is always a commit, so uncommitted changes never transfer.

`dive-in --into <ref>` (a forwarded `cw ss` flag — see `bro/reference/cw.md`) bases the new session on any branch/tag/sha instead: the container checks that ref out in its clone, and a `--host` session bases its worktree branch on it. A ref that's resolvable only on origin (e.g. a feature branch pushed from another container — the `@::run-feature` per-stage flow bases each stage on its feature branch this way) is fetched from origin automatically. Every launch creates a fresh workspace (see "Workspace naming"), so the base always takes effect.

## Initial-prompt composition

`dive-in` seeds the first user message as an `@:fix …:@` natural-language script command. The session calls `@::@`, which resolves it to `@::fix` and returns the script instructions with the typed arguments; the script body (`bro/bros/dev/scripts/fix.md`) carries the workflow — resolve → context → plan → log → implement → verify → hand off to `@::run-pr`. Both cw persona and `--raw` sessions mount the `at` server and receive this contract. The mapping from CLI form to message is:

- `dive-in -t <ref>` → `@:fix <ref>:@`
- `dive-in --new` → `@:fix --new "":@`
- `dive-in --new <seed>` → `@:fix --new <seed>:@`

Task mode also appends a pre-fetched task block to the message — the task metadata, description, and comments, fetched host-side by the launch-scoped backend (`_task_system`, see "Task mode") — so the session's first turn reads the task without calling the not-yet-connected brog MCP server.

If a positional `command` is present alongside `-t`, it gets appended as `Once you understand the task, <command>`. This lets you say `dive-in -t URL "draft a PR description"` and have it threaded through the task-orientation flow.

For bare mode, the prompt is just the `command` string verbatim — no dispatcher wrapping.

Every cw-session runs as a bro — `--bro`, defaulting to the required project default. Its session-local server mounts the bro's MRO-collected scripts; a persona derived from `Dev` inherits `@::fix`, `@::run-pr`, `@::land`, `@::audit` (`bro/bros/dev/scripts/`) and `@::ask` (summon). `_session_append_prompt` injects the canonical Scripts contract next to the persona prompt. Bro scripts have no generated slash-command copies; Claude's own third-party skill mechanism remains independent. The bro's other claude-harness-filtered MCP namespaces ride the same session-local server (see `bro/reference/cw.md`, "Session-local MCP serving"). To run the raw flavor outright, forward `--raw` (see "Forwarded flags").

## Resuming

`dive-in` always creates a fresh workspace, so it has no resume of its own. To pick up a finished session, run `cw resume <ref>` — the workspace as `cw list` shows it (`c:<name>` in the default container mode), relaunched under the session's own flags (see `bro/reference/cw.md`, "Resuming a session").

Known gap: `CW_TASK_ID` lives only in the launching `dive-in` process's environment, so a resumed session doesn't carry it, and commits made there lack the `Task: <url>` footer line. Accepted — resumes are the exception, and the merge usually happens in the original session.

## Forwarded flags

`dive-in` accepts all the flags `bro.cw.add_forwarded_flags` registers (`--host`, `--hold`, `--fast`, `--grant`, `--revoke`, `--effort`, `--into`, `--bro`, `--raw`) and forwards them straight through into `cw ss`. Two flags get dive-in-specific defaults, each resolved before forwarding so the explicit value overrides `cw ss`'s own default: an omitted `--hold` resolves to `attended` — or `guided` with `--host`, where skipped permission prompts would run unsandboxed — and an omitted `--into` resolves to origin's freshly fetched `HEAD` (see "Base ref"). Adding a new pass-through flag in `bro/cw/flags.py` makes it available to `dive-in` for free — no per-flag plumbing in this file. `--grant`/`--revoke` are repeatable and work in both modes — a plain name adjusts the credential scope (in a host session a materialized convenience scope rather than a security boundary), a `@bro` value the summon allow-list (a host session's broker root enforces the same list).

`--raw` makes the session the raw flavor of `cw ss` — `claude --bare` under the session bro's persona, serving the bro's own MCP tools (see `bro/reference/cw.md`, "`--raw`"). It consumes the same seeded `@:fix …:@` command as a persona session, so the session's bro must carry the fix/pr/land scripts and the brog toolset. Like any `--raw` session it is fenced to the container (rejected with `--host`) and requires the `anthropic` secret.

`-n / --dry-run` prints the final `cw ss …` invocation (shell-quoted) without running it.

## Env-var handoff

- `CW_TASK_ID` — set to the resolved task's canonical brog id in task mode (`-t`). Read by the `@::run-pr` script to build the commit footer's `Task: <url>` line (via `brog::get_task(id).url`).
- `BRO_SHELL_COMMAND` — set (if not already set) to the user-facing reconstruction of the dive-in invocation. The visual `cw banner` shows it as the outer launch command and extracts the user prompt from it; the agent-facing `cw banner --llm` omits it.

The user-facing `dive-in` reconstruction is rebuilt from dive-in's own parser (`Parser.reconstruct` with prog `dive-in`) so the visual banner shows `dive-in`, not the underlying `cw ss`.
