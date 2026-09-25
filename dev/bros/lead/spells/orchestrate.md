---
name: orchestrate
description:

This spell should be used when the user wants a large piece of work driven end to end from a coordinator session
— "orchestrate this", "kick off the <X> feature", "let's design and build <big thing>", "drive this refactoring end to end", "resume the work at <url>".
This session becomes the coordinator:
it opens a root task as the single source of truth,
then walks the work through design, review and planning, per-stage implementation, integration, rollout, and verification,
running each phase in a session of its own
— the design and planning phases, and any rollout step a summon cannot carry, as manual summons the user launches, the rest as summoned bros in started parties
— and recording each outcome on the root task before starting the next.
It never designs or implements itself.
For work that fits one session this is overkill — summon a single bro on the task ([[ask]]) and let it run [[fix]] itself.

parameters: {"task?": "ref of an existing root task to resume", "new?": "seed text for a new piece of work"}
version: 2.11.0
---

# orchestrate

Coordinate a large piece of work across many short-lived sessions.
You are the root:
you never design,
plan,
or write code.
You own the root task page, and each phase is a session of its own that does the work,
writes its artifact to the page,
and ends.

## Operating principle — the root does no hard work

- **Keep context sparse.** Do not read the codebase,
  draft designs,
  or implement.
  Every unit of real work happens in a phase session.
- **The root task page is the single source of truth.** Each turn, read it and the statuses of the stage tasks it links to recover where the work stands, then act.
- **One phase at a time.** Launch a phase,
  wait for it to finish,
  record the outcome,
  review the artifact,
  then launch the next.
  Everything durable must land on the page;
  a phase's answer carries only what you have to act on.
- **You are the human's interface.** A summoned bro runs isolated with no human channel,
  but each phase is granted `worker.question` so it can consult this coordinator before giving up its live state.
  Questions,
  corrections,
  and go/no-go between phases are yours to handle;
  a phase raises only when its question channel cannot resolve the blocker.

## Invocation forms

Read the appended `# Arguments` section:

- `new` — seed text for the work to open (Step 0).
  An empty value means collect the seed from the user.
- `task` — ref of an existing root task:
  resume it.
- neither — ask the user which it is.

`task` and `new` are mutually exclusive;
if both appear, stop and ask the user to choose one.

**Resuming.** Read the description and comments (`brog::read_task` / `brog::read_comments`) plus each linked stage task's status,
infer which phase the work stands at,
and continue from there.
Never redo a completed phase.

## Step 0 — open the root task

1. Discuss scope with the user only far enough to pin down a name and tags
   — you are framing the work, not designing it.
   Names start with a lowercase letter (except proper nouns).
2. Settle the **bro** for each phase.
   Read your allow-list off the banner (`bro::banner`, `may_summon`):
   where it names one plausible candidate, take it for every phase;
   where several could take a phase, ask the user which.
   If a phase wants a different bro than the rest
   — the one that does rollouts, say
   — settle that now too.
   Then check every phase's summon the way you check any summon, all of them at once, so the user settles each way out before the work starts.
3. `brog::create_task` with the name,
   tags,
   and a `## Goal` body stating in a few lines what the work must achieve.
   The task is born open.
   Its returned url is the root task URL every phase prompt carries.
4. Record the kickoff comment, naming who runs the phases so a resumed session recovers it,
   then start the design phase.

## The root task page

Sections accumulate on the description;
the phases write them with `brog::append_description` / `brog::edit_description`.
Your own running record lives in the comment stream, not in a section.

- `## Goal` — written at creation.
- `## Design` — written by the design phase, finalized by the review-and-plan phase.
- `## Implementation plan` — written by the review-and-plan phase:
  the landings in order, each with its integration branch and its ordered stages linking their stage tasks, and a `### Rollout` when the work needs one to go live.
- `## Design changelog` — appended by a stage when a design decision changes mid-build, so later stages and the history see it.
- `## Verification` — appended by the verification phase:
  what it exercised against the shipped result, and the outcome.

Record orchestration events as comments (`brog::add_comment(id, topic, body)`):
a `kickoff` entry to open,
then 1–3 lines after each phase whose topic names the outcome (`design done`, `stage 2 landed`).
Recover per-stage progress from the stage tasks' own statuses, not from memory.

