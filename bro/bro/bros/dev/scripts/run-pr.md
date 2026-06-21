---
name: pr
description: This skill should be used when the user signals that the worktree's changes are ready for review and a PR should be opened — "open a PR", "send for review", "PR it", "ship it", "ready for review", "finalize". Covers commit hygiene (CLAUDE.md sync, Dockerfile audit, policy audit, commit splitting), the project's commit-message style, footer generation via `./setup/claude_commit_footer.py`, submodule landing, rebase onto master, opens the PR via `gh pr create`, then launches `Monitor` + `poll-pr` to handle review comments and APPROVED events. In `--auto` mode, chains into `/land` automatically on approval. For the post-approval merge step, use `/land`.
version: 1.0.0
---

# /pr

Take worktree changes from "work is finished" to "PR open and through review". Stops at APPROVED — `/land` does the merge.

## Preconditions

- You are in a worktree (under `var/cw/worktrees/` or otherwise on a non-master branch). Do NOT run this against the main repo's working copy — the user's global CLAUDE.md forbids touching it.
- The work looks finished. If tests are failing, edits look WIP, or a refactor is half-done, confirm with the user before proceeding.

## Workflow

### 1. Survey the change

Run in parallel:
- `git status` — no `-uall` flag (memory issue on large repos).
- `git diff` — staged and unstaged together.
- `git log --oneline -10` — to match the repo's commit-message style.

If `git status` is clean and there are no untracked files to add, stop — nothing to land.

### 2. Pre-commit gates

Run before committing:
- `./format.sh` — formats Python via ruff. Stage any formatter-induced changes alongside your own.
- `./run_tests.py` — pyright + deptry + pytest + container smoke. **In a container session (`kind: container` from the `cw banner --llm` output), pass `--no-docker`** — the smoke step needs host Docker and will fail otherwise.

A red suite blocks the commit. Do not interpret or triage failures — propose fixing in this session or a separate one, but do not commit through failures.

**Policy audit**: before committing, re-read your Style guidance and audit your `git diff` for anything that violates it. The policies carry their own specifics, so this gate just re-applies all of them — including any added later. It's the cheapest place to catch a violation; the alternative is a review round-trip.

### 3. Sync CLAUDE.md

The user's global `PreToolUse:Bash` hook reminds you of this before each `git commit`, but do it proactively: if the change affects architecture, modules, commands, scripts, code style, or any section documented in `CLAUDE.md`, update it. Bundle the update into the relevant commit, or make it its own commit if the docs change stands alone.

### 4. Audit Dockerfiles

If the diff adds, deletes, or renames `.py` files, check every `Dockerfile` in the repo (`find . -name Dockerfile -not -path '*/.venv/*' -not -path '*/node_modules/*'`). For each Dockerfile that selectively copies `.py` files from a directory containing the changed files (e.g. `COPY notion/__init__.py notion/notion.py ... /app/notion/`), verify the COPY line is up to date — add new files, remove deleted files. Fix any discrepancies directly and mention them to the user.

### 5. Decide commit splits

If `/fix` already checkpointed completed units as it implemented, those commits are your splits — review them with `git log origin/master..HEAD`, commit any remaining uncommitted work the same way, and don't reorganize what's already on the branch. Otherwise, split the uncommitted work:

`CLAUDE.md` rule: split commits logically by feature/concern. Group by concern, not by file:
- Two unrelated fixes → two commits.
- A feature plus the docs for that feature → one commit.
- A rename plus its call-site updates → one commit.
- A CLAUDE.md overhaul that documents pre-existing state → its own commit.

Don't fragment trivially (every hunk as its own commit) and don't bundle unrelated work. If the split isn't obvious, ask the user.

### 6. Draft each commit message

Match recent-log style:

- **Title**: `<area>: <imperative lowercase summary>`, under ~70 chars. `<area>` is a module path or file stem — `cw`, `flow/mcp`, `.claude/settings`, `CLAUDE.md`, `sync-scripts`, etc.
- **Body**: terse — itemised bullets over prose, cap ~10 lines, often skipped entirely. Only context a reader can't recover from the code (motivation, constraint, non-obvious tradeoff).
- **Footer**: one blank line, then a `Task: <url>` line (resolve via `flow::get_task_info(task_id).address` — task id comes from `CW_TASK_ID` env var or a `flow::add_task` call earlier in this session), then the two-line output of `./setup/claude_commit_footer.py` (the per-commit token delta plus the session id). Example:
  ```
  Task: https://www.notion.so/my-task-abc123
  > created with Claude Code 2.1.181 | Opus 4.8: 45'231
  > session(s): 04ee83b5-ff91-4740-8791-073d14939b91
  ```
  Omit the `Task:` line if no task ID is available.
