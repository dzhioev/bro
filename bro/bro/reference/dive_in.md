# dive-in

`dive-in` is a thin wrapper around `cw ss` that turns "I want to work on this task" into a ready-to-go Claude Code session: it picks the workspace name from the task, composes an initial prompt that orients Claude toward the task, and forwards into `cw ss --mcp` with the right flags.

This document explains the modes, slug derivation, prompt composition, and the rules around `--host` and `--resume`. The source of truth is `dive_in.py` (which also has unit tests in `dive_in_test.py`).

## Modes

`dive-in` is always in exactly one mode. The mode is selected by which flags are present.

### Bare mode (default — no task flag, no `--focus`)

`dive-in` with no task-selecting flags falls through to "open a clean session, unattached to any task". The positional `command` (if any) becomes the entire initial prompt; otherwise the session starts with no prompt at all. This is the same as `cw ss --mcp -c <slug>` and is handy when you have a request to make but no task to attach to. The environment-awareness rule in `prompts/base/environment.md` tells Claude to ask what to work on in this case.

### Focused mode (`--focus`)

`dive-in --focus` (no `-t`, no `--new`) reads the currently focused task from the focus client (`flow/focus/client/client.py`). If nothing is focused, it logs an error and exits 1 — there is no implicit fallback to "any task". The dive-in prompt is built from `prompts/dive_in.prompt.template` with the `dive_in_focused.prompt` startup body, which tells Claude to call `get_focused_task` → `get_task_info` → `get_page_content`.

### Task mode (`-t / --task <ref>`)

`-t` accepts either a Notion URL (`https://www.notion.so/.../-<hex>(?:\?…)?`) or a UUID (`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`). URLs get normalised to UUIDs by `_resolve_task_id` — the 32-hex tail of the URL is split into the canonical 8-4-4-4-12 layout. A bare 32-hex string is **not** accepted (would be ambiguous with a future ref shape). Backslash-escaped query strings (e.g. shell-pasted `\?source\=copy_link`) are tolerated.

The startup is `dive_in_task.prompt.template`, which inlines the resolved task id so Claude can call `get_task_info("<id>")` / `get_page_content("<id>")` directly without rediscovering the id.

If `--focus` is combined with `-t`, the focus client is also told to focus the resolved task before the session starts — this is the canonical way to "switch to this task and dive in" in one command. In that case the startup becomes `dive_in_focused.prompt` and the target text becomes "the currently focused task", so the prompt stays consistent with what's actually focused.

### New mode (`--new`)

`--new` starts a session that will *create* the task and then dive into it. The session prompt is `dive_in_new.prompt.template`, which tells Claude to:
1. Collect any missing properties (name, importance, project, tags, deadline, content) from the user.
2. Call `add_task` and treat the returned id as the target.
3. Continue with the normal `get_task_info` / `get_page_content` flow on that new id.

If the positional `command` is present, it's threaded into the template as "Initial idea from the user: `<command>`" so Claude has the seed without prompting the user for it from scratch.

If `--focus` is combined with `--new`, an instruction is appended to the prompt telling Claude to call `set_focus` on the newly created task's id after creation.

`-t` and `--new` are mutually exclusive (argparse-enforced); `--focus` is orthogonal and combines with either or stands alone.

## Slug derivation and collision avoidance

The workspace name (= `cw ss <name>`) is derived from whatever the session is *about*:

- **Task / focused mode** — slug is `_slugify(task_name)`. `_slugify` lowercases, replaces any run of non-alphanumerics with `-`, trims leading/trailing `-`, and truncates to 40 chars (re-trimming a trailing `-` if the truncation produced one). If the slug ends up empty (e.g. all-CJK task name), it falls back to `dive-in`.
- **`--new` mode** — slug is `_slugify(command)` if a seed command is present, otherwise `dive-in-new`.
- **Bare mode** — slug is always `dive-in` (then `dive-in-2`, etc. on collision).

After slug derivation, **`_pick_fresh_name` walks the slug to the first non-colliding name**: `name`, then `name-2`, `name-3`, …, checking both `.claude/worktrees/<slug>` (host) and `var/cw/containers/<slug>` (container) — i.e. the two namespaces are checked together. This means a `--new` (or bare) dive-in never lands on a directory already in use by either mode, even if you're about to start a host session and the collision is in the container namespace (or vice versa). `dive_in_test.py:TestPickFreshName` covers the cases.

In task / focused mode the slug is **not** bumped — those modes intentionally reuse the worktree across sessions, because the slug deterministically maps to the task, and re-entering should land you back in the same workspace. Only `--new` (and bare mode) bumps. This is why the `_pick_fresh_name` call is gated on `new` / "no task" branches in `dive_in.py:dive_in`.

