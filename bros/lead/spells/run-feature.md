---
name: run-feature
description: This spell should be used when the user wants a large piece of work driven end to end from a coordinator session — "start a feature", "kick off the <X> feature", "let's design and build <big thing>", "run the feature workflow", "orchestrate this", "resume the feature at <url>". This session becomes the coordinator: it opens a feature task as the single source of truth, then walks the work through design, review and planning, per-stage implementation, integration, and verification, running each phase in a session of its own — the design and planning phases as interactive sessions it hands to the user to launch, the rest as summoned bros in isolated containers — and recording each outcome on the feature page before starting the next. It never designs or implements itself. For work that fits one session this is overkill — summon a single bro on the task ([[ask]]) and let it run [[fix]] itself.
parameters: {"feature?": "ref of an existing feature task to resume", "new?": "seed text for a new feature"}
version: 1.4.1
---

# run-feature

Coordinate a feature across many short-lived sessions. You are the root: you never design, plan, or write code. You own the feature task page, and each phase is a session of its own that does the work, writes its artifact to the page, and ends.

## Operating principle — the root does no hard work

- **Keep context sparse.** Do not read the codebase, draft designs, or implement. Every unit of real work happens in a phase session.
- **The feature page is the single source of truth.** Each turn, read it and the statuses of the stage tasks it links to recover where the feature stands, then act.
- **One phase at a time.** Launch a phase, wait for it to finish, record the outcome, review the artifact, then launch the next. Everything durable must land on the page: a summoned phase answers you as well, a handed-off one returns nothing at all.
- **You are the human's interface.** A summoned bro runs isolated with no human channel: one that cannot proceed raises with a reason instead of asking. Questions, corrections, and go/no-go between phases are yours to handle.

## Invocation forms

Read the appended `# Arguments` section:

- `new` — seed text for a feature to open (Step 0). An empty value means collect the seed from the user.
- `feature` — ref of an existing feature task: resume it.
- neither — ask the user which it is.

`feature` and `new` are mutually exclusive; if both appear, stop and ask the user to choose one.

**Resuming.** Read the description and comments (`brog::read_task` / `brog::read_comments`) plus each linked stage task's status, infer which phase the feature stands at, and continue from there. Never redo a completed phase.

## Step 0 — open the feature task

1. Discuss scope with the user only far enough to pin down a name and tags — you are framing the feature, not designing it. Names start with a lowercase letter (except proper nouns).
2. Settle the **bro** for each phase. Read your allow-list off the banner (`bro::banner`, `may_summon`): where it names one plausible candidate, take it for every phase; where several could take a phase, ask the user which. If a phase wants a different bro than the rest — the one that does rollouts, say — settle that now too, and say so up front when the list holds nobody who could run a phase, since the list is fixed at launch and only a relaunch widens it. The list bounds the summoned phases alone; a handed-off phase names its bro on the command the user runs.
3. `brog::create_task` with the name, tags, and a `## Goal` body stating in a few lines what the feature must achieve. The task is born open. Its returned url is the feature URL every phase prompt carries.
4. Record the kickoff comment, naming who runs the phases so a resumed session recovers it, then start the design phase.

## The feature page

Sections accumulate on the description; the phases write them with `brog::append_description` / `brog::edit_description`. Your own running record lives in the comment stream, not in a section.

- `## Goal` — written at creation.
- `## Design` — written by the design phase, finalized by the review-and-plan phase.
- `## Implementation plan` — written by the review-and-plan phase: the feature integration branch name plus the ordered stages, each linking its stage task.
- `## Design changelog` — appended by a stage when a design decision changes mid-build, so later stages and the history see it.
- `## Verification` — appended by the verification phase: what it exercised against the shipped result, and the outcome.

Record orchestration events as comments (`brog::add_comment(id, topic, body)`): a `kickoff` entry to open, then 1–3 lines after each phase whose topic names the outcome (`design done`, `stage 2 landed`). Recover per-stage progress from the stage tasks' own statuses, not from memory.

## Launching a phase

Every phase below opens with its **launch line** — the knobs that phase needs; anything the line does not name takes the default. Design and review-and-plan run as interactive sessions you hand to the user; the rest are summoned.

### Summoned phases

