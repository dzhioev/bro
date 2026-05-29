---
name: land
description: This skill should be used when the user signals that an open PR should be merged into master — "land it", "land", "merge it", "merge the PR", "merge to master". Squash-merges the open PR for the current branch via `gh pr merge --squash --delete-branch`, reusing the original commit's subject and body. Appends a `### Merged` entry to the task page and closes the task to Done unless the user explicitly said to keep it open. In `--auto` sessions, `/pr` chains into this skill automatically on APPROVED. Direct push to master (no PR) is a one-liner — see `bro/bros/ppp_dev.py` "Land" step — not this skill.
version: 1.0.0
---

# /land

Merge an approved PR for the current branch into master. The terminal action of a dev session.

## Preconditions

- You are in a worktree on a non-master branch.
- A PR exists for the current branch (`gh pr view --json number` returns one).
- The PR's `reviewDecision` is `APPROVED`, OR the user has explicitly said to merge despite missing approval.

If any precondition fails, stop and report — do not invent state.

## Workflow

### 1. Resolve the PR

```bash
gh pr view --json number,title,body,state,reviewDecision
```

- If `state` is not `OPEN`, report (`MERGED` / `CLOSED`) and stop.
- If `reviewDecision` is not `APPROVED` and the user has not explicitly waived it, stop and surface:
  > PR not approved (state=<X>). Use `/pr` to continue review, or override with explicit "merge anyway".

### 2. Recover the original commit message

Use the PR's title and body as the squash subject and body. Pull from `gh pr view --json title,body`. This preserves the commit footer (`Task:` + Claude Code footer) on the merge commit.

If the worktree has multiple commits, the PR title/body should already reflect the full scope (step 12 of `/pr` enforced this).

### 3. Squash-merge

```bash
gh pr merge <n> --squash --delete-branch \
  --subject "<orig PR title>" \
  --body "<orig PR body>"
```

`--delete-branch` removes the remote feature branch after merge.

### 4. Log "Merged" to the task (dive-in sessions only)

If this session was launched via `dive-in` (check `PPP_SHELL_COMMAND`):

```
### Merged — @YYYY-MM-DD HH:MM
<pr-url> merged to master
```

Use `date '+%Y-%m-%d %H:%M'` for the timestamp — do not invent it.

### 5. Close the task — conditionally

Check the session's initial prompt for an explicit instruction to keep the task open. Phrases like "keep this Live", "leave open with notes", "only landing a subset", or similar mean the user wants the task to stay in its current status after merge.

- **If no such instruction**: `update_task(task_id, status='Done')`.
- **If the user said to keep it open**: skip the status update. Mention in your final report that the task was left in its current status per the user's instruction.

### 6. Report to the user

One line: PR URL, "merged to master", and task status (closed-to-Done or left-Live-per-instruction).

## Safety rules

- Never bypass GitHub merge requirements (failing checks, missing approval) unless the user explicitly waived them in this session.
- Never `--admin` your way past branch protections.
- Never force-merge a PR with unresolved `CHANGES_REQUESTED` reviews.
- If the PR description has materially drifted from what shipped (the user committed new content between `/pr` and `/land`), surface it before merging so the squash body can be rewritten.
