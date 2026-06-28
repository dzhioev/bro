# dive-in

`dive-in` is a thin wrapper around `cw ss` that turns "I want to work on this task" into a ready-to-go Claude Code session: it picks the workspace name from the task, seeds `/fix <task-ref>` as the first user message (so the `ppp-dev` bro's `fix` skill orients Claude toward the task), and forwards into `cw ss --mcp` with the right flags.

This document explains the modes, slug derivation, the `/fix`-seeding rules, and the rules around `--host` and `--resume`. The source of truth is `dive_in.py` (which also has unit tests in `dive_in_test.py`).

## Modes

`dive-in` is always in exactly one mode. The mode is selected by which flags are present.

### Bare mode (default — no task flag, no `--focus`)

`dive-in` with no task-selecting flags falls through to "open a clean session, unattached to any task". The positional `command` (if any) becomes the entire initial prompt; otherwise the session starts with no prompt at all. This is the same as `cw ss --mcp -c <slug>` and is handy when you have a request to make but no task to attach to. The environment-awareness rule in `prompts/environment.md` tells Claude to ask what to work on in this case.

### Focused mode (`--focus`)

`dive-in --focus` (no `-t`, no `--new`) reads the currently focused task from the focus client (`flow/focus/client/client.py`). If nothing is focused, it logs an error and exits 1 — there is no implicit fallback to "any task". The first user message becomes `/fix --focus`, which tells the `fix` skill to call `get_focused_task` → `get_task_info` → `get_page_content`.

### Task mode (`-t / --task <ref>`)

`-t` accepts either a Notion URL or a UUID (`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`). URL forms accepted: `https://[www.|app.]notion.{so,com,site}/[<path>/][<slug>-]<32hex>(?:\?…)?` — the slug prefix and the workspace/path prefix are both optional, so root-level (`notion.so/<hex>`), workspace-prefixed without slug (`notion.so/<ws>/<hex>`), and share URLs (`app.notion.com/p/<ws>/<slug>-<hex>`) all parse. URLs get normalised to UUIDs by `_resolve_task_id` — the 32-hex tail is split into the canonical 8-4-4-4-12 layout. A bare 32-hex string (no URL prefix) is **not** accepted (would be ambiguous with a future ref shape). Backslash-escaped query strings (e.g. shell-pasted `\?source\=copy_link`) are tolerated.

The first user message becomes `/fix <original-task-ref>` — the URL or UUID exactly as the user typed it. The skill body resolves it and calls `get_task_info` / `get_page_content` itself.

If `--focus` is combined with `-t`, the focus client is also told to focus the resolved task before the session starts — this is the canonical way to "switch to this task and dive in" in one command. In that case the first message becomes `/fix --focus` instead (focus was just set, so the focused form is equivalent and slightly cleaner).

### New mode (`--new`)

`--new` starts a session that will *create* the task and then dive into it. The first user message becomes `/fix --new` (optionally `/fix --new <seed>` if a positional `command` is present, and `/fix --new <seed> --focus` if `--focus` is also passed). The `fix` skill body tells Claude to:

1. Collect any missing properties (name, importance, project, tags, deadline, content) from the user.
2. Call `add_task` and treat the returned id as the target.
3. Continue with the normal `get_task_info` / `get_page_content` flow on that new id.
4. If `--focus` was passed, call `set_focus` on the newly created task's id.

`-t` and `--new` are mutually exclusive (argparse-enforced); `--focus` is orthogonal and combines with either or stands alone.

## Slug derivation and collision avoidance

The workspace name (= `cw ss <name>`) is derived from whatever the session is *about*:

- **Task / focused mode** — slug is `_slugify(task_name)`. `_slugify` lowercases, replaces any run of non-alphanumerics with `-`, trims leading/trailing `-`, and truncates to 40 chars (re-trimming a trailing `-` if the truncation produced one). If the slug ends up empty (e.g. all-CJK task name), it falls back to `dive-in`.
- **`--new` mode** — base is `_slugify(command)` if a seed command is present, otherwise `dive-in-new`.
- **Bare mode** — base is always `dive-in`.

For `--new` and bare mode, **`_pick_fresh_name` appends a random suffix** — `base-<8 hex>` from `secrets.token_hex(4)` (e.g. `dive-in-new-a3f9c2b1`) — retrying until neither `var/cw/worktrees/<slug>` (host) nor `var/cw/containers/<slug>` (container) exists.

The suffix makes each session's `worktree-<slug>` branch **unique by construction**, which is what prevents the remote-branch collision: local cleanup (`cw clean` / `--drop`) deletes only the *local* `worktree-<slug>` branch, so an un-merged `--new` session leaves `origin/worktree-<slug>` behind — but the next session picks a different suffix, so it never reuses a slug whose pushed branch still holds unmerged work. Because uniqueness is structural, the remote is never consulted (no `git ls-remote`, no network); the two local `.exists()` checks only guard against the vanishingly rare clash with a live workspace, regenerating the suffix if one hits. `dive_in_test.py:TestPickFreshName` covers the cases.

In task / focused mode the slug is **not** suffixed — those modes intentionally reuse the worktree across sessions, because the slug deterministically maps to the task, and re-entering should land you back in the same workspace. Only `--new` and bare mode go through `_pick_fresh_name`; this is why the call is gated on the `new` / "no task" branches in `dive_in.py:dive_in`.

`dive-in` logs `workspace: <slug>` after picking (both `--new` and bare mode), so the generated name is visible — you need it to reattach via `cw exec <name>` or `cw ss --resume <name>` (`dive-in --resume` itself only works in task / focused mode).

## Host vs container

By default `dive-in` runs in container mode (it appends `-c` when calling `cw ss`). `--host` flips this off and runs as a same-machine git worktree instead.

The reason this isn't just "pass `-c` through" is that container is the safer / more isolated default for an unattended task-focused workflow (especially with `--auto`), so the wrapper inverts the polarity. If you need the host worktree (e.g. you want to use the host's docker daemon directly without the bind-mounted socket, or you want filesystem access to your dotfiles), `--host` opts out.

