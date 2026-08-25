---
name: deploy
description:

Use this spell when the user asks to deploy one or more repository services.
It resolves targets through the repository's operations registry,
checks out an explicitly requested ref safely,
runs each `infra::deploy` followed by its declared `infra::verify`,
and treats dry-run as an explicit opt-in.

version: 1.0.0
---

# Deploy

Call `infra::list_targets` first.
Its response is the only target roster:
do not rely on names, paths, service coordinates, or verification behavior remembered from another repository.

## Ref selection

When the user names a branch, tag, or commit, use shell git before deploying:

```bash
git fetch --all --prune
git status --porcelain
git checkout <ref>
git merge --ff-only origin/<ref>
```

Run the fast-forward only for a branch.
Stop on a dirty tree unless the user explicitly authorized replacing or ignoring its changes.
Stop when the ref cannot be resolved or checked out.

When no ref is named, deploy the current checkout.
A target's deploy command may require HEAD to be reachable from a remote branch;
report that command's failure rather than trying to bypass it.

## Target selection

Use explicit target names from the request when present.
For “deploy everything,” use every target in registry order.
For “deploy what changed,” read these sets with shell git:

```bash
git diff --name-only origin/master...HEAD
git diff --name-only
git ls-files --others --exclude-standard
```

Match each changed path against the target's declared `paths` prefixes.
A path may select multiple targets.
If no target matches, report the known targets and stop rather than guessing.

## Execution

Run targets sequentially in registry order.
For each target:

1. Call `infra::deploy(target, dry_run)`.
2. Stop on a non-zero exit code and report the captured output tail.
3. When this is a real deploy and the target declares verification, call `infra::verify(target)`.
4. Stop when verification returns `ok: false` or a non-zero command result.

Do not call `infra::restart` after a deploy.
Restart forces another rollout and is reserved for state changes that the deployed artifact does not carry.

Set `dry_run=false` unless the user explicitly asks for a dry run, command preview, or no changes.
A dry run executes no deploy and therefore skips verification.

For a quiet ECS rollout, call `infra::ecs_status(target)` no more than once per minute.
Report deployment counts and the latest event without polling tightly.

## Result

Report each target, whether it was a dry run or real deploy, the deploy exit code, and the verification outcome.
Do not continue to later targets after a failure.
