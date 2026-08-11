---
name: run-feature
description: This script should be used when the user wants a large piece of work driven end to end from a coordinator session — "start a feature", "kick off the <X> feature", "let's design and build <big thing>", "run the feature workflow", "orchestrate this", "resume the feature at <url>". This session becomes the coordinator: it opens a feature task as the single source of truth, then walks the work through design, design review, planning, per-stage implementation, integration, and verification, running every phase as a summoned worker in its own isolated container and recording each outcome on the feature page before starting the next. It never designs or implements itself. For work that fits one session this is overkill — summon a single worker on the task (`@::ask`) and let it run `@::fix` itself.
parameters: {"feature?": "ref of an existing feature task to resume", "new?": "seed text for a new feature"}
version: 1.0.0
---

# run-feature

Coordinate a feature across many short-lived worker sessions. You are the root: you never design, plan, or write code. You own the feature task page, and each phase is a summoned bro that does the work in its own container, writes its artifact to the page, and answers you.

## Operating principle — the root does no hard work

- **Keep context sparse.** Do not read the codebase, draft designs, or implement. Every unit of real work happens in a worker you summon.
- **The feature page is the single source of truth.** Each turn, read it and the statuses of the stage tasks it links to recover where the feature stands, then act.
- **One phase at a time.** Summon a phase, wait for its answer, record the outcome, review the artifact, then summon the next. A phase's answer is the only thing that returns from it — everything durable must land on the page.
- **You are the human's interface.** Workers run isolated with no human channel: a worker that cannot proceed raises with a reason instead of asking. Questions, corrections, and go/no-go between phases are yours to handle.

## Invocation forms

Read the appended `# Arguments` section:

- `new` — seed text for a feature to open (Step 0). An empty value means collect the seed from the user.
- `feature` — ref of an existing feature task: resume it.
- neither — ask the user which it is.

`feature` and `new` are mutually exclusive; if both appear, stop and ask the user to choose one.

**Resuming.** Read the description and comments (`brog::read_task` / `brog::read_comments`) plus each linked stage task's status, infer which phase the feature stands at, and continue from there. Never redo a completed phase.

## Step 0 — open the feature task

1. Discuss scope with the user only far enough to pin down a name and tags — you are framing the feature, not designing it. Names start with a lowercase letter (except proper nouns).
2. Settle the **worker bro** for the feature and its phases. Your summon allow-list is fixed at launch and you cannot introspect it, so ask the user which bro should do the work rather than discovering it by denial; `dev` is the default where nothing else is named. If a phase needs a different target than the rest (an operations bro for the rollout), settle that now too.
3. `brog::create_task` with the name, tags, and a `## Goal` body stating in a few lines what the feature must achieve. The task is born open. Its returned url is the feature URL every phase prompt carries.
4. Record the kickoff comment, naming the worker bro so a resumed session recovers it, then summon the design phase.

## The feature page

Sections accumulate on the description; workers write them with `brog::append_description` / `brog::edit_description`. Your own running record lives in the comment stream, not in a section.

- `## Goal` — written at creation.
- `## Design` — written by the design phase, finalized by the design review.
- `## Implementation plan` — written by the planning phase: the feature integration branch name plus the ordered stages, each linking its stage task.
- `## Design changelog` — appended by a stage when a design decision changes mid-build, so later stages and the history see it.
- `## Verification` — appended by the verification phase: what it exercised against the shipped result, and the outcome.

Record orchestration events as comments (`brog::add_comment(id, topic, body)`): a `kickoff` entry to open, then 1–3 lines after each phase whose topic names the outcome (`design done`, `stage 2 landed`). Recover per-stage progress from the stage tasks' own statuses, not from memory.

## Summoning a phase

The summon mechanics — client pick, relaying, and every failure mode — are `@::ask`'s; this script only says how a phase differs from a one-shot ask.

- **Always detached.** No phase is short enough for a blocking wait: send every one detached and poll for its result.
- **Size the timeout to the phase.** Stages and integration end in a PR review and idle on human latency, so `@::ask`'s open-ended-run exception applies to them — at the default they are killed mid-watch. The other phases finish on their own and take it.
- **Base ref (`into`).** Design, review, and planning inherit your workspace HEAD — pass nothing. Stages and integration pass the feature branch. Verification passes `master`, since the feature is on it by then.
- **Hold.** Leave the default. A worker with no human channel either delivers or raises with a reason you relay.
- **Scope.** A worker starts from its own credentials, not yours. Grant only what a phase needs beyond them and only what you hold yourself: a credential the phase must reach, or `@<bro>` when the phase has to hand work onward — the integration phase needs the operations bro granted when the feature requires a rollout to go live.
- **Self-contained prompts.** A worker shares no context with you: spell out the feature URL, what to produce, where to put it, and what not to touch. Ask for the open questions and unmet prerequisites in its *answer* rather than on the page — they are yours to act on, not the page's to carry.

Do not let this session end with a phase in flight; `@::ask` covers reclaiming a summon whose wait was lost.

## Phases

Between phases, review the artifact the worker produced before summoning the next one. A worker's report of a blocker, an unmet prerequisite, or a design change is yours to resolve — fold the fix into the next phase's prompt (an added grant, a corrected instruction) rather than reopening the finished one.

### 1 — design