## Base ref (`--into`)

By default a session is based on the host repo's current `HEAD` — both in container mode (the clone checks out the host's checkout) and host mode (the worktree branches from `HEAD`). `dive-in --into <ref>` (a forwarded `cw ss` flag — see `reference/cw.md`) overrides that, basing the new session on any branch/tag/sha instead: the container checks that ref out in its clone, and a `--host` session bases its worktree branch on it. Useful for basing a session on a ref you don't have checked out. A ref that's resolvable only on origin (e.g. a feature branch pushed from another container — the `/feature` per-stage flow bases each stage on its feature branch this way) is fetched from origin automatically. Only applies when the workspace is first created.

## Initial-prompt composition

`dive-in` seeds the first user message as a `/fix …` slash command and lets the `fix` skill body (`bro/bros/ppp_dev/skills/fix.md`) carry the workflow — resolve → context → plan → log → implement → verify → hand off to `/pr`. The mapping from CLI form to message is:

- `dive-in -t <ref>` → `/fix <ref>`
- `dive-in -t <ref> --focus` → `set_focus(<ref>)` (focus client), then `/fix --focus`
- `dive-in --focus` → `/fix --focus`
- `dive-in --new` → `/fix --new`
- `dive-in --new <seed>` → `/fix --new <seed>`
- `dive-in --new <seed> --focus` → `/fix --new <seed> --focus`

If a positional `command` is present alongside a task scope (`-t` or bare `--focus`), it gets appended as `Once you understand the task, <command>`. This lets you say `dive-in -t URL "draft a PR description"` and have it threaded through the task-orientation flow.

For bare mode, the prompt is just the `command` string verbatim — no `/fix` wrapping.

