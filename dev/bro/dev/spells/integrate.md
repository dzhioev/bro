---
name: integrate
description:

Land one landing of multi-phase work on the default branch
— "[[integrate <integration-branch> for <root-task-url>]]", the integration phase a coordinating session hands over once every stage of the landing has merged into its integration branch.
Rebases the integration branch onto the default branch, opens one pull request for all the landing's stages from a branch of its own through [[run pr]] with human approval, records it on the root task, follows it through the user's approving review and lands it with [[land]].
Rolls nothing out and leaves the root task open.

parameters: {"task": "ref of the root task the landing belongs to", "branch": "the landing's integration branch, which the workspace starts on"}
version: 1.0.0
---

# integrate

Take one landing's stages from its integration branch to the default branch as a single pull request.
Read `task` and `branch` from the appended `# Arguments`.
A coordinating session commissioned this phase and owns the root task;
your answer carries what it has to act on.

## 1. Branch the pull request

Sync `branch` against origin and rebase it onto `origin/$(default-branch)`.
Then take the rebased result onto a branch of its own before doing anything else:

```bash
git checkout -b integration-pr/<the integration branch's own slug>
```

The pull request goes up from THAT branch;
`branch` is never pushed again.
A repository that gates the branches stages merge into refuses every direct push to them, the rebase's included, so a phase that pushed the integration branch would be left with nothing to open a pull request from;
and where nothing gates them, what stages merge into still should not be what a merge to the default branch rewrites.

## 2. Land it

Open ONE pull request for all the landing's stages together with [[run pr]], its base argument `origin/<default branch>` and its default human-approval gate retained, and land it with [[land]].
Its body names the stage pull requests and the integration branch their commits landed on
— the root task and its stage tasks carry them
— so the reviewer reconciles those approvals instead of re-reading the stages.
Record the pull request on the root task as soon as it is open (`brog::add_comment`, topic `integration pr`), so the page carries the one that took the stages to the default branch.

Where the default branch is protected, the approving review that clears the merge has to come from the user:
an agent's own approval does not satisfy the base's rule, so a land refused for want of a review is the expected state there rather than a blocker.
Keep [[follow pr <integration-pr>]] running through it, restarting that watch if needed, and land when the user's approval arrives.

Keep the root task open whatever happens
— the coordinating session closes it after verification.
Roll nothing out, even where [[land]] would hand a deploy off:
the coordinating session runs the rollout as a phase of its own.

## 3. Answer

Answer with the merged pull request and the landed commit.