The summon mechanics — client pick, relaying, and every failure mode — are [[ask]]'s; this spell only says how a phase differs from a one-shot ask.

- **Never wait inline.** No phase is short enough for a blocking wait: send every one detached and poll for its result.
- **Hold and effort.** Leave both at the summon defaults. A bro with no human channel either delivers or raises with a reason you relay, and these phases execute a settled plan rather than working one out — the thinking was bought in the phases before them.
- **Scope.** A bro starts from its own credentials, not yours. Grant only what a phase needs beyond them and only what you hold yourself: a credential the phase must reach, or `@<bro>` when the phase has to hand work onward.
- **Self-contained prompts.** A bro shares no context with you: spell out the feature URL, what to produce, where to put it, and what not to touch. Ask for the open questions and unmet prerequisites in its *answer* rather than on the page — they are yours to act on, not the page's to carry.

Do not let this session end with a phase in flight; [[ask]] covers reclaiming a summon whose wait was lost.

### Handed-off phases

These want an interactive session rather than a one-shot summon, and no session can launch one for itself. Give the user one command; `--harness` selects Claude Code or the bro's native chat loop:

```
ride along <bro> '<phase prompt>' <launch line> --harness <claude|bro>
```

- The launch line's `--hold` and `--effort` carry the same meaning under either harness.
- The phase prompt goes in verbatim as the positional argument, so the session starts on it directly instead of through [[fix]]. It is as self-contained as a summon's — the session shares no context with you either. Quote it so its own apostrophes and backticks survive the shell.
- No base ref: neither phase needs one — design only reads the codebase, and review-and-plan resets the feature branch to `origin/master` itself.
- **Nothing returns to you.** There is no answer channel, so whatever the phase has to report goes on the feature page — its prompt closes by recording a comment rather than answering.
- Then stop and wait. The session runs in the user's terminal, not yours; you learn it finished when they tell you. Pick up by reading the page.

## Phases

Between phases, review the artifact the phase produced before launching the next one. A report of a blocker, an unmet prerequisite, or a design change is yours to resolve — fold the fix into the next phase's launch (an added grant, a corrected instruction) rather than reopening the finished one.

### 1 — design

**Hand off:** `--hold attended --effort max`

Attended because this is where the human's input is worth the most: the session brings each pivotal decision to them as it comes up.

> Design phase of a multi-phase feature coordinated by another session. Read the task at `<feature-url>` — its `## Goal` states what to design. Explore the codebase, work the design out, and stress-test it yourself: edge cases, risks, and the alternatives you rejected with the reason. Settle open questions with the user one at a time. Before writing, present the complete proposed design and obtain the user's explicit approval. If the discussion changed a requirement, update `## Goal` before publishing `## Design`. Do not publish or close while an agent-originated decision remains open to later veto. Write the result to that task under `## Design` (`brog::append_description`). Do NOT implement code, open a PR, or change the task status. Close by recording a comment on that task (`brog::add_comment`, topic `design`) with a summary of the design, every question left unsettled, and any prerequisite you could not satisfy from inside this session — a credential you would have needed, a step that needs the host.

Outcome: `## Design` on the page, and a `design` comment carrying what the coordinator has to act on.

### 2 — review and plan

**Hand off:** `--hold unattended --effort max`

A fresh session — the value here is eyes that did not write the design. Unattended, so the review is an independent pass rather than a steered one, and a blocker ends it with a stated reason rather than a question. Reviewing and planning are one phase: the reviewer ends up holding the deepest understanding of the design, which is what splitting it into stages needs.

