# Rebase conflicts

What to do when `git rebase origin/<base>` stops on a conflict
— [[run pr]]'s step 7, and the `conflicts` review event that reruns it.

## Resolving

Resolve the conflicts yourself, in-band:
merge each conflict, `git add` the resolved paths, `git rebase --continue`.
Then, where the session has a task and the brog tools are available, record the resolution on it:
`brog::add_comment(task_id, topic='rebase conflicts', body=...)` naming the conflicted files and the resolution each one took
— [[run pr]]'s gate (step 9) verifies the resolved result next.

Escalate only when a resolution is not obvious
— the two sides carry contradicting logic or intent that no merged version can honor both of:
stop and ask when questions reach the user;
raise with the contradiction spelled out when unattended.
Never `--abort` or `--skip` silently.

In an unattended session there is no user to stop for:
when a conflict clears that escalation bar, rescue your commits per [[run pr]]'s "Rescue committed work before a raise" (abort the rebase to restore them, push the branch, name the ref), then `raise` with the contradiction as the reason
— the parked commits stay recoverable.

## The `conflicts` review event

The PR's watcher reports `conflicts` when something landed on the base that the PR no longer merges into.
Rerun [[run pr]]'s step 7 whole
— `git fetch origin <base> && git rebase origin/<base>`, since without the fetch the rebase runs against the base as it stood before that landing and changes nothing
— resolve as above, then push the rebased branch:
`git push --force-with-lease origin HEAD`
— the PR branch, never the base.
No local pass answers this one:
what may have broken the branch is what landed on the base, which no diff of the branch's own changes points at, so [[run pr]]'s step 9 selection is scoped to the wrong thing
— the PR's CI on the pushed head runs every stage, and the merge is blocked on it.
