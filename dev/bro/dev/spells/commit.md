---
name: commit
description:

Commit work in progress as conventional, policy-audited commits
— "[[commit]]", "commit this", "checkpoint the work", or a review fix appended as a fixup of the landing commit it serves.
Formats the tree, groups the uncommitted work by concern with its docs, audits each commit's diff against the development style policy, writes the message in the repository's conventions with its task metadata, and stages by path.

parameters: {"fixup?": "the landing commit a review fix serves; the fix commits as its --fixup"}
version: 1.0.0
---

# commit

Commit the uncommitted work as conventional commits, one logical unit each.

## Format and group

Run the repository's formatter and include its changes with their logical units.
Read `git status` (no `-uall`) and the staged and unstaged diffs in parallel.
Sync the repository's docs wherever architecture, modules, commands, spells or behavior changed, bundling them with the concern they document.
Group the work by concern, not file: a feature with its docs, a rename with its consumers, unrelated fixes apart.

## Audit each commit

Immediately before **each commit**, call `dev-style-source::read` and the `man-source::read` guides for every area it reaches, then audit that commit's diff against the returned policy text.
State a visible one-line `clean` verdict, or the violations found and fixed, before committing.

## Write the message

Follow the repository's commit conventions, or `git log --oneline -10` where none exist.
Resolve the task's URL through `brog::get_task` where a task exists, and add the repository's required task metadata.
Do not invent task metadata without a task.
Never hand-write, edit or strip generated trailers, co-author lines or token footers; the hooks own them.

With `fixup`, the fix commits as `git commit --fixup=<fixup>`, its `-m` body only the repository's required task metadata:
the fold that groups it into the commit it serves finalizes the message there.
Never amend the served commit.

## Stage and commit

Stage **specific paths**, never `git add -A`, `git add .` or `git add -N` (intent-to-add breaks stash-based tooling).
Never stage credentials, key files or minting configs; surface suspected secrets first.
Use a quoted HEREDOC to preserve the message:

```bash
git add path/to/file1 path/to/file2
git commit -m "$(cat <<'EOF'
<imperative subject in the repo's style>

<why and shape where needed>

<task metadata required by the repo>
EOF
)"
```

If a hook fails, fix, re-stage and create a new commit, never `--amend` (that failed commit did not happen) or `--no-verify`.
To verify a test catches a bug by reverting the fix, use `git stash push <fix-file>`, never bare stash which also hides the test.