In `--new` mode `dive-in` logs `workspace <slug> is in use, picking <bumped>` when a bump happens so the user notices. In bare mode the bump is silent — `_pick_fresh_name` has no logging of its own, and the bare-mode branch in `dive_in.py:dive_in` does not log around the call.

## Host vs container

By default `dive-in` runs in container mode (it appends `-c` when calling `cw ss`). `--host` flips this off and runs as a same-machine git worktree instead.

The reason this isn't just "pass `-c` through" is that container is the safer / more isolated default for an unattended task-focused workflow (especially with `--auto`), so the wrapper inverts the polarity. If you need the host worktree (e.g. you want to use the host's docker daemon directly without the bind-mounted socket, or you want filesystem access to your dotfiles), `--host` opts out.

## Initial-prompt composition

The initial prompt that gets passed via `cw ss -p` is built from `prompts/dive_in.prompt.template`:

```
Dive into {target} and figure out how to accomplish it.

Step 1 — understand the task:
{startup}

{context}
```

Slots:
- `{target}` — either `the currently focused task` (focused / `-t --focus`) or `task <task_id>` (`-t` alone).
- `{startup}` — `dive_in_focused.prompt` (call `get_focused_task` first) or `dive_in_task.prompt.template` rendered with the resolved id.
- `{context}` — `dive_in_context.prompt`, which contains the Step 2 (gather context — project, sibling tasks, tags) and Step 3 (plan) instructions, plus the **task-closure policy** (propose `update_task status='Done'` when goal is met, don't close automatically) and the **development-log convention** (`append_page_content` with a timestamped plan summary, then key decisions, under a `## Development log` heading).

If a positional `command` is present alongside a task scope, it gets appended as: `Once you understand the task, <command>`. This lets you say `dive-in -t URL "draft a PR description"` and have it threaded through the task-orientation flow.

For `--new`, the prompt comes from `dive_in_new.prompt.template` instead (which embeds the seed idea, instructs Claude to add the task, and concatenates `dive_in_context.prompt` as `{context}`).

For bare mode, the prompt is just the `command` string verbatim — no template wrapping.

On top of that, `cw ss` injects its own auto-base prompt (`prompts/shared/*` + `prompts/base/*`) into the system prompt — that's where the environment-awareness and interaction-policy rules live. `dive-in` doesn't need to know about it; it's appended by `cw.py:_load_base_prompts`.

## `--resume`

`--resume` resumes the latest Claude session in the workspace, skipping the initial prompt entirely (you're picking up mid-conversation, not orienting Claude to the task afresh).

Argparse-enforced rules in `dive_in.py:main`:
- **Requires a task scope** — `-t <ref>` or `--focus`. Bare `dive-in --resume` is rejected. (You need to know which workspace to resume; the slug comes from the task.)
- **Cannot be combined with `--new`** — `--new` creates a new task, which would create a new workspace; resuming is the opposite of new.
- **Cannot be combined with a positional `command`** — the initial prompt is ignored on resume, so passing one would silently do nothing.

When resuming, `dive-in` still resolves the task id (and, with `--focus`, still tells the focus client to focus it), sets `CW_TASK_ID`, and logs the task name — but skips building the initial prompt. It then forwards `--resume` through `cw.add_forwarded_flags` / `cw.extract_forwarded_argv` into `cw ss`, which is the layer that actually looks up the latest session jsonl and adds `--resume <session-id>` to the claude argv.

## Forwarded flags

`dive-in` accepts all the flags `cw.add_forwarded_flags` registers (`--auto`, `--fast`, `--aws`, `--effort`, `--rc`, `--resume`) and forwards them straight through into `cw ss`. Adding a new pass-through flag in `cw.py` makes it available to `dive-in` for free — no per-flag plumbing in this file.

`-n / --dry-run` prints the final `cw ss …` invocation (shell-quoted) without running it.

## Env-var handoff

- `CW_TASK_ID` — set to the resolved task id in any mode that has one (focused, `-t`, `-t --focus`). Picked up by `setup/claude_commit_footer.py` to insert `Task: <notion-url>` into commit messages.
- `PPP_SHELL_COMMAND` — set (if not already set) to the user-facing reconstruction of the dive-in invocation. Read by `prompts/base/environment.md` so Claude can self-detect that it was launched via `dive-in` (and whether `-t` / `--focus` / `--new` was used) without needing to be told.

The user-facing `dive-in` reconstruction is built explicitly in `dive_in.py` (not via `cw.add_forwarded_flags`'s reconstruct) so that env-detection sees `dive-in`, not the underlying `cw ss`.
