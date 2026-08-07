Before you `raise` while the branch carries commits that aren't on any remote — a rebase conflict you're parking, any blocker you can't resolve unattended — push them first. An unsuccessful session's workspace survives on disk, but a retry runs in a fresh workspace and can't reach it; a pushed ref is what the retry recovers from.

1. If you're mid-rebase (a conflict you're about to raise on), `git rebase --abort` first — it restores the branch to your committed work.
2. Push the branch as-is: `git push -u origin HEAD`. The worktree branch is already uniquely named for this session, so it lands a recoverable ref; if that push is rejected, push a fresh `rescue/<name>` branch instead (`<name>` is the session `name` from `bro::banner`).
3. Name the pushed ref in the `raise` reason — it is the failure record a retry reads first.
4. Record it on the task: `brog::add_comment(<task-id>, topic='rescue', body='raised on <blocker>; commits pushed to <ref> — fetch and cherry-pick, do not redo')`.

Skip this only when there is nothing to lose — no local commits, or the branch is already fully pushed.