## Launching a phase

Every phase below opens with its **launch line**
— the knobs that phase needs;
anything the line does not name takes the default.
Design and review-and-plan are manual summons the user launches into an interactive session;
the rest are summoned outright, bar the rollout steps your summon check leaves to a manual summon (phase 5).

### Summoned phases

The summon mechanics — client pick,
relaying,
and every failure mode
— are [[ask]]'s;
this spell only says how a phase differs from a one-shot ask.

- **Never wait inline.** No phase is short enough for a blocking wait:
  send every one detached with `worker.question` in its talk and collect its result through the surface's watch or polling flow.
  {{iff #harness = claude}}Keep a `watch-next` waiting on the quest watch.
  When it reports a child's question, answer that quest with `quest say --reply-to`.
  Then resume the same `quest check --wait` loop.{{eliff #harness = bro}}Keep the `quest watch` job armed and call `bro::chill` whenever nothing else remains;
  a turn that ends with a phase in flight gets one notice, and the next such turn end ends the run and orphans the phase.
  When it reports a child's question, answer that quest with `bro::quest_say(reply_to=…)`, then chill again.
  Read the retained answer with `bro::quest_check` after the terminal line.{{end}}
  Never launch a replacement phase to answer it.
- **Hold and effort.** Leave both at the summon defaults.
  A bro with no human channel either delivers or raises with a reason you relay, and these phases execute a settled plan rather than working one out
  — the thinking was bought in the phases before them.
- **Scope.** Your summon check settles what a phase needs.
  Grant only what it needs beyond its bro's declarations and only what you hold yourself:
  `@<bro>` when the phase has to hand work onward, or a party permit when it has to start sessions of its own.{{when #may_summon contains eyebro}}
- **The eyebro.** Every phase that opens or lands a pull request gets it granted,
  under the name your banner's `may_summon` gives rather than `eyebro` itself:
  a child renders [[run pr]]'s and [[land]]'s reviewer steps only where its own allow-list carries one.{{end}}
- **Self-contained prompts.** A bro shares no context with you:
  spell out the root task URL,
  what to produce,
  where to put it,
  and what not to touch.
  Ask for the open questions and unmet prerequisites in its *answer* rather than on the page
  — they are yours to act on, not the page's to carry.

Do not let this session end with a phase in flight;
[[ask]] covers reclaiming a summon whose wait was lost.

### Manually summoned phases

These want the user in the session rather than a one-shot run, so they go out as manual summons
— the host registers the phase on a token, and the user launches the session against it.
The mechanics are [[ask]]'s;
the summon carries the phase prompt and none of the launch line, since hold, effort, and harness belong to the launch.
Relay the token as the command the user pastes, the launch line appended and `--harness` selecting Claude Code or the bro's native chat loop:

```
ride along --summoned <token> <bro> <launch line> --harness <claude|bro>
```

- The launch line's `--hold` and `--effort` carry the same meaning under either harness.
- The phase prompt becomes the session's first message, so the session starts on it directly instead of through [[fix]].
  It is as self-contained as any summon's
  — the session shares no context with you either.
- No base ref:
  neither phase needs one
  — design only reads the codebase,
  and review-and-plan resets the integration branch to `origin/master` itself.{{when #may_summon contains eyebro}}
- The eyebro goes to review-and-plan alone, on its summon request rather than the launch line, since the request fixes a manual child's allow-list;
  the design phase opens no pull request.{{end}}
- No talk beyond the default:
  the user is in the session, so its questions go to them rather than to you.
- **The answer reaches you.** A manual child delivers through `answer` like any summoned one, so the prompt closes by answering with what you have to act on.
- Then wait as for any detached summon:
  the token reads as running until the user launches, and a manual summon carries no timer, so pace the wait to human time.
  Pick up from the answer and the page.

## Phases

Between phases, review the artifact the phase produced before launching the next one.
A report of a blocker,
an unmet prerequisite,
or a design change is yours to resolve
— fold the fix into the next phase's launch (an added grant, a corrected instruction) rather than reopening the finished one.

Phases 3 to 5 run once per landing, in the plan's order:
the landing's stages, its integration, then the rollout steps that follow it;
verification comes after the last.

### 1 — design

**Manual summon:** launched `--hold attended --effort max`

Attended because this is where the human's input is worth the most:
the session brings each pivotal decision to them as it comes up.

> Design phase of multi-phase work coordinated by another session.
> Read the task at `<root-task-url>`
> — its `## Goal` states what to design.
> Explore the codebase,
> work the design out,
> and stress-test it yourself:
> edge cases,
> risks,
> and the alternatives you rejected with the reason.
> Settle open questions with the user one at a time.
> Before writing, present the complete proposed design and obtain the user's explicit approval.
> If the discussion changed a requirement, update `## Goal` before publishing `## Design`.
> Do not publish or close while an agent-originated decision remains open to later veto.
> Write the result to that task under `## Design` (`brog::append_description`).
> Do NOT implement code,
> open a PR,
> or change the task status.
> Answer with a summary of the design,
> every question left unsettled,
> and any prerequisite you could not satisfy from inside this session
> — a credential you would have needed,
> a step that needs the host.

Outcome:
`## Design` on the page, and an answer carrying what the coordinator has to act on.

### 2 — review and plan

**Manual summon:** launched `--hold attended --effort max`{{when #may_summon contains eyebro}}, requested with `grant` `@<the eyebro>`{{end}}

A fresh session supplies independent eyes.
Attended hold lets it bring material objections and open design decisions to the user before finalizing the design.{{when #may_summon contains eyebro}}
The eyebro reviews the design as a document on a throwaway pull request:
a line-addressable surface, and approvals on record.{{end}}
Reviewing and planning are one phase:
the reviewer ends up holding the deepest understanding of the design, which is what splitting it into stages needs.

> Design-review and planning phase of multi-phase work coordinated by another session.
> Read the whole task at `<root-task-url>`, then its `## Design` with fresh eyes:
> find the problems and the improvements,
> and verify the design's assumptions wherever you can reach them
> — introspect the real schemas,
> APIs,
> and call sites instead of leaving them as open questions;
> for every contract shared with a separately deployed process, state the rollout order and how mixed versions remain operable.
> Bring material objections and open design decisions to the user before editing the design.{{when #may_summon contains eyebro}}
> Then put the design as it stands into a markdown file on a throwaway branch and open a temporary pull request against master whose title and body say it is a design review only, never to be merged.
> Watch it with `poll-pr` the way [[run pr]] watches the pull requests it opens, armed before the reviewer is summoned.
> [[Ask <the eyebro>]] to review that pull request as a document, not code:
> gaps, contradictions, unverified assumptions, a missing rollout order, and better alternatives, not tests or CI.
> Fold its findings into the file until it approves, settling with the user in this session whatever needs their opinion;
> the design is settled when both the eyebro and the user have approved the pull request on GitHub.{{end}}
> Finalize the design in place (`brog::edit_description` on the `## Design` section, leaving the other sections intact).{{when #may_summon contains eyebro}}
> Then close the pull request without merging and delete the branch:
> the page is the single source of truth and the repository copy is throwaway.{{end}}
> Then plan the implementation of the design you just finalized and split it into stages
> — one stage if the work is small.
> Group the stages into landings, each a merge to master through an integration branch of its own:
> one landing, unless the rollout needs steps between merges to master
> — deploying what accepts both shapes before anything writes the new one, say.
> For EACH stage call `brog::create_task` to open a stage task whose body carries:
> the stage's goal and details;
> "part of the multi-phase work tracked at [`<root-task-name>`](`<root-task-url>`) — read its `## Design` and `## Design changelog` before starting";
> "land via [[run pr]] with its base argument set to `<its landing's integration branch>`, so this PRs into the integration branch rather than master";
> "when the PR merges, mark this task done — do not hold it for a rollout, the coordinator rolls the work out after its landing merges";
> and "if you change a design decision mid-build, append it to the root task's `## Design changelog` for the history and the later stages".
> The first stage of each later landing carries one more line, since its branch can only start from what the landing before it left on master:
> "first create `<its landing's integration branch>` on origin from master, staying on your own workspace branch: `git fetch origin master && git push origin origin/master:refs/heads/<branch>`, skipped where origin already has it".
> Every end-to-end route the design names is owed automated coverage by the last stage that completes it, and that stage's task names the routes it owes;
> the verification phase confirms the shipped result against the real system and is never where a route runs for the first time.
> Then establish the first landing's integration branch, every landing's named after the root task rather than left as the workspace's own:
> `git fetch origin master && git reset --hard origin/master && git checkout -b integration/<root-task-id>-<short-slug> && git push -u origin HEAD`.
> The prefix is what lets a repository write branch rules over the branches stages merge into, so the name is part of the contract, not decoration.
> Finally write `## Implementation plan` on the root task (`brog::append_description`):
> the landings in order, each with its integration branch and its ordered stages linking their stage tasks,
> and, when the work needs a rollout to go live, a `### Rollout` of ordered steps.
> Each step carries its commands, the check that confirms it, its rollback, and the landing it follows,
> and names the session that runs it:
> the bro, its isolation, and the credentials it needs.
> A step is the user's only where nothing but a human can do it, such as an approving review.
> Do NOT implement code or open a PR{{when #may_summon contains eyebro}} other than the design review's{{end}},
> and do not change any task's status.
> Answer with what you changed in the design and why,{{when #may_summon contains eyebro}}
> the closed design-review pull request,{{end}}
> the landings with their integration branches and stages,
> the rollout steps with the session each names,
> and any prerequisite implementation or rollout will still need.

Outcome:
a finalized `## Design`,
stage tasks created and linked under `## Implementation plan`,
the first landing's integration branch pushed,
and an answer carrying what changed and what the coordinator has to act on.
Before starting stage 1, take to the user the material objections, the open questions, and whatever ways out your summon check finds for the rollout's steps.

### 3 — stages

**Summon:** `into` the landing's integration branch, or the commit the previous landing landed for the stage that creates that branch · `timeout` 28800 · `talk` `worker.question`{{when #may_summon contains eyebro}} · `grant` `@<the eyebro>`{{end}}

The long timeout covers the PR review a phase ends on:
it idles on human latency, and the summon default kills it mid-watch.

Run the landing's stages **in order, one at a time**:
each stage builds on the branch state the previous one left.
For each, summon the bro on the stage task:

> Work the task at `<stage-url>` through [[fix]].
> Its body carries the root task it belongs to and how to land it.

The bro implements,
opens its PR into the integration branch,
carries it through review,
lands it there,
and closes its stage task.
Record the outcome and move to the next stage.

If a stage reports a blocker or a design change, decide with the user whether the plan needs adjusting
— a repointed stage,
an added one — before continuing.
A stage that raises leaves its work recoverable on a pushed ref named in the reason;
the retry is a fresh summon on the same stage task.

### 4 — integrate

**Summon:** `into` the landing's integration branch · `timeout` 28800 · `talk` `worker.question`{{when #may_summon contains eyebro}} · `grant` `@<the eyebro>`{{end}}

Once every stage task of the landing is done, tell the user what the last step needs from them:
where master is a protected base, the approving review that lands the integration PR is theirs to give, and nobody in the run can supply it.
Then summon:

> Integration phase of multi-phase work coordinated by another session.
> This workspace is on the integration branch `<integration-branch>`.
> Sync it against origin and rebase it onto `origin/master`.
> Then take the rebased result onto a branch of its own before doing anything else:
> `git checkout -b integration-pr/<the integration branch's own slug>`.
> The pull request goes up from THAT branch;
> the integration branch is never pushed again.
> A repository that gates the branches stages merge into refuses every direct push to them, the rebase's included, so a phase that pushed the integration branch would be stuck with nothing to open a PR from
> — and the two roles want separating anyway, since what stages merge into should not be the thing a merge to master rewrites.
> Then open ONE pull request for all the landing's stages together with [[run pr]] based on master and land it with [[land]].
> Its body names the stage pull requests and the integration branch their commits landed on
> — the task's comments carry them
> — so the reviewer reconciles those approvals instead of re-reading the stages.
> Record the pull request on the task at `<root-task-url>` as soon as it is open (`brog::add_comment`, topic `integration pr`), so the page carries the one that took the stages to master.
> Where master is protected, the approving review that clears the merge has to come from the user:
> an agent's own approval does not satisfy the base's rule, so a land refused for want of a review is the expected state there rather than a blocker.
> Keep the review watcher running through it
> — restart it if the chain into [[land]] already stopped it
> — and land when the user's approval arrives.
> Keep the task at `<root-task-url>` open whatever happens
> — the coordinating session closes it after verification.
> Roll nothing out, even where [[land]] would hand a deploy off:
> the coordinating session runs the rollout as a phase of its own.
> Answer with the merged PR and the landed commit.

Outcome:
the landing on master as a single commit.
A phase whose time runs out waiting for that approval leaves the pull request open:
re-fire it on that PR, which [[run pr]] resumes through its `pr` argument, rather than opening a second one.

### 5 — roll out

**Summon:** the bro the step names · `into` the commit its landing landed, as the integration phase answered it · `timeout` sized for the step · `talk` `worker.question`

**Manual summon** where your summon check leaves the step to one:
launched with what it lacks
— `--grant <kind>` for a credential its bro does not declare, `--cred <kind>+<instance>` beside it where a non-default instance must be picked, and `--unboxed` only where the step needs the host

Skip this phase when no `### Rollout` step follows the landing.
Otherwise run the steps that follow it in order, one session each, a step starting once the previous step's check has passed.

> Rollout step <n> of multi-phase work coordinated by another session.
> Run step <n> of `### Rollout` on the task at `<root-task-url>` as written, then its check;
> on a failed check, run the step's rollback and stop.
> Record the outcome on that task (`brog::add_comment`, topic naming the step).
> Do NOT change code, open a pull request, run any other step, or change the task status.
> Answer with the check's result and whatever a later step must know.

A manual step keeps the summon's `into` and `talk` on its request and is relayed like the manually summoned phases, its launch line carrying the scope it lacks:

```
ride along --summoned <token> <bro> <what the step lacks> --harness <claude|bro>
```

A step that needs this session gone first
— an upgrade of the installation it runs on, say
— cannot wait on a token, so hand the user the whole sequence instead, `<repo>` being your banner's `repo`:

1. end this session;
2. launch the step:
   `ride along --repo <repo> --into <landed commit> <what the step lacks> <bro> '<step prompt>'`;
3. once the step has recorded its outcome on the page, relaunch the coordinator fresh from your banner's `ride_command`, dropping its `--workspace` and `--into`, with a prompt naming the root task;
   it resumes from that outcome.

Outcome:
every step's check passed and recorded on the page, or a failed step rolled back and surfaced to the user.

### 6 — verify

**Summon:** `into` `master`, which the work is on by then · `talk` `worker.question` · credentials from `projects.<identity>.bros.<the phase bro>` in the host config

Once the work is live
— merged, and rolled out if it needed a rollout:

> Verification phase of multi-phase work coordinated by another session.
> The work has shipped.
> Read `## Goal` and `## Design` on the task at `<root-task-url>`,
> derive concrete checks from the goal,
> and exercise the shipped capability end to end against the real system
> — confirm the behavior, not merely that the services are up.
> Append a `## Verification` section to that task (`brog::append_description`) recording what you checked and what happened.
> Do NOT change code or the task status;
> if something is broken, report the defect rather than fixing it.
> Answer with the result.

A failed verification is not a close:
surface it, and plan a fix stage with the user.

### 7 — close

**Closing the root task is yours alone**
— no phase does it for you.
Once the goal is met,
any rollout is confirmed,
and verification passed, add a final comment summarizing what shipped and close the task (`brog::update_task(<root-task-id>, status='done')`).
Don't leave finished work open.

## Guardrails

- If you find yourself about to read code,
  weigh a design,
  or write an instruction that only makes sense to someone who has read the diff
  — stop.
  That belongs in a phase.
- If you find yourself about to hand the user commands to run
  — a deploy, a migration, a host-side check
  — stop.
  That is a phase too, and the user's part in it is a launch line and the approvals only they can give.
- If a phase's report contradicts the plan
  — a stage proved infeasible,
  the design moved materially
  — surface it and re-plan with the user before launching the next phase.
- Skip a phase that does not apply and say so;
  don't invent work to fill it.
