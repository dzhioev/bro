---
name: plan
description:

Review a commissioned design and plan its implementation
— "[[plan <root-task-url>]]", the review-and-plan phase a coordinating session hands over for multi-phase work.
Reads the design file the root task's `## Design` links with fresh eyes, verifies its assumptions against the real system, settles material objections and revisions with the user through [[design]], finalizes the file on its kept branch, then splits the implementation into stage tasks grouped into landings, pushes the first landing's integration branch, and writes `## Implementation plan` with any `### Rollout`.
Implements nothing and changes no task status.

parameters: {"task": "ref of the root task whose design to review and plan", "delivery?": "coordinator roles and gates the coordinator chose for the stages, beyond the stage default"}
version: 1.0.0
---

# plan

Review the design of multi-phase work, then plan its implementation into stages and landings.
Read `task` and `delivery` from the appended `# Arguments`.
A coordinating session commissioned this phase and owns the root task:
the root task page is what later phases read, so everything durable lands there or on the design branch, and your answer carries what the coordinator has to act on.

## 1. Review the design

Read the whole root task, then the design file its `## Design` links, with fresh eyes:
find the problems and the improvements,
and verify the design's assumptions wherever you can reach them
— introspect the real schemas,
APIs,
and call sites instead of leaving them as open questions.
For every contract shared with a separately deployed process, state the rollout order and how mixed versions remain operable.

Bring material objections and open design decisions to the user before editing the design.
Use [[design]] for agreed revisions, without restarting settled decisions;
revise the file on the design branch and push, never copying the specification back to the page.{{when #may_summon contains eyebro}}

The design's pull request is the design review.
Watch it with `poll-pr` using [[follow pr]]'s trust-file and replay procedure, armed before the reviewer is summoned.
Reconcile the whole replay before acting;
this is document review only, with no code folds or landing.
[[Ask <the eyebro your banner's may_summon grants>]] to review that pull request as a document, not code:
gaps, contradictions, unverified assumptions, a missing rollout order, and better alternatives, not tests or CI.
Fold its findings into the file until it approves, settling with the user in this session whatever needs their opinion;
the design is settled when both the eyebro and the user have approved the pull request on GitHub.{{end}}

Finalize the design in the file and update the summary under `## Design` (`brog::edit_description`, leaving the other sections intact).
Then close the pull request without merging and keep its branch:
the file on the branch is the specification that stages and verification read, and the pull request is only its review.

## 2. Plan the stages

Plan the implementation of the design you just finalized and split it into stages
— one stage if the work is small.
Group the stages into landings, each a merge to the default branch through an integration branch of its own:
one landing, unless the rollout needs steps between those merges
— deploying what accepts both shapes before anything writes the new one, say.
Name every landing's integration branch after the root task, `integration/<root-task-id>-<short-slug>`, never after the workspace:
the prefix is what lets a repository write branch rules over the branches stages merge into, so the name is part of the contract.

For EACH stage call `brog::create_task` to open a stage task whose body carries:

- the stage's goal and details;
- "part of the multi-phase work tracked at [`<root-task-name>`](`<root-task-url>`) — read its `## Design`, including the design file it links on the kept branch, and `## Design changelog` before starting";
- "land via [[run pr]] with its base argument set to `origin/<integration branch>`, reviewed on github by a reviewer you summon, with gates verdict and checks where CI covers its landing method, otherwise verdict and tests
  — the human approval comes at the integration landing";
- "when the PR merges, mark this task done — do not hold it for a rollout, the coordinator rolls the work out after its landing merges";
- "if you change a design decision mid-build, append it to the root task's `## Design changelog` for the history and the later stages".

The first stage of each later landing carries one more line, since its branch can only start from what the landing before it left on the default branch:
"first create `<its landing's integration branch>` on origin from the default branch, staying on your own workspace branch:
`base=$(default-branch) && git fetch origin "$base" && git push origin "origin/$base:refs/heads/<branch>"`, skipped where origin already has it".

Carry the coordinator roles and gates `delivery` names into the landing line of every stage it covers, explicitly, including `approval` for summoner approval.

Every end-to-end route the design names is owed automated coverage by the last stage that completes it, and that stage's task names the routes it owes;
the verification phase confirms the shipped result against the real system and is never where a route runs for the first time.

## 3. Publish the plan

Establish the first landing's integration branch from origin's default branch:

```bash
base=$(default-branch) && git fetch origin "$base" && git reset --hard "origin/$base" && git checkout -b integration/<root-task-id>-<short-slug> && git push -u origin HEAD
```

Then write `## Implementation plan` on the root task (`brog::append_description`):
the landings in order, each with its integration branch and its ordered stages linking their stage tasks,
and, when the work needs a rollout to go live, a `### Rollout` of ordered steps.
Each step carries its commands, the check that confirms it, its rollback, and the landing it follows,
and names the session that runs it:
the bro, its isolation, and the credentials it needs.
A step is the user's only where nothing but a human can do it, such as an approving review.

Do NOT implement code or open a PR other than the design's,
and do not change any task's status.

## 4. Answer

Answer with what you changed in the design and why,
the closed design pull request and its branch,
the landings with their integration branches and stages,
the rollout steps with the session each names,
and any prerequisite implementation or rollout will still need.
