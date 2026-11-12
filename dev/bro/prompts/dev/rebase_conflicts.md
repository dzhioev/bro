# Rebase conflicts

What to do when `git rebase <base>` stops on a conflict
— [[run pr]]'s "Rebase onto `<base>`", and [[follow pr]]'s `conflicts` event.
`<base>` is the resolved ref as given: a remote's branch or a local branch, never a name with an implied origin prefix.

## Resolving

Resolve the conflicts yourself, in-band:
merge each conflict, `git add` the resolved paths, `git rebase --continue`.
Then, where the session has a task and the brog tools are available, record the resolution on it:
`brog::add_comment(task_id, topic='rebase conflicts', body=...)` naming the conflicted files and the resolution each one took
— [[run pr]]'s "Pre-review check" verifies the prepared result.

Escalate only when a resolution is not obvious
— the two sides carry contradicting logic or intent that no merged version can honor both of:
stop and ask when questions reach the user;
raise with the contradiction spelled out when unattended.
Never `--abort` or `--skip` silently.

When a conflict clears that escalation bar unattended, retain committed work per [[run pr]]'s "Rescue committed work before a raise", then raise with the contradiction and rescue ref or artifact
— a retry must be able to recover the parked commits.
A delegated [[land]] instead aborts a conflicting landing rebase and returns it to the author for review; it does not resolve and land unreviewed code.

## The `conflicts` review event

The PR watch reports `conflicts` when the PR no longer merges into its base.
At the author's turn, rerun [[run pr]]'s "Rebase onto `<base>`":
fetch the base's remote where it has one, then `git rebase <base>`; a local base needs no fetch.
Without the fetch, a rebase onto a stale remote-tracking ref may change nothing.
Resolve as above, then push the rewritten **PR head branch** with `--force-with-lease` to its head destination, never the base.

Before a verdict, the same live reviewer judges the new head and conflict resolution as another round.
After a verdict, preserve verdict and approval only if the complete one-to-one range-diff shows every commit unchanged (`=`); checks must rerun on the new head, and tests where its tree changed.
A resolution or CI fix needs [[run pr]]'s "Failures and successors" delta review, then its landing fold and fresh gates.
For CI-covered changes, the pushed head's whole CI gate covers integration breakage that a branch-only local selection may miss; a selected `tests` gate also runs whole on the final head.
