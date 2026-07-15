# dive-in

`dive-in` is a thin wrapper around `cw ss` that turns "I want to work on this task" into a ready-to-go Claude Code session: it picks the workspace name from the task, seeds `/fix <task-ref>` as the first user message (so the `ppp-dev` bro's `fix` skill orients Claude toward the task), and forwards into `cw ss` with the right flags.

This document explains the modes, workspace naming, the `/fix`-seeding rules, and the rules around `--host`. The source of truth is `dive_in.py` (which also has unit tests in `dive_in_test.py`).

## Modes

`dive-in` is always in exactly one mode. The mode is selected by which flags are present.

### Bare mode (default — no task flag, no `--focus`)

`dive-in` with no task-selecting flags falls through to "open a clean session, unattached to any task". The positional `command` (if any) becomes the entire initial prompt; otherwise the session starts with no prompt at all. This is the same as `cw ss <slug>` and is handy when you have a request to make but no task to attach to.

### Focused mode (`--focus`)

`dive-in --focus` (no `-t`, no `--new`) reads the currently focused task from the focus client (`flow/focus/client/client.py`). If nothing is focused, it logs an error and exits 1 — there is no implicit fallback to "any task". The first user message becomes `/fix <task-id>` with the focused task's id — the `fix` skill has no focus form of its own.

Focus is a flow-surface concept (the focus service stores flow task ids), so both `--focus` forms require the flow brog backend; with any other backend the launch fails with a clear error before a session is created.

### Task mode (`-t / --task <ref>`)

`-t` accepts any task ref the repo's brog backend takes natively (see `brog/CLAUDE.md`): with the flow backend a Notion URL or a dashed UUID, with the GitHub backend an issue number, `#N`, or an issue URL. The ref is resolved host-side through `brog.system.default_system()`, which normalizes it to the backend's canonical id — the value `CW_TASK_ID` carries.

The first user message becomes `/fix <original-task-ref>` — the ref exactly as the user typed it — followed by the pre-fetched task block (see "Initial-prompt composition").

If `--focus` is combined with `-t`, the focus client is also told to focus the resolved task (its canonical id) before the session starts — this is the canonical way to "switch to this task and dive in" in one command. The first message stays `/fix <original-task-ref>`; only the focus state changes.

### New mode (`--new`)

`--new` starts a session that will *create* the task and then dive into it. The first user message becomes `/fix --new` (optionally `/fix --new <seed>` if a positional `command` is present). The `fix` skill body tells Claude to:

1. Collect any missing properties (name, tags, body) from the user.
2. Call `brog::create_task` — the task is born open (workable) — and treat the returned id as the target.
3. Continue the normal flow on that new id.

`-t` and `--new` are mutually exclusive (argparse-enforced), and so are `--new` and `--focus` — the task to focus doesn't exist at launch, and the skill has no focus form to delegate the set to; `--focus` combines with `-t` or stands alone.

## Workspace naming

Every launch gets a **fresh workspace**: the name is always `base-<8 hex>` — `bro_run.fresh_workspace_name` appends a `secrets.token_hex(4)` suffix (e.g. `my-task-a3f9c2b1`), retrying until neither `var/cw/worktrees/<name>` (host) nor `var/cw/containers/<name>` (container) exists. The base is derived from whatever the session is *about*:

- **Task / focused mode** — `_slugify(task_name)`. `_slugify` lowercases, replaces any run of non-alphanumerics with `-`, trims leading/trailing `-`, and truncates to 40 chars (re-trimming a trailing `-` if the truncation produced one). If the slug ends up empty (e.g. all-CJK task name), it falls back to `dive-in`.
- **`--new` mode** — `_slugify(command)` if a seed command is present, otherwise `dive-in-new`.
- **Bare mode** — always `dive-in`.

A fresh name means a fresh clone/worktree on the intended base (the host checkout's `HEAD`, or `--into`) by construction. In particular, task mode never reuses a workspace an earlier session on the same task created — silent reuse would ignore `--into` and could land the session on a tree predating the work it is meant to build on. Two accepted side effects: concurrent sessions on one task are possible (there is no implicit one-live-session-per-task lock), and workspaces accumulate per launch — containers are cheap (shared objects + baked venv), and `cw clean` reclaims landed workspaces in both modes.

The suffix also makes each session's `worktree-<slug>` branch **unique by construction**, which is what prevents the remote-branch collision: local cleanup (`cw clean` / `--drop`) deletes only the *local* `worktree-<slug>` branch, so an un-merged session leaves `origin/worktree-<slug>` behind — but the next session picks a different suffix, so it never reuses a slug whose pushed branch still holds unmerged work. Because uniqueness is structural, the remote is never consulted (no `git ls-remote`, no network); the two local `.exists()` checks only guard against the vanishingly rare clash with a live workspace, regenerating the suffix if one hits. `bro_run_test.py` covers the collision cases.

`dive-in` logs `workspace: <name>` after picking, so the generated name is visible — you need it to reattach via `cw exec <name>` or to resume via `cw ss --resume <name>` (see "Resuming").

## Host vs container

Like `cw ss` itself, `dive-in` runs in container mode by default; `--host` (a forwarded `cw ss` flag — see "Forwarded flags") opts out into a same-machine git worktree. Reach for it when the session needs the host itself — e.g. the host's docker daemon directly instead of the bind-mounted socket, or filesystem access to your dotfiles.

## Base ref (`--into`)

By default a session is based on the host checkout's current `HEAD` — both in container mode (the clone checks it out) and host mode (the worktree branches from it) — so it builds on what the launcher has checked out, offline included; `HEAD` is the commit, so uncommitted changes never transfer. `dive-in --into <ref>` (a forwarded `cw ss` flag — see `reference/cw.md`) overrides that, basing the new session on any branch/tag/sha instead: the container checks that ref out in its clone, and a `--host` session bases its worktree branch on it. A ref that's resolvable only on origin (e.g. a feature branch pushed from another container — the `/feature` per-stage flow bases each stage on its feature branch this way) is fetched from origin automatically. Every launch creates a fresh workspace (see "Workspace naming"), so `--into` always takes effect.

## Initial-prompt composition

`dive-in` seeds the first user message as a `/fix …` slash command and lets the `fix` skill body (`bro/bros/ppp_dev/skills/fix.md`) carry the workflow — resolve → context → plan → log → implement → verify → hand off to `/pr`. The mapping from CLI form to message is:

- `dive-in -t <ref>` → `/fix <ref>`
- `dive-in -t <ref> --focus` → `set_focus(<canonical-id>)` (focus client), then `/fix <ref>`
- `dive-in --focus` → `/fix <focused-task-id>`
- `dive-in --new` → `/fix --new`
- `dive-in --new <seed>` → `/fix --new <seed>`

The task-scoped forms (all but `--new`) also append a pre-fetched task block to the message — the task metadata, description, and comments, fetched host-side via `brog.system.default_system()` — so the session's first turn reads the task without calling the not-yet-connected brog MCP server.

If a positional `command` is present alongside a task scope (`-t` or bare `--focus`), it gets appended as `Once you understand the task, <command>`. This lets you say `dive-in -t URL "draft a PR description"` and have it threaded through the task-orientation flow.

For bare mode, the prompt is just the `command` string verbatim — no `/fix` wrapping.

The skill is discoverable by Claude Code's slash-command resolution because every cw-session runs as a persona bro — `--persona`, ppp-dev by default (`cw.constants.DEFAULT_SESSION_BRO`). The in-place session runner (`cw/runner.py`) copies that bro's skills, rendered for the claude harness — the MRO-walked set: ppp-dev's own `/fix`, `/pr`, `/land` (`bro/bros/ppp_dev/skills/`) plus inherited ones such as `/audit` (dev) and the shared `/ask` (summon) — into a per-session `tempfile.mkdtemp` directory and passes it to claude via `--add-dir <tmp>`, so concurrent dive-in sessions on the same repo don't share `.claude/skills/`. Beyond skills, the persona delivers the bro's `persona` prompt (its own MRO-concatenated class `system_prompt`(s), without the shared / data-source / skills blocks) into the session's `--append-system-prompt` (`cw/system_prompt.py:_session_append_prompt`), and its claude-harness-filtered MCP namespaces through the session-local server (see `reference/cw.md`, "Session-local MCP serving") — so the dive-in session carries ppp-dev's policies and brog task tools even though it runs the Claude Code harness rather than `--bro`. To run the bro flavor outright, forward `--bro` (see "Forwarded flags").

## Resuming

`dive-in` always creates a fresh workspace, so it has no `--resume` of its own. To pick up a finished session, use the hint `cw` prints on session exit — `cw ss --resume <name>` with the session's own flags (see `reference/cw.md`, "`--resume`").

Known gap: `CW_TASK_ID` lives only in the launching `dive-in` process's environment, so a `cw ss --resume` session doesn't carry it, and commits made there lack the `Task: <url>` footer line. Accepted — resumes are the exception, and the merge usually happens in the original session.

## Forwarded flags

`dive-in` accepts all the flags `cw.add_forwarded_flags` registers (`--host`, `--mode`, `--fast`, `--grant-cred`, `--revoke-cred`, `--grant-summon`, `--revoke-summon`, `--effort`, `--into`, `--persona`, `--bro`) and forwards them straight through into `cw ss`. Adding a new pass-through flag in `cw/flags.py` makes it available to `dive-in` for free — no per-flag plumbing in this file. `--grant-cred`/`--revoke-cred` (repeatable) require container mode, so they are unusable with `dive-in --host`; the summon pair works in both modes (a host session's broker root enforces the same allow-list).

`--bro <name>` makes the session the bro flavor of `cw ss` — `claude --bare` under the named bro's persona, serving the bro's own MCP tools (see `reference/cw.md`, "`--bro`"). The seeded `/fix …` message still works there — a bro session resolves `/<name>` messages through its `bro::skill` tool — provided the named bro carries the fix/pr/land skills and the brog toolset (ppp-dev does). Like any `--bro` session it is fenced to the container (rejected with `--host`) and requires the `anthropic` secret.

`-n / --dry-run` prints the final `cw ss …` invocation (shell-quoted) without running it.

## Env-var handoff

- `CW_TASK_ID` — set to the resolved task's canonical brog id in any mode that has one (focused, `-t`, `-t --focus`). Read by the `/pr` skill to build the commit footer's `Task: <url>` line (via `brog::get_task(id).url`).
- `PPP_SHELL_COMMAND` — set (if not already set) to the user-facing reconstruction of the dive-in invocation. The visual `cw banner` shows it as the outer launch command and extracts the user prompt from it; the agent-facing `cw banner --llm` omits it.

The user-facing `dive-in` reconstruction is rebuilt from dive-in's own parser (`Parser.reconstruct` with prog `dive-in`) so the visual banner shows `dive-in`, not the underlying `cw ss`.