> Design-review and planning phase of a multi-phase feature coordinated by another session. Read the whole task at `<feature-url>`, then its `## Design` with fresh eyes: find the problems and the improvements, and verify the design's assumptions wherever you can reach them — introspect the real schemas, APIs, and call sites instead of leaving them as open questions; for every contract shared with a separately deployed process, state the rollout order and how mixed versions remain operable. Finalize the design in place (`brog::edit_description` on the `## Design` section, leaving the other sections intact). Then plan the implementation of the design you just finalized and split it into stages — one stage if the work is small. For EACH stage call `brog::create_task` to open a stage task whose body carries: the stage's goal and details; "part of feature [`<feature-name>`](`<feature-url>`) — read its `## Design` and `## Design changelog` before starting"; "land via [[run pr]] with its base argument set to `<feature-branch>`, so this PRs into the feature branch rather than master"; "when the PR merges, mark this task done — do not hold it for a rollout, the feature rolls out once after integration"; and "if you change a design decision mid-build, append it to the feature page's `## Design changelog` for the history and the later stages". Then establish the feature integration branch: `git fetch origin master && git reset --hard origin/master && git push -u origin $(git branch --show-current)`. Finally write `## Implementation plan` on the feature task (`brog::append_description`): the feature branch name and the ordered stages, each linking its stage task. Do NOT implement code or open a PR, and do not change any task's status. Close by recording a comment on the feature task (`brog::add_comment`, topic `plan`) with what you changed in the design and why, the stage list, the feature branch name, and any prerequisite implementation will still need.

Outcome: a finalized `## Design`, stage tasks created and linked under `## Implementation plan`, the feature branch pushed, and a `plan` comment. Take material objections and open questions to the user before starting stage 1.

### 3 — stages

**Summon:** `into` the feature branch · `timeout` 28800

The long timeout covers the PR review a phase ends on: it idles on human latency, and the summon default kills it mid-watch.

Run them **in order, one at a time**: each stage builds on the branch state the previous one left. For each, summon the bro on the stage task:

> Work the task at `<stage-url>` through [[fix]]. Its body carries the feature context and how to land it.

The bro implements, opens its PR into the feature branch, carries it through review, lands it there, and closes its stage task. Record the outcome and move to the next stage.

If a stage reports a blocker or a design change, decide with the user whether the plan needs adjusting — a repointed stage, an added one — before continuing. A stage that raises leaves its work recoverable on a pushed ref named in the reason; the retry is a fresh summon on the same stage task.

### 4 — integrate

**Summon:** `into` the feature branch · `timeout` 28800 · `grant` `@<the bro that does rollouts>` when the feature needs one to go live

Once every stage task is done:

> Integration phase of a multi-phase feature coordinated by another session. This workspace is on the feature branch `<feature-branch>`. Sync it against origin, rebase it onto `origin/master` (force-push the FEATURE branch with `--force-with-lease` if the rebase rewrote it — never force-push master), then open ONE pull request for the whole feature with [[run pr]] based on master and land it with [[land]]. Skip the review round: every stage PR was reviewed already, so treat this as an explicit waiver of the approval precondition rather than waiting on a second review of the same code. Keep the task at `<feature-url>` open whatever happens — the coordinating session closes it after verification. If the merged feature needs a rollout to take effect, hand it off per [[land]]'s own rules and report what came back. Answer with the merged PR, the landed commit, and the rollout outcome if there was one.

Outcome: the feature on master as a single commit, rolled out if it needed one. When the phase reports a rollout it could not hand off — nobody in its allow-list to do it — relay the exact command to the user and confirm it ran before verifying.

### 5 — verify

**Summon:** `into` `master`, which the feature is on by then · `grant` the credentials the feature's live surface needs

Once the feature is live — merged, and rolled out if it needed a rollout:

> Verification phase of a multi-phase feature coordinated by another session. The feature has shipped. Read `## Goal` and `## Design` on the task at `<feature-url>`, derive concrete checks from the goal, and exercise the shipped capability end to end against the real system — confirm the behavior, not merely that the services are up. Append a `## Verification` section to that task (`brog::append_description`) recording what you checked and what happened. Do NOT change code or the task status; if something is broken, report the defect rather than fixing it. Answer with the result.

A failed verification is not a close: surface it, and plan a fix stage with the user.

### 6 — close

**Closing the feature task is yours alone** — no phase does it for you. Once the goal is met, any rollout is confirmed, and verification passed, add a final comment summarizing what shipped and close the task (`brog::update_task(<feature-id>, status='done')`). Don't leave a finished feature open.

## Guardrails

- If you find yourself about to read code, weigh a design, or write an instruction that only makes sense to someone who has read the diff — stop. That belongs in a phase.
- If a phase's report contradicts the plan — a stage proved infeasible, the design moved materially — surface it and re-plan with the user before launching the next phase.
- Skip a phase that does not apply and say so; don't invent work to fill it.
