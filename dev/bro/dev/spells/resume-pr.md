---
name: resume-pr
description:

Resume at review a delivery whose author session was lost
— "resume PR <url-or-number>", a PR watch that failed or died, or a coordinator routing a failed landing back to a new author.
Recovers the task from the PR body's Task: line, the delivery plan from its Delivery: line and the base from the PR itself, checks out the PR head, then casts [[run pr]] under the recorded plan and enters at its review handoff with a new reviewer, through [[follow pr]]'s replay-safe watch.
A merged or closed PR gets bookkeeping or a report instead.

parameters: {"pr": "pull request URL or number to resume"}
version: 1.0.0
---

# resume-pr

Resume the delivery the PR records, not a new one with defaults.
Read `pr` from the appended `# Arguments`; batch independent reads.

## 1. Read the PR and its plan

Resolve the PR repository from its URL or, for a number, the checkout, and bind every `gh` call to it; read its metadata:

```bash
gh pr view <pr> --repo <owner>/<repo> --json number,url,state,headRefName,headRefOid,headRepository,headRepositoryOwner,baseRefName,title,body
gh api repos/<owner>/<repo>/pulls/<n> --jq .base.repo.clone_url
```

Read feedback only through trusted `pr-state` or [[follow pr]].
Recover the task from the body's `Task:` line, and the whole plan from the PR as the delivery reference's "Record the plan" recovers one (`man-source::read`, topic `delivery`).
An explicit plan change from the user or the caller goes through the reference's plan check and updates the body's `Delivery:` line.

## 2. Stop at a terminal PR

A merged PR gets [[land]]'s "Bookkeeping and report" only; a closed PR gets a report.
Either stops here, with no second landing or checkout.

## 3. Take the head

Check out the PR's head branch unless already on it, with `gh pr checkout <pr> --repo <owner>/<repo>`, without discarding uncommitted work.
Confirm its SHA matches the PR and recover its push destination, including a fork's head repository: every later push goes there.

## 4. Enter run-pr at review

Cast [[run pr]] with the recovered plan as its arguments, so it renders the recorded channel and landing.
Run its "Delivery plan" over that plan, then continue at its "Review handoff":
the branch is prepared and the PR published, so its branch preparation and publishing are skipped.
[[Follow pr]] reconciles the whole replay before any round or gate is acted on.

A reviewer's verdict does not survive the session that owned it, so start the reviewer again:
on github it reconstructs earlier rounds from trusted PR history;
talk and local start afresh on the current head unless this live session retains a predecessor verdict commissioning delta review.