> Design phase of a multi-phase feature coordinated by another session. Read the task at `<feature-url>` — its `## Goal` states what to design. Explore the codebase, work the design out, and stress-test it yourself: edge cases, risks, and the alternatives you rejected with the reason. Write the result to that task under `## Design` (`brog::append_description`). Do NOT implement code, open a PR, or change the task status. Answer with a summary of the design, every question you could not settle, and any prerequisite you could not satisfy from inside this container — a credential you would have needed, a step that needs the host.

Outcome: `## Design` on the page.

### 2 — design review

Summon a **fresh** worker — the value here is eyes that did not write the design — and raise its reasoning effort to the maximum the summon accepts.

> Design-review phase of a multi-phase feature coordinated by another session. Read the whole task at `<feature-url>`, then its `## Design` with fresh eyes: find the problems and the improvements, and verify the design's assumptions wherever you can reach them — introspect the real schemas, APIs, and call sites instead of leaving them as open questions. Then finalize the design in place (`brog::edit_description` on the `## Design` section, leaving the other sections intact). Do NOT implement, open a PR, or change the task status. Answer with what you changed and why, anything you would change but did not, and any prerequisite implementation will still need.

Outcome: a finalized `## Design`. Take material objections to the user before moving on.

### 3 — plan

> Planning phase of a multi-phase feature coordinated by another session. From the finalized `## Design` on the task at `<feature-url>`, produce an implementation plan and split it into stages — one stage if the work is small. For EACH stage call `brog::create_task` to open a stage task whose body carries: the stage's goal and details; "part of feature [`<feature-name>`](`<feature-url>`) — read its `## Design` and `## Design changelog` before starting"; "land via `@::run-pr` with its base argument set to `<feature-branch>`, so this PRs into the feature branch rather than master"; "when the PR merges, mark this task done — do not hold it for a rollout, the feature rolls out once after integration"; and "if you change a design decision mid-build, append it to the feature page's `## Design changelog` for the history and the later stages". Then establish the feature integration branch: `git fetch origin master && git reset --hard origin/master && git push -u origin $(git branch --show-current)`. Finally write `## Implementation plan` on the feature task (`brog::append_description`): the feature branch name and the ordered stages, each linking its stage task. Do NOT implement. Answer with the stage list, the feature branch name, and any open question about the breakdown.

Outcome: stage tasks created and linked, the feature branch pushed. Settle the worker's open questions with the user before starting stage 1.

### 4 — stages

Run them **in order, one at a time**: each stage builds on the branch state the previous one left. For each, summon a worker on the stage task with the feature branch as its base ref and a review-sized timeout:

> Work the task at `<stage-url>` through `@::fix`. Its body carries the feature context and how to land it.

The worker implements, opens its PR into the feature branch, carries it through review, lands it there, and closes its stage task. Record the outcome and move to the next stage.

If a stage reports a blocker or a design change, decide with the user whether the plan needs adjusting — a repointed stage, an added one — before continuing. A stage that raises leaves its work recoverable on a pushed ref named in the reason; the retry is a fresh summon on the same stage task.

### 5 — integrate

Once every stage task is done:

> Integration phase of a multi-phase feature coordinated by another session. This workspace is on the feature branch `<feature-branch>`. Sync it against origin, rebase it onto `origin/master` (force-push the FEATURE branch with `--force-with-lease` if the rebase rewrote it — never force-push master), then open ONE pull request for the whole feature with `@::run-pr` based on master and land it with `@::land`. Skip the review round: every stage PR was reviewed already, so treat this as an explicit waiver of the approval precondition rather than waiting on a second review of the same code. Keep the task at `<feature-url>` open whatever happens — the coordinating session closes it after verification. If the merged feature needs a rollout to take effect, hand it off per `@::land`'s own rules and report what came back. Answer with the merged PR, the squash commit, and the rollout outcome if there was one.

Outcome: the feature on master as a single squash, rolled out if it needed one. When the worker reports a rollout it could not hand off — no operations bro in its allow-list — relay the exact command to the user and confirm it ran before verifying.

### 6 — verify

Once the feature is live — merged, and rolled out if it needed a rollout:

> Verification phase of a multi-phase feature coordinated by another session. The feature has shipped. Read `## Goal` and `## Design` on the task at `<feature-url>`, derive concrete checks from the goal, and exercise the shipped capability end to end against the real system — confirm the behavior, not merely that the services are up. Append a `## Verification` section to that task (`brog::append_description`) recording what you checked and what happened. Do NOT change code or the task status; if something is broken, report the defect rather than fixing it. Answer with the result.

Grant this phase the credentials the feature's live surface needs. A failed verification is not a close: surface it, and plan a fix stage with the user.

### 7 — close

**Closing the feature task is yours alone** — no phase does it for you. Once the goal is met, any rollout is confirmed, and verification passed, add a final comment summarizing what shipped and close the task (`brog::update_task(<feature-id>, status='done')`). Don't leave a finished feature open.

## Guardrails

- If you find yourself about to read code, weigh a design, or write an instruction that only makes sense to someone who has read the diff — stop. That belongs in a phase.
- If a worker's report contradicts the plan — a stage proved infeasible, the design moved materially — surface it and re-plan with the user before summoning the next phase.
- Skip a phase that does not apply and say so; don't invent work to fill it.