- **Never** include `Co-Authored-By:` lines or "Generated with Claude Code" boilerplate.
- Log/echo/help strings in code: lowercase, no trailing dots, neutral tone (per `CLAUDE.md`).

**Regenerate the footer immediately before every commit — and again before every retry.** It emits the token *delta* since this session's last successful commit (the baseline lives in the gitignored `.token_accounting_state.json`), and the session cumulative grows with each attempt, so a reused footer would misattribute the delta. A `post-commit` git hook advances the baseline only after a commit actually lands, which keeps retries and footerless commits correct — so never reuse an earlier footer.

```bash
./setup/claude_commit_footer.py
```

### 7. Commit

Use a HEREDOC to preserve formatting. Stage specific files — never `git add -A` or `git add .` (risks committing `.configs/` or other secrets). Never `git add -N` (intent-to-add) — it breaks `git stash create`, which `/ultrareview` and some hooks rely on.

```bash
git add path/to/file1 path/to/file2
git commit -m "$(cat <<'EOF'
<area>: <short imperative summary>

<optional terse body>

Task: https://www.notion.so/...
> created with Claude Code 2.1.181 | Opus 4.8: 45'231
> session(s): 04ee83b5-ff91-4740-8791-073d14939b91
EOF
)"
```

If a pre-commit hook fails: fix the issue, re-stage, and create a **new** commit. Never `--amend` (the original commit didn't happen — amend would modify the previous one and destroy work). Never `--no-verify`.

Repeat steps 6–7 for each logical commit.

To verify a new test catches a bug (revert-and-rerun), use `git stash push <path-to-fix-file>` — bare `git stash` would also hide the new test, masking the verification.

### 8. Land submodules

Before pushing the main repo, check whether any submodules have commits that aren't on their remote. For each submodule with changes:

1. **Pre-flight remote writability.** Check the submodule's remote is writable (read-only `/host-repo` bind mounts in `cw -c` containers will silently fail at push time):
   ```bash
   git -C <submodule> remote get-url origin
   # if the URL points at a local path, `test -w <path>` to confirm writability
   ```
   If the remote is read-only, surface the constraint *now* — do not commit inside the submodule until the user fixes the mount or commits on the host instead.

2. `cd` into the submodule directory.
3. `git fetch origin` and check if the current commit is reachable from `origin/master` (or `origin/HEAD`).
4. If not, push the submodule: `git push origin HEAD:master` (or the appropriate branch).
5. Return to the worktree root.

The main repo must not be pushed until all submodules it references are available on their remotes — otherwise anyone cloning/pulling gets a dangling submodule pointer.

### 9. Rebase onto master

```bash
git fetch origin master && git rebase origin/master
```

Conflicts → stop and report to the user. Do not `--abort` or `--skip` without asking. Prefer `Edit` over `Write` for resolving conflict markers — re-Read in conflict state, replace each `<<<<<<<...=======...>>>>>>>` block with the merged version. Cheaper than rewriting whole files.

### 10. Verify PR scope

```bash
git log origin/master..HEAD --oneline
```

Confirm the commit list matches the intended PR title and body. If the worktree carries unrelated in-flight commits, either:
- Split the branch (move unrelated commits to a separate branch), or
- Rewrite the planned PR title/body to cover the full set, or
- Ask the user.

Do not silently open a PR whose scope is wider than its title says.

### 11. Push the branch

```bash
git push -u origin HEAD
```

### 12. Open the PR

Use `gh` for everything GitHub-related — it's pre-authenticated via `$GH_TOKEN`, auto-detects the repo from the origin remote, and handles JSON encoding. Do not use `curl` against `api.github.com`.

Build the PR title and body:
- **Title**: if single commit, use its title. If multiple commits, use the first commit's `<area>:` prefix + a brief summary.
- **Body**: `Task:` line linking the Notion URL (if `CW_TASK_ID` is set), then `## Summary` bullets describing the changes, then a `## Test plan` checklist.

```bash
gh pr create --base master --title "<title>" --body "$(cat <<'EOF'
Task: https://www.notion.so/...

## Summary
- ...

## Test plan
- [ ] ...
EOF
)"
```

Report the PR URL `gh` prints to the user.

### 13. Log "PR opened" to the task (dive-in sessions only)

If this session was launched via `dive-in` (check `launch_command` from `cw banner --llm`) and you have access to flow MCP tools:

```
### PR opened — @YYYY-MM-DD HH:MM
<pr-url>
- [`<short-hash>`](<repo-url>/commit/<full-hash>) <commit title>
- [`<short-hash>`](<repo-url>/commit/<full-hash>) <commit title>
```

Use `date '+%Y-%m-%d %H:%M'` for the timestamp — do not invent it. Build commit links from `git remote get-url origin` (strip trailing `.git`).

### 14. Launch the review watcher

**MUST launch via the `Monitor` tool with `persistent: true`. Do NOT use Bash `run_in_background`** — that only notifies on process exit, so review/comment events sit silently in the output file and approvals never trigger the auto-chain.

`--self` filters out your own bot identity. `$GITHUB_ACTOR` is unset in dive-in containers, so derive it from `gh api user` instead.

```bash
poll-pr <owner>/<repo> <pr_number> --token "$GH_TOKEN" --self "$(gh api user --jq '.login')" --allow-env
```

`poll-pr` outputs JSON-lines to stdout:
- `{"event": "merged", "pr": N}` — PR was merged
- `{"event": "closed", "pr": N}` — PR was closed without merging
- `{"event": "comment", "id": N, "user": "...", "body": "...", "path": "...", "url": "..."}` — new comment from the repo owner (bot and self filtered out). Standalone inline review comments (replies to existing review threads) fire here; inline comments attached to a fresh review are bundled into the `review` event instead.
- `{"event": "review", "id": N, "user": "...", "state": "APPROVED|CHANGES_REQUESTED|COMMENTED|DISMISSED", "body": "...", "url": "...", "comments": [{"id": N, "path": "...", "line": N, "body": "...", "url": "..."}]}` — new review. `comments` is the array of inline comments attached to this review at the moment `poll-pr` saw it (typically all of them; rarely empty if the inline-comments endpoint lags the reviews endpoint — late arrivals then fire as standalone `comment` events on a later cycle).

### 15. React to review events

**`comment` event** or **`review` with `state: "CHANGES_REQUESTED"` or non-empty `comments`**:

A non-empty `comments` array on an APPROVED review counts as actionable feedback too — the reviewer may be saying "ship after fix" via inline nits even when the top-level review body is empty. Read every comment in the array before chaining.

1. Read and understand the feedback (review body + every `comments[]` entry).
2. Make the requested code changes locally.
3. Re-run tests (`./run_tests.py --no-docker` inside a container).
4. Commit (a **new** commit, not `--amend`) with the same conventions as step 6–7.
5. Push: `git push origin HEAD`.
6. Reply on the PR confirming the fix (reference the commit SHA):
   - **Top-level PR comment**: `gh pr comment <n> --body "..."`
   - **Reply to a specific review comment** (endpoint includes the PR number `<n>`):
     ```bash
     gh api -X POST repos/<owner>/<repo>/pulls/<n>/comments/<comment_id>/replies -f body="..."
     ```

**`review` with `state: "APPROVED"` and empty `comments`**:

Unconditional approval. Stop polling and surface:

> PR approved — ready to merge. Invoke `/land` to squash and close.

**In `--auto` sessions** (detect via `launch_command` from `cw banner --llm` containing `--auto`), immediately invoke `/land` to chain into the merge. **In manual sessions**, wait for the user's `/land` (or "land it") trigger.

**`review` with `state: "COMMENTED"` or `"DISMISSED"`**: informational; the actionable feedback (if any) is in this event's `comments` array or arrives via accompanying `comment` events.

**`merged` / `closed`**: someone (the user, `/land`, or external action) terminated the PR. If `merged`, hand off to `/land`'s post-merge steps — append `### Merged` entry and propose closing the task. If `closed` without merge, log it and ask the user.

## Safety rules

- Never commit credentials: `.configs/`, anything under `dot-ppp/`. If these show up in `git status`, warn the user before staging.
- Never skip hooks (`--no-verify`, `--no-gpg-sign`, etc.).
- Never force-push (`--force`, `--force-with-lease`) to master.
- Never stage with `git add -A` / `git add .` — stage by explicit path.
- Never `git add -N` — breaks `git stash create`.
- For verify-test-catches-bug, use `git stash push <file>`, never bare `git stash`.
