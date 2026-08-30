---
name: deploy
description:

Use this spell when the user asks to deploy one or more repository services.
It resolves targets through the repository's operations registry,
checks out an explicitly requested ref safely,
runs each target's declared plan before any live resource changes,
then its deploy command as a backgrounded shell job followed by its declared `infra::verify`,
and treats dry-run as an explicit opt-in that stops after the plan.

version: 3.0.0
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

A deploy runs for minutes, so it runs as a backgrounded shell job rather than a tool call:
a tool call the harness stops waiting for is reported as failed while its command keeps running,
which invites a second deploy on top of a live rollout.
The backgrounded job reports its own exit code when it finishes.
A plan deploys nothing and repeating one is harmless, so it stays a tool call.
It is not free of account writes, though:
a CDK plan creates and deletes a change set, and a temporary stack when the target's own does not exist yet.

Run targets sequentially in registry order.
For each target:

1. Take the target's `deploy` command and its `plan` entry from the `infra::list_targets` roster.
   The deploy command is repository-relative; run it from the repository root.
2. When the target declares a plan, call `infra::plan(target)`.
   `outcome: unsafe` names live resources the plan cannot certify survive the deploy:
   report those lines and stop, and deploy past them only when the user says so explicitly.
   `outcome: failed` means the plan never ran to a verdict, so nothing was checked:
   report it and stop, with nothing to override and no deploy failure to retry.
   A target declaring no plan has no gate at all:
   report that and stop, and deploy it only when the user says so explicitly.
3. For a dry run, report the deploy command and go to the next target without running it.
4. Start the deploy command as a backgrounded shell job, capturing stdout and stderr to a log file.
5. Wait for the job to exit rather than polling it.
   Never start a second deploy for a target whose job has not exited.
6. Stop on a non-zero exit code and report the log's tail.
7. When the target declares verification, call `infra::verify(target)`.
8. Stop when verification returns `ok: false` or a non-zero command result.

Do not call `infra::restart` after a deploy.
Restart forces another rollout and is reserved for state changes that the deployed artifact does not carry.

Set dry run only when the user explicitly asks for a dry run, command preview, or no changes.
A dry run stops after the plan, so it deploys nothing and skips verification.
Do not report it as writing nothing at all, since the plan itself reaches the account.

For a quiet ECS rollout, call `infra::ecs_status(target)` no more than once per minute.
Report deployment counts and the latest event without polling tightly.

## Result

Report each target, whether it was a dry run or real deploy, the plan outcome, the deploy exit code, and the verification outcome.
Do not continue to later targets after a failure.
