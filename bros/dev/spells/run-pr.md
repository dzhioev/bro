---
name: run-pr
description: This spell should be used when the user signals that the worktree's changes are ready for review and a PR should be opened — "open a PR", "[[run pr]]", "send for review", "PR it", "ship it", "ready for review", "finalize". Covers commit hygiene (docs sync, policy audit, commit splitting), the repo's commit-message conventions, rebases onto the base branch (master by default), opens the PR via `gh pr create`, then launches the `poll-pr` review watcher to handle review comments, failing CI checks, merge conflicts, and APPROVED events. On approval, chains into [[land]] for the merge step. Also the re-entry point for a PR that is already open — "resume PR <pr-url-or-number>", "resume the PR", "pick up the review" — checking out the PR's head branch, reconciling unaddressed feedback, and resuming the watch.
parameters: {"base?": "base branch for the pull request instead of master", "pr?": "existing pull request URL or number to resume"}
version: 4.4.1
---

# run-pr

Take worktree changes from "work is finished" to "PR open and through review". Stops at APPROVED — [[land]] does the merge.

## Arguments

Passed values appear in the `# Arguments` section appended by the spell tool:

- `base` — base the PR on this branch instead of `master`: rebase onto it (step 7), scope the commit list against it (steps 4, 9), and pass `--base <branch>` to `gh pr create` (step 11). Default `master`. A coordinator driving a multi-stage feature passes the feature integration branch here so each stage opens its PR into the feature branch rather than master. Below, `<base>` means this value.
- `pr` — re-entry mode for an existing PR URL or number (typically after a previous session died mid-review). Skip the normal workflow and follow "Re-entry: PR already open" below.

## Preconditions

Normal flow only — re-entry has its own entry conditions:

- You are in a managed workspace (under `var/cw/workspaces/` or otherwise on a non-master branch). Do NOT run this against the main checkout's working copy.
- The work looks finished. If edits look WIP, a refactor is half-done, or the suite is known-red, confirm with the user before proceeding.

## Re-entry: PR already open

The `pr` argument carries the existing PR URL or number — e.g. `bro run <bro> '[[resume PR <pr-url>]]'` after the session that opened it died. Restore the state that session had, reconcile what happened while nobody watched, then rejoin the normal flow at the watcher (step 13).