For the skill to be discoverable by Claude Code's slash-command resolution, `dive-in` sets `CW_BRO=ppp-dev` in the environment. Container mode: the entrypoint reads `CW_BRO` and runs `cw populate-bro-skills "$CW_BRO"`, which symlinks the bro's skills (`/fix`, `/pr`, `/land` — all owned by ppp-dev in `bro/bros/ppp_dev/skills/`) into the workspace's `.claude/skills/<name>/SKILL.md`. Host mode: `cw.py:start_session` populates a per-session `tempfile.mkdtemp` directory and passes it to claude via `--add-dir <tmp>` so concurrent dive-in sessions on the same repo don't share `.claude/skills/`. Beyond skills, the bro's `persona` (its own MRO-concatenated class `system_prompt`(s), without the shared / data-source / skills blocks) is appended to the session's `--append-system-prompt` by `cw.py:_session_append_prompt`, so the dive-in session carries ppp-dev's policies even though it runs the native Claude Code harness rather than `--bro`. This is the bridge toward making `dive-in` a `cw ss --bro ppp-dev` session outright.

## `--resume`

`--resume` resumes the latest Claude session in the workspace, skipping the initial prompt entirely (you're picking up mid-conversation, not orienting Claude to the task afresh).

Argparse-enforced rules in `dive_in.py:main`:

- **Requires a task scope** — `-t <ref>` or `--focus`. Bare `dive-in --resume` is rejected. (You need to know which workspace to resume; the slug comes from the task.)
- **Cannot be combined with `--new`** — `--new` creates a new task, which would create a new workspace; resuming is the opposite of new.
- **Cannot be combined with a positional `command`** — the initial prompt is ignored on resume, so passing one would silently do nothing.

When resuming, `dive-in` still resolves the task id (and, with `--focus`, still tells the focus client to focus it), sets `CW_TASK_ID`, and logs the task name — but skips building the initial prompt. It then forwards `--resume` through `cw.add_forwarded_flags` / `cw.extract_forwarded_argv` into `cw ss`, which is the layer that actually looks up the latest session jsonl and adds `--resume <session-id>` to the claude argv.

## Forwarded flags

`dive-in` accepts all the flags `cw.add_forwarded_flags` registers (`--auto`, `--fast`, `--grant`, `--revoke`, `--effort`, `--resume`, `--into`) and forwards them straight through into `cw ss`. Adding a new pass-through flag in `cw.py` makes it available to `dive-in` for free — no per-flag plumbing in this file. `--grant`/`--revoke` (repeatable) require container mode, so they are unusable with `dive-in --host`.

`-n / --dry-run` prints the final `cw ss …` invocation (shell-quoted) without running it.

## Env-var handoff

- `CW_TASK_ID` — set to the resolved task id in any mode that has one (focused, `-t`, `-t --focus`). Picked up by `setup/claude_commit_footer.py` to insert `Task: <notion-url>` into commit messages, and by the `/pr` skill to build the commit footer.
- `CW_BRO` — set unconditionally to `ppp-dev` so the bro's skills (`/fix`, `/pr`, `/land` — all owned by ppp-dev) are surfaced to Claude Code's slash-command resolution. Container mode: the entrypoint (`setup/container/entrypoint.sh`) symlinks them into the workspace's `.claude/skills/`. Host mode: `cw.py:start_session` populates a per-session tmp dir and passes it to claude via `--add-dir`.
- `PPP_SHELL_COMMAND` — set (if not already set) to the user-facing reconstruction of the dive-in invocation. Consumed by `cw banner` and surfaced as `launch_command` in `cw banner --llm`, which is what Claude reads at session start (see `prompts/environment.md`) to self-detect that it was launched via `dive-in` (and whether `-t` / `--focus` / `--new` was used).

The user-facing `dive-in` reconstruction is built explicitly in `dive_in.py` (not via `cw.add_forwarded_flags`'s reconstruct) so that env-detection sees `dive-in`, not the underlying `cw ss`.
