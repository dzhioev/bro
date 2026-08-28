---
name: run-pr
description:

This spell should be used when the user signals that the worktree's changes are ready for review and a PR should be opened
— "open a PR", "[[run pr]]", "send for review", "PR it", "ship it", "ready for review", "finalize".
Covers commit hygiene (docs sync, policy audit, commit splitting),
the repo's commit-message conventions,
rebases onto the base branch (master by default),
folds the branch into the commits master will carry so what is reviewed is what lands,
opens the PR via `gh pr create`,
then launches the `poll-pr` review watcher to handle review comments, failing CI checks, merge conflicts, and APPROVED events.
On approval, chains into [[land]] for the merge step.
Also the re-entry point for a PR that is already open
— "resume PR <pr-url-or-number>", "resume the PR", "pick up the review"
— checking out the PR's head branch, reconciling unaddressed feedback, and resuming the watch.

parameters: {"base?": "base branch for the pull request instead of master", "pr?": "existing pull request URL or number to resume"}
version: 5.4.0
---

# run-pr

Take worktree changes from "work is finished" to "PR open and through review".
Stops at APPROVED — [[land]] does the merge.

## Arguments

Passed values appear in the `# Arguments` section appended by the spell tool:

- `base` — base the PR on this branch instead of `master`:
  rebase onto it (step 7), fold against it (step 8), scope the commit list against it (steps 4, 10), and pass `--base <branch>` to `gh pr create` (step 12).
  Default `master`.
  A coordinator driving multi-stage work passes its integration branch here so each stage opens its PR into that branch rather than master.
  Below, `<base>` means this value.
- `pr` — re-entry mode for an existing PR URL or number (typically after a previous session died mid-review).
  Skip the normal workflow and follow "Re-entry: PR already open" below.

## Preconditions

Normal flow only — re-entry has its own entry conditions:

