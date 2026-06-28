---
name: land
description: This skill should be used when the user signals that an open PR should be merged into master — "land it", "land", "merge it", "merge the PR", "merge to master". Squash-merges the open PR for the current branch via `gh pr merge --squash --delete-branch`, reusing the original commit's subject and body and injecting an aggregated token footer (`claude-commit-footer --squash`) so the session spend survives the squash. Appends a `### Merged` entry to the task page and closes the task to Done unless the user explicitly said to keep it open. In `--auto` sessions, `/pr` chains into this skill automatically on APPROVED. Direct push to master (no PR) is a one-liner (`git fetch origin && git rebase origin/master && git push origin HEAD:master`) — not this skill.
version: 1.0.0
---

# /land

Merge an approved PR for the current branch into master. The terminal action of a dev session.

## Preconditions

- You are in a worktree on a non-master branch.
- A PR exists for the current branch (`gh pr view --json number` returns one).
- The PR's `reviewDecision` is `APPROVED`, OR the user has explicitly said to merge despite missing approval.
- **Test plan fully checked (auto-chain).** When `/land` is reached via the `--auto` APPROVED chain rather than a direct user "land it", the PR's `## Test plan` has no unchecked boxes (`- [ ]`). An unchecked box means you couldn't verify that item yourself — stop, surface the unchecked items, and wait for the user to verify them or say to land anyway.

If any precondition fails, stop and report — do not invent state.

## Workflow

### 1. Resolve the PR

```bash
gh pr view --json number,title,body,state,reviewDecision,baseRefName
```

`baseRefName` is the branch the PR merges into — `master` unless the PR was opened against another base. Step 2 scopes the footer range to it.

- If `state` is not `OPEN`, report (`MERGED` / `CLOSED`) and stop.
- If `reviewDecision` is not `APPROVED` and the user has not explicitly waived it, stop and surface:
  > PR not approved (state=<X>). Use `/pr` to continue review, or override with explicit "merge anyway".

### 2. Recover the commit message and aggregate the token footer

Use the PR's title and body as the squash subject and body. Pull from `gh pr view --json title,body`.

GitHub builds the squash commit server-side from the PR title + `--body`, so the per-commit token footers on the branch commits are discarded with those commits. To keep the session spend on the squash commit, generate an **aggregated** footer over the PR's commits and append it to the body:

```bash
./setup/claude_commit_footer.py --squash origin/<baseRefName>..HEAD
```

Use the `baseRefName` from step 1 (`master` unless the PR targets another base) — the range must be relative to the PR's actual base, or the footer would miscount. This emits the two-line footer summing every branch commit's per-model deltas (with the union of their session ids and Claude Code versions) plus this land session's own uncommitted work. Append its two lines to the PR body, below the `Task:` line. If it warns about footerless commits in the range, surface that — those commits' tokens are not captured.

If the worktree has multiple commits, the PR title/body should already reflect the full scope (step 12 of `/pr` enforced this).

### 3. Squash-merge

Merge with the recovered subject and the body that now carries the aggregated footer:

```bash
gh pr merge <n> --squash --delete-branch \
  --subject "<orig PR title>" \
  --body "<orig PR body, with the aggregated footer appended>"
```

`--delete-branch` removes the remote feature branch after merge.

### 4. Log "Merged" to the task (dive-in sessions only)

If this session was launched via `dive-in` (check `launch_command` from `cw banner --llm`):

```
### Merged — @YYYY-MM-DD HH:MM
<pr-url> merged to master
```

Use `date '+%Y-%m-%d %H:%M'` for the timestamp — do not invent it.

### 5. Close the task — conditionally

Don't close if either holds:

- **The change needs a deploy or migration to take effect.** If it touches code/config that runs in a deployed service (the ECS services / emails pipeline — see `infra/CLAUDE.md`) or adds a migration/backfill, the merge alone doesn't make it live. Leave the task in its current status and **propose** the rollout as a `call devoops "<what to deploy or run>"` command — don't run it yourself; the task closes only after the deploy succeeds.
- **The user said to keep it open.** Phrases in the initial prompt like "keep this Live", "leave open with notes", or "only landing a subset" mean the task stays in its current status; note it in your report.

Otherwise: `flow::update_task(task_id, status='Done')`.

### 6. Report to the user

One line: PR URL, "merged to master", and task status — closed-to-Done, left open per instruction, or left open pending deploy (in which case include the proposed `call devoops "…"` command).

## Safety rules

- Never bypass GitHub merge requirements (failing checks, missing approval) unless the user explicitly waived them in this session.
- Never `--admin` your way past branch protections.
- Never force-merge a PR with unresolved `CHANGES_REQUESTED` reviews.
- If the PR description has materially drifted from what shipped (the user committed new content between `/pr` and `/land`), surface it before merging so the squash body can be rewritten.