1. **Check out the PR's head branch first**: `gh pr checkout <number-or-url>`. A fresh clone sits on a `worktree-<name>` branch at the base ref — the PR's head branch is not checked out locally; `gh pr checkout` fetches it and sets up tracking so later pushes go to the right branch.
2. **Recover the context** the environment no longer carries (`CW_TASK_ID` is unset here):
   ```bash
   gh pr view <number> --json number,url,state,baseRefName,title,body
   ```
   - The task link is the `Task:` line in the PR body — use it for the task-logging steps (13, and [[land]]'s bookkeeping). No `Task:` line → proceed without task logging.
   - `<base>` is `baseRefName`.
3. **Handle a terminal PR**: `state: MERGED` → run [[land]]'s post-merge bookkeeping (`merged` comment, task closure) and stop; `state: CLOSED` → report it and stop.
4. **Reconcile unaddressed feedback from `gh` state — never trust a lost watcher.** The dead session may have died before, during, or after handling any event, and a restarted `poll-pr` baselines all existing events as already seen — feedback left unhandled now would be silently skipped forever. Pull the full review state:
   ```bash
   gh pr view <number> --json reviews,comments
   gh api repos/<owner>/<repo>/pulls/<number>/comments   # inline review comments
   ```
   Treat as actionable any repo-owner feedback per step 14's rules that has no later reply from the PR author and no later commit addressing it; handle each per step 14. If the latest owner review is APPROVED and nothing actionable is pending, chain straight into [[land]] — no watcher needed.
5. **Resume watching**: continue at step 13.

## Workflow

### 1. Survey the change

Run in parallel:
- `git status` — no `-uall` flag (memory issue on large repos).
- `git diff` — staged and unstaged together.
- `git log --oneline -10` — to match the repo's commit-message style.

If `git status` is clean and there are no untracked files to add, stop — nothing to land.

### 2. Pre-commit gates

Run before committing:
- The repo's formatter (its own docs name the command). Stage any formatter-induced changes alongside your own.

No full-suite run here: the suite is the pre-push gate (step 8), run once on the final rebased tree — a pass before the rebase is evidence the rebase discards.

**Policy audit**: before each commit, call `dev-style-source::read` and audit that commit's `git diff` against the returned policy text. The tool read is part of the gate — it puts the policy fresh in context instead of relying on recall of a read far behind. The policies carry their own specifics, so this gate just re-applies all of them.

Treat it as a precondition of the `git commit`, run at the moment of committing — not an intention you form earlier and carry across other steps. **State the verdict as visible output before the commit**: a one-line `clean`, or the violation(s) you found and how you fixed them. An audit done only in your head is indistinguishable from one skipped, so an interruption (a `[Request interrupted]`, a "continue") can silently drop it; a written verdict can't be dropped unnoticed. It's the cheapest place to catch a violation — the alternative is a review round-trip.

### 3. Sync the repo docs

If the repo documents itself (`AGENTS.md` files or an equivalent) and the change affects architecture, modules, commands, spells, code style, or any documented section, update the docs. Bundle the update into the relevant commit, or make it its own commit if the docs change stands alone.

### 4. Decide commit splits

If [[fix]] already checkpointed completed units as it implemented, those commits are your splits — review them with `git log origin/<base>..HEAD`, commit any remaining uncommitted work the same way, and don't reorganize what's already on the branch. Otherwise, split the uncommitted work:

Split commits logically by feature/concern. Group by concern, not by file:
- Two unrelated fixes → two commits.
- A feature plus the docs for that feature → one commit.
- A rename plus its call-site updates → one commit.
- A docs overhaul that documents pre-existing state → its own commit.

Don't fragment trivially (every hunk as its own commit) and don't bundle unrelated work. If the split isn't obvious, ask the user.

### 5. Draft each commit message

Match the repo's conventions — the recent log (step 1) is the reference, and the repo's docs may spell them out. Then:

- **Task metadata**: add the task link the repo requires (resolve it via `brog::get_task(task_id).url`; the task id comes from `CW_TASK_ID` or a task created earlier in this session). Omit task metadata when no task id is available.
- **Never** include `Co-Authored-By:` lines or generated-by boilerplate unless the repo's policy explicitly requires them.

### 6. Commit

Use a HEREDOC to preserve formatting. Stage specific files — never `git add -A` or `git add .` (risks committing credential stores or other untracked secrets). Never `git add -N` (intent-to-add) — it breaks `git stash create`, which stash-based review tooling and some hooks rely on.

```bash
git add path/to/file1 path/to/file2
git commit -m "$(cat <<'EOF'
<short imperative summary in the repo's style>

<optional terse body>

<task metadata required by the repo's policy>
EOF
)"
```

If a pre-commit hook fails: fix the issue, re-stage, and create a **new** commit. Never `--amend` (the original commit didn't happen — amend would modify the previous one and destroy work). Never `--no-verify`.

Repo hooks may append trailers to the committed message (e.g. a token-accounting footer); never hand-write, edit, or strip them.

Repeat steps 5–6 for each logical commit.

To verify a new test catches a bug (revert-and-rerun), use `git stash push <path-to-fix-file>` — bare `git stash` would also hide the new test, masking the verification.

### 7. Rebase onto the base branch

```bash
git fetch origin <base> && git rebase origin/<base>
```

Conflicts → resolve them yourself, in-band: merge each conflict, `git add` the resolved paths, `git rebase --continue`. Then record the resolution on the task (same conditions as step 12): `brog::add_comment(task_id, topic='rebase conflicts', body=...)` naming the conflicted files and the resolution each one took — the pre-push gate (step 8) verifies the resolved result next.

Escalate only when a resolution is not obvious — the two sides carry contradicting logic or intent that no merged version can honor both of: stop and ask when questions reach the user; raise with the contradiction spelled out when unattended. Never `--abort` or `--skip` silently.

In an **unattended** session there is no user to stop for: when a conflict clears that escalation bar, rescue your commits per _Rescue committed work before a raise_ below (abort the rebase to restore them, push the branch, name the ref), then `raise` with the contradiction as the reason — the parked commits stay recoverable.

### 8. Pre-push gate: the full test suite

The flow's one mandatory suite pass — on the final rebased tree, immediately before anything is published. Run the repo's full verification gate (its own docs name the command and any environment-specific flags).{{when #harness = bro}} Run it with an explicit large `timeout_seconds` (600 fits) — `dev::bash`'s default kills a full suite mid-run; same for any other long command.{{end}}

A red suite blocks the push. Do not interpret or triage failures — fix with new commits, or propose fixing pre-existing failures in this session or a separate one; do not push through failures.

Earlier full passes (per checkpoint, pre-rebase) are optional and usually redundant — this gate re-verifies the exact tree that ships.

### 9. Verify PR scope

```bash
git log origin/<base>..HEAD --oneline
```

Confirm the commit list matches the intended PR title and body. If the worktree carries unrelated in-flight commits, either:
- Split the branch (move unrelated commits to a separate branch), or
- Rewrite the planned PR title/body to cover the full set, or
- Ask the user.

Do not silently open a PR whose scope is wider than its title says.

### 10. Push the branch

```bash
git push -u origin HEAD
```

### 11. Open the PR

Use `gh` for everything GitHub-related — it's pre-authenticated, auto-detects the repo from the origin remote, and handles JSON encoding. Do not use `curl` against `api.github.com`.

Build the PR title and body:
- **Title**: if single commit, use its title. If multiple commits, a brief summary in the same style.
- **Body**: `Task:` line linking the task URL (if a task id is known), then `## Summary` bullets describing the changes, then a `## Test plan` checklist of what you verified, each box ticked. Don't list a step this session cannot run — the repo's own CI gate runs on the PR, and an unticked box blocks [[land]] later with nobody able to clear it.

```bash
gh pr create --base <base> --title "<title>" --body "$(cat <<'EOF'
Task: https://tracker.example.com/...

## Summary
- ...

## Test plan
- [ ] ...
EOF
)"
```

Report the PR to the user as a **review link**: a markdown hyperlink titled `#<n>` (the PR number) whose target is the review section — the PR URL with `/files` appended (the Files changed tab, where the review UI lives) — not the main PR page. `gh pr create` prints the PR URL; `<n>` is its last path segment.

```markdown
[#<n>](https://github.com/<owner>/<repo>/pull/<n>/files)
```

Surface this link every time the PR enters review-pending: here at creation, and again after each push of review-fix commits (step 14).

### 12. Log "PR opened" to the task (sessions with a task)

If the session has a task (`CW_TASK_ID` — `dive-in` sets it — or a task resolved earlier in this session) and the brog tools are available, record the event with `brog::add_comment(task_id, topic='PR opened', body=...)`:

```
[PR:<n>](<pr-url>)
- [`<short-hash>`](<repo-url>/commit/<full-hash>) <commit title>
- [`<short-hash>`](<repo-url>/commit/<full-hash>) <commit title>
```

Build commit links from `git remote get-url origin` (strip trailing `.git`).

### 13. Launch the review watcher

The watcher is one long-lived `poll-pr` process. It authenticates through the credential store (`--credential`, default `github`), re-resolved each cycle so the watch survives short-lived minted tokens; your own comments are filtered via `--self`, which defaults to the PR author:

```bash
poll-pr <owner>/<repo> <pr_number>
```

`poll-pr` outputs JSON-lines to stdout:
- `{"event": "merged", "pr": N}` — PR was merged
- `{"event": "closed", "pr": N}` — PR was closed without merging
- `{"event": "conflicts", "pr": N}` — the PR became unmergeable into its base (GitHub's `mergeable` turned false, typically after something landed on the base). Fires once per conflicted episode — it re-arms only after the PR turns mergeable again.
- `{"event": "checks", "pr": N, "failing": [{"name": "...", "conclusion": "...", "url": "..."}]}` — a status check on the PR's head commit concluded as a failure. Fires once per red episode — it re-arms only after nothing is failing again (a re-run that goes green, or a new push).
- `{"event": "comment", "id": N, "user": "...", "body": "...", "path": "...", "url": "..."}` — new comment from the repo owner (bot and self filtered out). Standalone inline review comments (replies to existing review threads) fire here; inline comments attached to a fresh review are bundled into the `review` event instead.
- `{"event": "review", "id": N, "user": "...", "state": "APPROVED|CHANGES_REQUESTED|COMMENTED|DISMISSED", "body": "...", "url": "...", "comments": [{"id": N, "path": "...", "line": N, "body": "...", "url": "..."}]}` — new review. `comments` is the array of inline comments attached to this review at the moment `poll-pr` saw it (typically all of them; rarely empty if the inline-comments endpoint lags the reviews endpoint — late arrivals then fire as standalone `comment` events on a later cycle).
- `{"event": "watch_failed", "pr": N, "source": "baseline|pull-request|checks|reviews", "reason": "...", "failing_for": N}` — terminal: the named event source kept failing past the grace window, so the watch ended rather than going quiet on those events — the process exits nonzero right after. A short blip costs nothing: one source failing is tolerated for `--failure-grace` seconds while the others keep reporting. `baseline` means the watch never started at all.

How to run it:

{{iff #harness = bro}}
Run it as a background job and read it iteratively — a plain `dev::bash` call would kill it at its timeout:

1. `dev::job("poll-pr …")` → note the job id.
2. Loop on `dev::watch(job_id, wait_seconds=1500)`. Each return is one iteration:
   - new output → react to every JSON line per step 14, then watch again;
   - a bare `running` state line (quiet window) → watch again;
   - `exited` right after a `merged`/`closed` event → the PR is terminal; react per step 14, stop looping;
   - `exited` right after a `watch_failed` event → react per step 14, stop looping;
   - `exited` with no terminal event → the watcher died. Do not just restart it — a fresh `poll-pr` baselines all existing events as seen; reconcile first (re-entry step 4), then start a new `dev::job`.
3. When chaining into [[land]], stop the watcher with `dev::kill(job_id)`.

The large `wait_seconds` keeps the run idling inside the tool call between events; don't shorten it to poll — every quiet return costs a full model round trip.

**The watch loop is the rest of the run.** Your terminal answer comes only after the PR reaches a terminal state — merged (typically via the [[land]] chain on APPROVED) or closed. Until then, keep calling `dev::watch` iteration after iteration, however quiet the PR stays; that idling is the run working as designed, not a stall to wrap up. Do not kill the job and end the run with a "waiting for review" report — an ended run watches nothing, and every later review event goes unhandled.
{{eliff #harness = claude}}
**MUST launch via the `Monitor` tool with `persistent: true`. Do NOT use Bash `run_in_background`** — that only notifies on process exit, so review/comment events sit silently in the output file and approvals never trigger the auto-chain. The harness wakes you on each output event; react per step 14. Stop the watcher with `TaskStop` when chaining into [[land]].

If the `Monitor` schema needs a `ToolSearch` fetch, load `TaskStop` in the same query (`select:Monitor,TaskStop`) — the APPROVED handler needs it and shouldn't spend a round trip on it later.
{{end}}

### 14. React to review events

**`comment` event** or **`review` with `state: "CHANGES_REQUESTED"` or non-empty `comments`**:

A non-empty `comments` array on an APPROVED review counts as actionable feedback too — the reviewer may be saying "ship after fix" via inline nits even when the top-level review body is empty. Read every comment in the array before chaining.

Handle pending feedback as one batch: address every comment that has arrived, then pay one gate pass and one push for the batch — not a suite run per comment.

1. Read and understand the feedback (review body + every `comments[]` entry).
2. Make the requested code changes locally.
3. Re-run the pre-commit gates (step 2).
4. Commit (a **new** commit, not `--amend`) with the same conventions as step 5–6.
5. Run the pre-push gate (step 8).
6. Push: `git push origin HEAD`.
7. Reply on the PR confirming the fix (reference the commit SHA):
   - **Top-level PR comment**: `gh pr comment <n> --body "..."`
   - **Reply to a specific review comment** (endpoint includes the PR number `<n>`):
     ```bash
     gh api -X POST repos/<owner>/<repo>/pulls/<n>/comments/<comment_id>/replies -f body="..."
     ```
8. Surface the review link again (step 11's `[#<n>](<pr-url>/files)` format) — the pushed commits put the PR back into review-pending.

**`review` with `state: "APPROVED"` and empty `comments`**:

Unconditional approval — the PR is ready to merge. Chain into the merge, and batch it: stop the watcher ({{iff #harness = bro}}`dev::kill(job_id)`{{else}}`TaskStop`{{end}}) and [[land]] **in the same response**, then follow it (it reads the branch to decide what master should carry, then merges with a single `land-pr` command).

**`review` with `state: "COMMENTED"` or `"DISMISSED"`**: informational; the actionable feedback (if any) is in this event's `comments` array or arrives via accompanying `comment` events.

**`checks` event**: CI went red on what you pushed. Fetch the failing run's log (`gh run view --log-failed <run-id>`, the id is the tail of the event's `url`) and diagnose it as your own breakage — a failure the local gate missed is the interesting kind (environment-dependent, ordering-dependent, or a file you forgot to stage). Fix it exactly like review feedback: gates (step 2), a new commit (steps 5–6), the pre-push gate (step 8), push. Report the failure and your fix to the user; never wait for it to disappear on a re-run you didn't trigger, and never land around it — `land-pr` refuses a failed check anyway.

**`conflicts` event**: rerun step 7 — the rebase, the in-band resolution default, the escalation bar, and the task comment all apply unchanged — then the pre-push gate (step 8), then push the rebased branch: `git push --force-with-lease origin HEAD` — the PR branch, never the base.

**`merged` / `closed`**: someone (the user, a [[land]] run, or external action) terminated the PR. If `merged`, run [[land]]'s post-merge bookkeeping — the `merged` comment and task closure. If `closed` without merge, log it and report to the user.

**`watch_failed` event**: the watch is over and the PR is not — from here on nothing on the PR reaches you. The `reason` is typically a credential the watch cannot use for that source (`HTTP 403` / `HTTP 404` on `checks` means the token lacks `checks: read`) or GitHub being unreachable for minutes. Do not restart the watcher on the same credential and hope: reconcile the review state from `gh` (re-entry step 4) and handle whatever arrived, then report the failing source and reason to the user — stop and ask when questions reach the user; when unattended, `raise` with the source and reason as the reason.

## Rescue committed work before a raise

{{include fragments/rescue_before_raise.md}}

## Safety rules

- Never commit credential material — credential stores, key files, minting configs. If anything that looks like a secret shows up in `git status`, warn the user before staging.
- Never skip hooks (`--no-verify`, `--no-gpg-sign`, etc.).
- Never force-push (`--force`, `--force-with-lease`) to master.
- Never stage with `git add -A` / `git add .` — stage by explicit path.
- Never `git add -N` — breaks `git stash create`.
- For verify-test-catches-bug, use `git stash push <file>`, never bare `git stash`.