- You are in a managed workspace (under the runtime state root's `workspaces/` or otherwise on a non-master branch).
  Do NOT run this against the main checkout's working copy.
- The work looks finished.
  If edits look WIP, a refactor is half-done, or the suite is known-red, confirm with the user before proceeding.

## Re-entry: PR already open

The `pr` argument carries the existing PR URL or number
— e.g. `bro run <bro> '[[resume PR <pr-url>]]'` after the session that opened it died.
Restore the state that session had, reconcile what happened while nobody watched, then rejoin the normal flow at the watcher (step 14).

1. **Check out the PR's head branch first**:
   `gh pr checkout <number-or-url>`.
   A fresh clone sits on a `worktree-<name>` branch at the base ref
   — the PR's head branch is not checked out locally;
   `gh pr checkout` fetches it and sets up tracking so later pushes go to the right branch.
2. **Recover the context** the environment no longer carries (`RIDE_TASK_ID` is unset here):
   ```bash
   gh pr view <number> --json number,url,state,baseRefName,title,body
   ```
   - The task link is the `Task:` line in the PR body
     — use it for the task-logging steps (13, and [[land]]'s bookkeeping).
     No `Task:` line → proceed without task logging.
   - `<base>` is `baseRefName`.
3. **Handle a terminal PR**:
   `state: MERGED` → run [[land]]'s post-merge bookkeeping (`merged` comment, task closure) and stop;
   `state: CLOSED` → report it and stop.
4. **Reconcile unaddressed feedback from `gh` state
   — never trust a lost watcher.**
   The dead session may have died before, during, or after handling any event, and a restarted `poll-pr` baselines all existing events as already seen
   — feedback left unhandled now would be silently skipped forever.
   Pull the full review state:
   ```bash
   gh pr view <number> --json reviews,comments
   gh api repos/<owner>/<repo>/pulls/<number>/comments   # inline review comments
   ```
   Treat as actionable any repo-owner feedback per step 15's rules that has no later reply from the PR author and no later commit addressing it;
   handle each per step 15.
   If the latest owner review is APPROVED and nothing actionable is pending, chain straight into [[land]]
   — no watcher needed.
5. **Resume watching**:
   continue at step 14.

## Workflow

### 1. Survey the change

Run in parallel:
- `git status` — no `-uall` flag (memory issue on large repos).
- `git diff` — staged and unstaged together.
- `git log --oneline -10`
  — to match the repo's commit-message style.

If `git status` is clean and there are no untracked files to add, stop
— nothing to land.

### 2. Pre-commit gates

Run before committing:
- The repo's formatter (its own docs name the command).
  Stage any formatter-induced changes alongside your own.

No full-suite run here:
the suite is the mandatory gate (step 9), run once on the final rebased tree
— a pass before the rebase is evidence the rebase discards.

{{iff #may_summon contains eyebro}}
**Policy audit**:
owned by the eyebro's independent review of the whole branch ("Pre-review by the eyebro", after step 6)
— nothing to audit per commit.
{{else}}
**Policy audit**:
before each commit, call `dev-style-source::read` and audit that commit's `git diff` against the returned policy text.
The tool read is part of the gate
— it puts the policy fresh in context instead of relying on recall of a read far behind.
The policies carry their own specifics, so this gate just re-applies all of them.

Treat it as a precondition of the `git commit`, run at the moment of committing
— not an intention you form earlier and carry across other steps.
**State the verdict as visible output before the commit**:
a one-line `clean`, or the violation(s) you found and how you fixed them.
An audit done only in your head is indistinguishable from one skipped, so an interruption (a `[Request interrupted]`, a "continue") can silently drop it;
a written verdict can't be dropped unnoticed.
It's the cheapest place to catch a violation
— the alternative is a review round-trip.
{{end}}

### 3. Sync the repo docs

If the repo documents itself (`AGENTS.md` files or an equivalent) and the change affects architecture, modules, commands, spells, code style, or any documented section, update the docs.
Bundle the update into the relevant commit, or make it its own commit if the docs change stands alone.

### 4. Decide commit splits

If [[fix]] already checkpointed completed units as it implemented, those commits are your splits
— review them with `git log origin/<base>..HEAD`, commit any remaining uncommitted work the same way, and don't reorganize what's already on the branch.
Otherwise, split the uncommitted work:

Split commits logically by feature/concern.
Group by concern, not by file:
- Two unrelated fixes → two commits.
- A feature plus the docs for that feature → one commit.
- A rename plus its call-site updates → one commit.
- A docs overhaul that documents pre-existing state → its own commit.

Don't fragment trivially (every hunk as its own commit) and don't bundle unrelated work.
If the split isn't obvious, ask the user.

### 5. Draft each commit message

Match the repo's conventions
— the recent log (step 1) is the reference, and the repo's docs may spell them out.
Then:

- **Task metadata**:
  add the task link the repo requires (resolve it via `brog::get_task(task_id).url`; the task id comes from `RIDE_TASK_ID` or a task created earlier in this session).
  Omit task metadata when no task id is available.
- **Never** hand-write `Co-Authored-By:` lines or generated-by boilerplate.
  An interactive session's commit hook adds the co-author trailer itself (`bro.workflow.co_author`);
  a repo whose policy wants more says so.

### 6. Commit

Use a HEREDOC to preserve formatting.
Stage specific files — never `git add -A` or `git add .` (risks committing credential stores or other untracked secrets).
Never `git add -N` (intent-to-add)
— it breaks `git stash create`, which stash-based review tooling and some hooks rely on.

```bash
git add path/to/file1 path/to/file2
git commit -m "$(cat <<'EOF'
<short imperative summary in the repo's style>

<optional terse body>

<task metadata required by the repo's policy>
EOF
)"
```

If a pre-commit hook fails:
fix the issue, re-stage, and create a **new** commit.
Never `--amend` (the original commit didn't happen — amend would modify the previous one and destroy work).
Never `--no-verify`.

Repo hooks may append trailers to the committed message (e.g. a token-accounting footer);
never hand-write, edit, or strip them.

Repeat steps 5–6 for each logical commit.

To verify a new test catches a bug (revert-and-rerun), use `git stash push <path-to-fix-file>`
— bare `git stash` would also hide the new test, masking the verification.

{{when #may_summon contains eyebro}}

### Pre-review by the eyebro

The branch's style audit:
one independent review of the whole change, paid once before the PR instead of per commit.

With the branch committed, [[ask]] the eyebro for a style review and wait on the findings
— the child bases on this workspace's HEAD and shares no context, so the relayed request names what to diff against:

    [[review diff of HEAD against origin/<base>, in terms of the development style policy and the repository's own guides]]

Handle the findings at pre-PR prices:
fix what is right with further commits (steps 5–6), and let go of what you'd only debate
— the eyebro reviews the PR next ("Hand the review to the eyebro"), where a finding it still holds returns as a thread.
No identity constraint binds this step:
a diff review approves nothing.
If the ask is denied or fails, audit the branch yourself before moving on:
call `dev-style-source::read` and audit `git diff origin/<base>..HEAD` against the returned policy, stating the verdict as visible output.

{{end}}

### 7. Rebase onto the base branch

```bash
git fetch origin <base> && git rebase origin/<base>
```

Conflicts → resolve them yourself, in-band:
merge each conflict, `git add` the resolved paths, `git rebase --continue`.
Then record the resolution on the task (same conditions as step 13):
`brog::add_comment(task_id, topic='rebase conflicts', body=...)` naming the conflicted files and the resolution each one took
— the gate (step 9) verifies the resolved result next.

Escalate only when a resolution is not obvious
— the two sides carry contradicting logic or intent that no merged version can honor both of:
stop and ask when questions reach the user;
raise with the contradiction spelled out when unattended.
Never `--abort` or `--skip` silently.

In an **unattended** session there is no user to stop for:
when a conflict clears that escalation bar, rescue your commits per _Rescue committed work before a raise_ below (abort the rebase to restore them, push the branch, name the ref), then `raise` with the contradiction as the reason
— the parked commits stay recoverable.

### 8. Fold the branch into what master will carry

Master carries one commit per logical change, and the branch is rarely shaped that way already:
[[fix]]'s checkpoints are commits about *making* the change, not changes of their own.
The reviewer approves commits, not a diff
— so the branch is folded into the ones that will land before anyone looks at it, and the merge rewrites nothing afterwards.

Read what the branch became and decide what it should land as:

```bash
git log origin/<base>..HEAD --oneline
```

One commit is the default:
a branch that does one thing lands as one commit, checkpoint noise folded away.
Land several when the branch carries a change that stands on its own beside the task
— the unrelated bug fixed along the way, a drive-by cleanup worth its own line in the history.
The user's own words settle it either way ("land these as two commits", "one commit please").

Write the plan as a scratch file (untracked — never stage it), one `fold` line per landed commit, oldest first, with that commit's message in the lines under it:

```
fold
<the subject the branch lands under>

<body, written exactly as a commit message>

<task metadata required by the repo's policy>

fold <sha>
```

A `fold` line naming no commits takes every commit the others leave
— the whole branch when it is the only line
— so the one-commit plan names no shas at all.
A `fold` line with nothing written under it keeps the message of the first commit it folds, which is what a commit that already names its unit wants.
The messages here are what master carries:
nothing downstream composes one, so they follow the repo's commit conventions and carry the task metadata (step 5).

```bash
fold-branch --plan plan.txt --base <base>
```

It refuses a dirty worktree, refuses a plan that does not partition the branch, and proves the fold moved only commit boundaries before it writes anything.
Conflicts are the fold's own failure mode
— grouping reorders the branch, and hoisting one fold above another can collide;
it aborts, leaves the branch untouched, and names what collided.
Regroup so the colliding commits keep their relative order, or land them as one commit.
When a single commit straddles two landing commits
— it changed the feature and the unrelated thing together
— no grouping of whole commits is clean:
split it first (`git rebase -i`, `edit` on that commit), then plan over the commits you now have.

The fold is also where the token-accounting footers are aggregated onto the landed commits, so the accounting rides what lands.
Whatever the session spends after the last fold
— the approval wait, the landing turns
— is credited to nothing;
a branch that is final before review cannot account for the work that follows it.

### 9. The mandatory gate: the full test suite

The flow's one mandatory full pass
— on the final rebased tree, before the change reaches a reviewer.
It is mandatory *here* because a branch with no PR on it triggers no CI:
this is the only point in the flow where nothing else will run the suite.
Run the repo's full verification gate (its own docs name the command and any environment-specific flags).{{when #harness = bro}} Run it with an explicit large `timeout_seconds` (600 fits)
— `dev::bash`'s default kills a full suite mid-run;
same for any other long command.{{end}}

Locally is the default.
Where the repo's CI runs that same gate against a branch of its own
— a manual dispatch, a branch trigger
— pushing the branch (step 11) and waiting on that run counts as the pass, and is the better route when CI is faster or covers stages the workspace cannot run at all.
What waits for green is the PR, not the push:
a pushed branch has offered nothing to anyone.

A red gate blocks the PR.
Do not interpret or triage failures
— fix with new commits, or propose fixing pre-existing failures in this session or a separate one;
do not open a PR over failures.
Re-verify a fix with the narrowest evidence the repo offers (a change-scoped gate selection, the affected test files);
a second full pass buys nothing this one and the PR's own CI do not.

Earlier full passes (per checkpoint, pre-rebase) are optional and usually redundant
— this gate re-verifies the exact tree that ships.

### 10. Verify PR scope

```bash
git log origin/<base>..HEAD --oneline
```

Confirm the commit list matches the intended PR title and body.
If the worktree carries unrelated in-flight commits, either:
- Split the branch (move unrelated commits to a separate branch), or
- Rewrite the planned PR title/body to cover the full set, or
- Ask the user.

Do not silently open a PR whose scope is wider than its title says.

### 11. Push the branch

```bash
git push -u origin HEAD
```

A branch already on the remote needs `git push --force-with-lease origin HEAD` instead
— the fold rewrote it.
A gate that ran on CI (step 9) already pushed this branch, so the command is a no-op there.

### 12. Open the PR

Use `gh` for everything GitHub-related
— it's pre-authenticated, auto-detects the repo from the origin remote, and handles JSON encoding.
Do not use `curl` against `api.github.com`.

Build the PR title and body:
- **Title**:
  if the branch lands as one commit, use its title.
  If several, a brief summary in the same style.
- **Body**:
  review context, not commit text
  — the landed commits carry the change's own story.
  A `Task:` line linking the task URL (if a task id is known), a `## Test plan` checklist of what you verified with each box ticked, and whatever else the reviewer needs that the commits do not carry.
  Don't list a step this session cannot run
  — the repo's own CI gate runs on the PR, and an unticked box blocks [[land]] later with nobody able to clear it.

```bash
gh pr create --base <base> --title "<title>" --body "$(cat <<'EOF'
Task: https://tracker.example.com/...

## Test plan
- [ ] ...
EOF
)"
```

Report the PR to the user as a **review link**:
a markdown hyperlink titled `#<n>` (the PR number) whose target is the review section
— the PR URL with `/files` appended (the Files changed tab, where the review UI lives)
— not the main PR page.
`gh pr create` prints the PR URL;
`<n>` is its last path segment.

```markdown
[#<n>](https://github.com/<owner>/<repo>/pull/<n>/files)
```

Surface this link every time the PR enters review-pending:
here at creation, and again after each push of review-fix commits (step 15).

### 13. Log "PR opened" to the task (sessions with a task)

If the session has a task (`RIDE_TASK_ID` — `dive-in` sets it — or a task resolved earlier in this session) and the brog tools are available, record the event with `brog::add_comment(task_id, topic='PR opened', body=...)`:

```
[PR:<n>](<pr-url>)
- [`<short-hash>`](<repo-url>/commit/<full-hash>) <commit title>
- [`<short-hash>`](<repo-url>/commit/<full-hash>) <commit title>
```

Build commit links from `git remote get-url origin` (strip trailing `.git`).

### 14. Launch the review watcher

The watcher is one long-lived `poll-pr` process.
It authenticates through the credential store (`--credential`, default `github`), re-resolved each cycle so the watch survives short-lived minted tokens;
your own comments are filtered via `--self`, which defaults to the PR author:

```bash
poll-pr <owner>/<repo> <pr_number>
```

`poll-pr` outputs JSON-lines to stdout:
- `{"event": "merged", "pr": N}`
  — PR was merged
- `{"event": "closed", "pr": N}`
  — PR was closed without merging
- `{"event": "conflicts", "pr": N}`
  — the PR became unmergeable into its base (GitHub's `mergeable` turned false, typically after something landed on the base).
  Fires once per conflicted episode
  — it re-arms only after the PR turns mergeable again.
- `{"event": "checks", "pr": N, "failing": [{"name": "...", "conclusion": "...", "url": "..."}]}`
  — a status check on the PR's head commit concluded as a failure.
  Fires once per red episode
  — it re-arms only after nothing is failing again (a re-run that goes green, or a new push).
- `{"event": "pushed", "pr": N, "head": "..."}`
  — the PR's head moved to a new commit.
- `{"event": "comment", "id": N, "user": "...", "body": "...", "path": "...", "url": "..."}`
  — new comment from a party to the review:
  the PR author, the repo owner, or anyone with a review on the PR (self filtered out — a reviewing session hears the author this way).
  Standalone inline review comments (replies to existing review threads) fire here;
  inline comments attached to a fresh review are bundled into the `review` event instead.
- `{"event": "review", "id": N, "user": "...", "state": "APPROVED|CHANGES_REQUESTED|COMMENTED|DISMISSED", "body": "...", "url": "...", "comments": [{"id": N, "path": "...", "line": N, "body": "...", "url": "..."}]}`
  — new review.
  `comments` is the array of inline comments attached to this review at the moment `poll-pr` saw it (typically all of them;
  rarely empty if the inline-comments endpoint lags the reviews endpoint — late arrivals then fire as standalone `comment` events on a later cycle).
- `{"event": "watch_failed", "pr": N, "source": "baseline|pull-request|checks|reviews", "reason": "...", "failing_for": N}`
  — terminal:
  the named event source kept failing past the grace window, so the watch ended rather than going quiet on those events
  — the process exits nonzero right after.
  A short blip costs nothing:
  one source failing is tolerated for `--failure-grace` seconds while the others keep reporting.
  `baseline` means the watch never started at all.

How to run it:

{{iff #harness = bro}}
Run it as a background job and read it iteratively
— a plain `dev::bash` call would kill it at its timeout:

1. `dev::job("poll-pr …")` → note the job id.
2. Loop on `dev::watch(job_id, wait_seconds=1500)`.
   Each return is one iteration:
   - new output → react to every JSON line per step 15, then watch again;
   - a bare `running` state line (quiet window) → watch again;
   - `exited` right after a `merged`/`closed` event → the PR is terminal;
     react per step 15, stop looping;
   - `exited` right after a `watch_failed` event → react per step 15, stop looping;
   - `exited` with no terminal event → the watcher died.
     Do not just restart it
     — a fresh `poll-pr` baselines all existing events as seen;
     reconcile first (re-entry step 4), then start a new `dev::job`.
3. When chaining into [[land]], stop the watcher with `dev::kill(job_id)`.

The large `wait_seconds` keeps the run idling inside the tool call between events;
don't shorten it to poll
— every quiet return costs a full model round trip.

**The watch loop is the rest of the run.**
Your terminal answer comes only after the PR reaches a terminal state
— merged (typically via the [[land]] chain on APPROVED) or closed.
Until then, keep calling `dev::watch` iteration after iteration, however quiet the PR stays;
that idling is the run working as designed, not a stall to wrap up.
Do not kill the job and end the run with a "waiting for review" report
— an ended run watches nothing, and every later review event goes unhandled.
{{eliff #harness = claude}}
**MUST launch via the `Monitor` tool with `persistent: true`.
Do NOT use Bash `run_in_background`**
— that only notifies on process exit, so review/comment events sit silently in the output file and approvals never trigger the auto-chain.
The harness wakes you on each output event;
react per step 15.
Stop the watcher with `TaskStop` when chaining into [[land]].

If the `Monitor` schema needs a `ToolSearch` fetch, load `TaskStop` in the same query (`select:Monitor,TaskStop`)
— the APPROVED handler needs it and shouldn't spend a round trip on it later.
{{end}}
{{when #may_summon contains eyebro}}

### Hand the review to the eyebro

Don't wait for a review to arrive — hand it over:

1. Summon the eyebro detached, with the watcher already running
   — the watcher baselines existing events as seen at start, so a review posted before it starts would never fire.
   `bro::summon` with `target: eyebro`, `detach: true`, a `timeout` sized in hours (a review conversation outlives the default; `14400` fits), and a self-contained prompt naming the PR
   — the child shares no context with this session:
   `[[review pr <pr-url>]]`.
2. The review then flows through the PR:
   the eyebro's reviews and comments fire as ordinary step-15 events, your replies and pushed fixes reach it the same way, and its approval chains into [[land]] like any other.
   Don't wait on the summon result
   — check the request id (`bro::summon_check`) only when the PR stays quiet past reason.
3. A summon denied at launch, or a child that raises right away
   — typically because its GitHub identity is the PR author's own, which GitHub refuses to let approve
   — stops nothing:
   report the reason and continue under human review.

{{end}}

### 15. React to review events

**`comment` event** or **`review` with `state: "CHANGES_REQUESTED"` or non-empty `comments`**:

A non-empty `comments` array on an APPROVED review counts as actionable feedback too
— the reviewer may be saying "ship after fix" via inline nits even when the top-level review body is empty.
Read every comment in the array before chaining.

Handle pending feedback as one batch:
address every comment that has arrived, then pay one verification pass and one push for the batch
— not a suite run per comment.

1. Read and understand the feedback (review body + every `comments[]` entry).
2. Make the requested code changes locally.
3. Re-run the pre-commit gates (step 2).
4. Commit (a **new** commit, not `--amend`) with the same conventions as step 5–6.
5. Verify with the narrowest evidence the repo offers
   — a change-scoped gate selection, or the affected test files.
   **Not the full suite:**
   the push in step 7 puts the branch back through the PR's own CI, which runs the whole gate, and the merge is blocked on it
   — a full local pass here duplicates a run that is about to happen anyway and that nothing can land without.
6. Re-fold (step 8):
   the branch has to be back in landing shape before it is pushed, or the approval that follows would cover commits that are not the ones landing.
   Write the plan over the branch as it stands now
   — the last fold rewrote the shas
   — and let a `fold` line naming no commits absorb what this round added, or name the round's commit in the fold whose unit it serves.
   The re-approval this costs is the point:
   the commits genuinely changed.
7. Push:
   `git push --force-with-lease origin HEAD`
   — the fold rewrote the branch.
8. Reply on the PR confirming the fix (reference the commit SHA).
   Never place Markdown containing backticks or `$()` directly inside a double-quoted shell argument.
   ```bash
   read -r -d '' body <<'EOF' || true
   <markdown reply>
   EOF
   gh pr comment <n> --body "$body"
   # or:
   gh api -X POST repos/<owner>/<repo>/pulls/<n>/comments/<comment_id>/replies -f body="$body"
   ```
9. Surface the review link again (step 12's `[#<n>](<pr-url>/files)` format)
   — the pushed commits put the PR back into review-pending.

**`review` with `state: "APPROVED"` and empty `comments`**:

Unconditional approval — the PR is ready to merge.
Chain into the merge, and batch it:
stop the watcher ({{iff #harness = bro}}`dev::kill(job_id)`{{else}}`TaskStop`{{end}}) and [[land]] **in the same response**, then follow it (it reads the branch to decide what master should carry, then merges with a single `land-pr` command).

**`review` with `state: "COMMENTED"` or `"DISMISSED"`**:
informational;
the actionable feedback (if any) is in this event's `comments` array or arrives via accompanying `comment` events.

**`checks` event**:
CI went red on what you pushed.
Fetch the failing run's log (`gh run view --log-failed <run-id>`, the id is the tail of the event's `url`) and diagnose it as your own breakage
— a failure the local gate missed is the interesting kind (environment-dependent, ordering-dependent, or a file you forgot to stage).
Fix it exactly like review feedback:
gates (step 2), a new commit (steps 5–6), step 15's narrow verification, the re-fold (step 8), a force-with-lease push.
Report the failure and your fix to the user;
never wait for it to disappear on a re-run you didn't trigger, and never land around it
— `land-pr` refuses a failed check anyway.

**`pushed` event**:
usually your own push of review fixes echoing back — nothing to do.
One you didn't cause means someone else pushed to the PR branch (typically the user amending it directly):
`git fetch origin` and reset your local branch onto the pushed head before your next commit
— continuing from the stale head would discard their commits on your next force-with-lease push.

**`conflicts` event**:
rerun step 7 — the rebase, the in-band resolution default, the escalation bar, and the task comment all apply unchanged
— then the full gate (step 9), not step 15's narrow pass:
what may have broken the branch is what landed on the base, which no diff of the branch's own changes points at.
Then push the rebased branch:
`git push --force-with-lease origin HEAD`
— the PR branch, never the base.

**`merged` / `closed`**:
someone (the user, a [[land]] run, or external action) terminated the PR.
If `merged`, run [[land]]'s post-merge bookkeeping
— the `merged` comment and task closure.
If `closed` without merge, log it and report to the user.

**`watch_failed` event**:
the watch is over and the PR is not
— from here on nothing on the PR reaches you.
The `reason` is typically a credential the watch cannot use for that source (`HTTP 403` / `HTTP 404` on `checks` means the token lacks `checks: read`) or GitHub being unreachable for minutes.
Do not restart the watcher on the same credential and hope:
reconcile the review state from `gh` (re-entry step 4) and handle whatever arrived, then report the failing source and reason to the user
— stop and ask when questions reach the user;
when unattended, `raise` with the source and reason as the reason.

## Rescue committed work before a raise

{{include fragments/rescue_before_raise.md}}

## Safety rules

- Never commit credential material
  — credential stores, key files, minting configs.
  If anything that looks like a secret shows up in `git status`, warn the user before staging.
- Never skip hooks (`--no-verify`, `--no-gpg-sign`, etc.).
- Never force-push (`--force`, `--force-with-lease`) to master.
- Never stage with `git add -A` / `git add .`
  — stage by explicit path.
- Never `git add -N`
  — breaks `git stash create`.
- For verify-test-catches-bug, use `git stash push <file>`, never bare `git stash`.
