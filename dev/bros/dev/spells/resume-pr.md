---
name: resume-pr
description:

This spell should be used when a pull request is already open and nothing is watching its review
— "resume PR <pr-url-or-number>", "resume the PR", "pick up the review"
— after the session that opened it died, or when [[run pr]]'s `poll-pr` watcher exited without a terminal event or failed.
Restores the PR's branch and context, reconciles the review feedback that arrived while nothing watched, and rejoins [[run pr]] at its review watcher.

parameters: {"pr": "the open pull request's URL or number"}
version: 1.0.0
---

# resume-pr

Pick up an open pull request whose review nothing is watching, reconcile what happened meanwhile, and rejoin [[run pr]] at its review watcher.
`<pr>` below is the `pr` value from the `# Arguments` section appended by the spell tool.

A restarted `poll-pr` baselines every existing event as already seen, so feedback left unhandled when the watch stopped would be skipped forever:
reconcile from `gh` state before watching again, and never trust a lost watcher.

## 1. Check out the PR's head branch

Skip this step when the session already sits on the PR's head branch
— a watcher that stopped under a live session.

`gh pr checkout <pr>`.
A fresh clone sits on a `workspace-<name>` branch at the base ref
— the PR's head branch is not checked out locally;
`gh pr checkout` fetches it and sets up tracking so later pushes go to the right branch.

## 2. Recover the context

Skip this step too when the session already holds the PR's task and base.

```bash
gh pr view <pr> --json number,url,state,baseRefName,title,body
```

- The task link is the `Task:` line in the PR body
  — use it for [[run pr]]'s task logging and [[land]]'s bookkeeping.
  No `Task:` line → proceed without task logging.
- `<base>` is `baseRefName`.

## 3. Handle a terminal PR

`state: MERGED` → run [[land]]'s post-merge bookkeeping (`merged` comment, task closure) and stop;
`state: CLOSED` → report it and stop.

## 4. Reconcile unaddressed feedback

The lost session or watcher may have stopped before, during, or after handling any event.
Pull the full review state:

```bash
gh pr view <pr> --json reviews,comments,reviewDecision
gh api repos/<owner>/<repo>/pulls/<number>/comments   # inline review comments
```

Treat as actionable any repo-owner feedback per [[run pr]]'s step 15 rules that has no later reply from the PR author and no later commit addressing it;
handle each per that step.
If the latest owner review is APPROVED, nothing actionable is pending, and `reviewDecision` is `APPROVED` or `null`, retain that cleared review state against the current head and continue to the watcher.
Where that review's `commit` is the current head, retain it as step 15's owner's approval too:
the restarted watcher baselines it as seen.
Its first green edge clears the checks gate before landing;
watcher silence does not.{{when #may_summon contains eyebro}}
A reviewer's verdict does not survive the session that summoned it, and the PR is no substitute:
the quest id `bro::quest_check` needs died with that session, and an approval sitting on the PR says a review approved, not that the reviewer you delegated to did
— on a public repository any account can leave one.
Where that session is gone, summon a reviewer again and read the answer off that summon.
A child that finds the head already approved reconciles it as a completed review and returns saying so without posting again, which is both the verdict you need and the reason waiting on the watcher here would wait forever.{{end}}
Any other `reviewDecision` is step 15's base gate reached with no event to carry it, and step 15's answers apply:
a `REVIEW_REQUIRED` leaves the PR owed a review its base counts, and a `CHANGES_REQUESTED` leaves a standing review asking for work
— resume watching and settle it rather than landing.

## 5. A failed watch

When the watch ended on a `watch_failed` event, nothing on the PR reached the session from then on, and starting it again on the same credential only fails the same way.
Its `reason` is typically a credential the watch cannot use for that source (`HTTP 403` / `HTTP 404` on `checks` means the token lacks `checks: read`) or GitHub being unreachable for minutes.
Once step 4 has handled whatever arrived, report the failing source and reason to the user
— stop and ask when questions reach the user;
when unattended, `raise` with the source and reason as the reason.

## 6. Resume watching

Continue with [[run pr]] on `<base>` from its step 14:
start the watch anew and react to every line per its step 15.
