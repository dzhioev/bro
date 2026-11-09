# Design: review channels, roles and landing gates for `run-pr`

This file is the specification of [#1205](https://github.com/dzhioev/bro/issues/1205).
It lives on the branch `design/1205-split-run-pr`, which is never merged:
the pull request that carries it is a design review only.

## 1. Goal, scope and constraints

Goal: `run-pr` takes a change through an explicitly chosen review and an independently gated landing, with its roles, the accounts it listens to, and its token accounting made explicit.
The task's `## Goal` states the six outcomes the verification phase checks.

What it enables, for example:

- A developer whose reviewer bro would share their GitHub identity gets a verdict over the reviewer's quest (`talk` or `local`) instead of a summon that fails.
- A benchmark box with neither Docker nor a remote reviews a change with a joined reviewer and lands it onto its local base branch, with no credential at all.
- A coordinator summons the developer and the reviewer as two unlinked children and holds the verdict itself.
- A landing requires a human's approval, a bro's verdict, CI, the local gate, or any subset stated explicitly, whichever method lands it.
- A comment from an account the review does not trust never reaches the author's model.
- The footer of an orchestrated feature's landing counts its design, planning and coordination as well as its stages and their reviewers.

The owner settled the design's decisions on 2026-11-08 and 2026-11-09;
§15 records each one and the section that applies it.

Out of scope, each with where it belongs:

- what a review judges, which stays `review-diff`'s;
- the substance of preparing a branch (survey, formatter, policy audit, docs, commit splits, messages, rebase, the change-scoped gate), which `run-pr` keeps as it is;
- deploys, which stay `land`'s handoff;
- the usage-totals mission §9 relies on, and recording `mu` calls with their summoner, which are a design of their own in the coordinator's plan (§14);
- signed review tags, a follow-up the verdict of §4 is ready for (§14).

Constraints:

- framework code stays consumer-neutral, so the core addition (§7.1) names no development concept;
- core imports no other workspace member;
- backward compatibility is not a constraint (`AGENTS.md`, "Conventions");
- a violated assumption fails fast (the development style policy);
- every rendering of a spell fits the 30,000-character tool-result window (`bro.base.text_window.BYTE_LIMIT`) that both `bro::cast` and the `spell::` tools return through;
- GitHub's rebase merge writes new commit ids and drops commits that were empty to begin with (§16);
- this repository's `master` requires a pull request with one approval from an account its rules count, the last push approved by someone other than its pusher, linear history, and the `tests` check (§16).

## 2. Concepts

Terms the repository already defines, as this design uses them:

- **Change** — the commits a branch adds over its base, landing together.
- **Head** — the commit at the tip of a change;
  verdicts, checks and approvals are each about one head.
- **Base** — the branch a change lands on, written `<base>`;
  the **base commit** is the tip of the base a verdict or check was made against.
- **Landing shape** — one commit per logical change, each with the message the base will carry (`run-pr`, "Fold the branch").
- **Fold** — `fold-branch` rewriting the branch into landing shape, proving the final tree unchanged.
- **Footer** — the token-accounting line `> created with <agents> | <model>: ↑(<input> <cache-write> <cache-read>) ↓<output>, …` owned by `bro.llm.usage`, appended to agent commits by the `commit-msg` hook.
- **Quest**, **talk rights** and **placement** — a summon's conversation, its four rights, and `boxed`, `unboxed` or `join` (`bro/reference/ride.md`, "Summoning another bro").
- **Artifact** — a content-addressed file or directory passed between peers of one ride (`bro/reference/ride.md`, "Sharing artifacts between peers").
- **Trail** — the recording of a session, pointing at its summoner's trail through `summoned_by` and at the trail it continues through `forked_from`.
- **PR watch** — the long-lived `poll-pr` process a session reacts to through [[watch]], one per open pull request it follows.

Terms this design introduces:

- **Delivery plan** — the choices one run of `run-pr` delivers a change under (§3).
- **Review channel** — how findings, replies and code travel between author and reviewer:
  `github`, `talk` or `local` (§3.2).
- **Roles** — the reviewer, the approver and the lander, each with its options (§3.3).
- **Landing method** — `merge` or `push` (§3.4).
- **Gate** — a predicate on the head about to land, chosen by the plan:
  `verdict`, `checks`, `tests` or `approval` (§3.5).
- **Platform rule** — a precondition the remote enforces, which no plan chooses or waives (§3.6).
- **Verdict** — the reviewer's judgement of one named head against one named base commit (§4).
- **Round** — one exchange of findings and answers about a named head (§5.1).
- **Landing fold** — the fold made once the review has nothing open, which writes the landing's footers (§5.2, §9).
- **Turn** — in `talk` and `local`, the span in which the named head belongs to the reviewer (§5.3).
- **Trusted account** — a GitHub account whose comments, reviews and approvals the author acts on (§5.5).
- **Credit** — a trail the plan names whose whole tree counts toward the landing's total (§9.2).
- **Coordination line** — a second footer line, `> coordinated with …`, carrying what a credited landing's total adds beyond its folded commits (§9.3).
- **Choice parameter** — a spell parameter with a closed set of values its body renders on (§7.1).

## 3. The delivery plan

### 3.1 Parameters

`run-pr` resolves its plan from these parameters before it touches anything:

| parameter | values | default |
|---|---|---|
| `base` | a branch | the branch `default-branch` prints |
| `pr` | an open pull request to resume (§3.8) | none |
| `channel` | `github`, `talk`, `local` (choice parameter) | `github` |
| `reviewer` | `summon`, `coordinator`, `others` | `summon` where `may_summon` names a reviewer, else `others` |
| `approver` | `human`, `summoner` | `human` |
| `lander` | `self`, `summon`, `coordinator`, `others` | `self` |
| `landing` | `merge`, `push` (choice parameter) | `merge` |
| `gates` | any subset of `verdict`, `checks`, `tests`, `approval` | §3.5 |
| `placement` | `boxed`, `unboxed`, `join` | `boxed` |
| `trust` | GitHub logins | none |
| `credit` | trail ids | none |

A reviewer is a summon target that is an `eyebro`, by `may_summon`'s is-a reading;
a lander is a summon target that offers [[land]].
`placement` applies to the bros the author summons, the reviewer and a lander.

### 3.2 Review channels

- `github` — the pull request carries the conversation:
  reviews, inline comments, replies and approvals.
  The code under review is the PR head.
  Trusted accounts can take part (§5.5).
- `talk` — the conversation is the reviewer's quest:
  findings are the reviewer's questions on it, and the author's replies answer them.
  The code travels over the review branch, a branch the author pushes to origin and the reviewer fetches;
  fetching it needs a GitHub credential of any account, or a public repository.
- `local` — the `talk` conversation, with the code delivered through the ride and never through a remote:
  - a started reviewer, boxed or unboxed, reads round 1 from its summon's base, the author's `HEAD` with local-only commits included;
    later heads arrive as a `git bundle` the author mints as an artifact and hands over with `bro::quest_share`;
  - a joined reviewer (`placement=join`) reads the shared tree under the turn rules of §5.3.

  `local` needs no credential on either side.

### 3.3 Roles

The reviewer:

- `summon` — the author summons a reviewer bro with talk `worker.say` and `worker.question`, a 14,400-second timeout and the plan's placement;
  its answer is the verdict.
- `coordinator` — `github` only:
  the author's summoner arranges a reviewer and relays its verdict (§5.6).
- `others` — `github` only:
  trusted accounts review on the PR, and an approving review of the head from a trusted account is the verdict (§5.5).

The approver:

- `human` — where questions reach the user (an attended or guided session), their go-ahead to land this change, their own initial request included;
  elsewhere, an approving review of the head from a trusted account GitHub does not mark as a bot.
  A summoner's prompt is never a human's go-ahead.
- `summoner` — the summoner's reply to `approve landing <ref> at <head>?` (§5.6).

The lander:

- `self` — the author runs [[land]].
- `summon` — the author summons a lander bro and hands it the head (§6.4).
- `coordinator` — the author ends at "ready", and its summoner lands (§6.4).
- `others` — a person merges the pull request, and that merge is the approval;
  the author keeps its PR watch running until it reports `merged`.

### 3.4 Landing methods

- `merge` — GitHub's rebase merge of the pull request, through `land-pr`.
- `push` — a fast-forward of `<base>` to the head:
  `git push <remote> HEAD:<base>` where the repository has a remote, `git push . HEAD:<base>` where it has none.
  No push of a landing is ever forced.

### 3.5 Gates

- `verdict` — the reviewer approved this head (§4).
- `checks` — CI concluded green on this head:
  the pull request's checks for `merge`;
  for `push` to GitHub, a run of the repository's CI workflow dispatched on a temporary branch at the head.
- `tests` — the repository's whole local gate passed on this head, in the landing session's workspace, with whatever stages that workspace cannot run reported skipped.
- `approval` — the approver's go-ahead for this head.

Without a `gates` parameter, the gates are `verdict`, `approval`, and `checks` where CI covers the method, otherwise `tests`.
CI covers `merge` where a workflow has ever run on a pull request in the repository (`gh api 'repos/{owner}/{repo}/actions/runs?event=pull_request&per_page=1' --jq .total_count` above zero),
and it covers `push` where the remote is GitHub and the repository's docs name a CI workflow `gh workflow run` accepts.
A `gates` parameter replaces the default whole,
so a landing no human takes part in is always explicit:
a `gates` list without `approval`, or approver `summoner`.
A plan without the `verdict` gate engages no reviewer, whatever its channel,
and its one fold before landing is its landing fold (§9.2).

### 3.6 Platform rules

No plan chooses or waives them:

- a `merge` obeys the base's GitHub rules, which `land-pr` reads off `reviewDecision` and the checks;
- a `push` obeys the remote, and a refusal (a non-fast-forward, or a rule against direct pushes) ends that attempt with the remote's message;
- on GitHub, a base whose rules require a pull request refuses `push` at plan time, read from `gh api repos/{owner}/{repo}/rules/branches/<base>`.

### 3.7 Plan check

`run-pr` refuses a plan before preparing anything when:

- `channel` is `talk` or `local` and `reviewer` is not `summon`;
- a `summon` role has no matching target in `may_summon`;
- a `coordinator` role or approver `summoner` is chosen in a session that was not summoned, or whose quest lacks the `worker.question` that role asks with;
- `placement` is `join` for a reviewer outside `local`, or the session's authority lacks `bro.placement=join`;
- lander `others` is chosen with `landing` other than `merge`;
- lander `summon` is to push onto a base with no remote, and `placement` is not `join`, the one placement whose push reaches the author's repository;
- approver `human` has no way to be reached:
  questions do not reach the user and the plan opens no pull request;
- `checks` is listed in `gates` where CI does not cover the method;
- `landing` is `push` onto a GitHub base whose rules require a pull request.

A refusal names the explicit form that would work.
Where questions reach the user, `run-pr` reports it and stops;
an unattended run raises with it.

### 3.8 Recording and resuming

`run-pr` states its plan in one line when it starts, and records it as a `Delivery:` line:
in the pull request's body where there is one, in a lander's prompt, and in a coordinator's "ready" answer.
For example:

```
Delivery: channel github · reviewer summon · approver human · lander self · landing merge · gates verdict, approval, checks
```

With `pr`, `run-pr` resumes an open pull request:

1. it checks out the pull request's head branch, unless the session already sits on it;
2. it reads the task from the body's `Task:` line and the plan from its `Delivery:` line;
3. a merged pull request gets [[land]]'s bookkeeping, and a closed one a report, and the run stops;
4. otherwise it enters at the review (§5), the PR watch reconciling what arrived while nothing watched (§7.3).

A resume whose recorded `channel` or `landing` differs from the values its cast rendered casts `run-pr` again with the recorded ones.
A verdict does not survive its author's session:
a resumed run starts its reviewer again, which on `github` reconstructs its earlier rounds from the pull request and on `talk` or `local` starts afresh on the current head.
A plan the user or coordinator changes mid-run is checked (§3.7) and recorded again.

### 3.9 Examples

- A `fix` session in this checkout:
  review on `github` by a summoned eyebro, the user's approval, the author landing by `merge`;
  the gates are `verdict`, `approval` and `checks`.
- An orchestrated stage:
  the stage task states `gates=verdict,checks`, and the author merges into the integration branch.
  The integration landing keeps the defaults and adds `credit=<the lead's trail>`, so the user's approval on the pull request is required, as `master` requires anyway.
- A benchmark box with neither Docker nor a remote:
  review over `local` with `placement=join`, landing by `push` onto the local base;
  attended, the gates are `verdict`, `tests` and `approval`, and an unattended run states `gates=verdict,tests`.
- A developer with a single GitHub identity:
  review over `talk` or `local`, landing by `merge`;
  the verdict arrives over the quest, and the approval is the user's go-ahead in the session,
  since GitHub refuses an account's approval of its own pull request.

## 4. Verdicts

### 4.1 When a reviewer approves, and what a verdict says

A reviewer bro approves a head only when all three hold:

- it is the head of the landing fold (§5.2);
- the plan's `checks` concluded green on it, or its `tests` passed, whichever the plan has:
  read off the reviewer's own PR watch on `github`, and on `talk` or `local` as the author's reply reports them, checked wherever the reviewer can read them itself;
- it merges cleanly into the base commit the verdict names:
  GitHub's mergeability for a pull request, a fast-forward or a conflict-free rebase for a push.

Red checks, failed tests or a conflict before then are rounds like any other, with the same reviewer.
Under reviewer `others` the verdict is the approval of §5.5,
and the `checks` gate and the platform rules cover what such an approval does not.

A reviewer bro's verdict is its answer, and its text is what a signed review tag would carry (§14):

```
approved <head sha> on <base sha>
checks: <their state, and where they ran>
<each finding>: <how it settled>
```

A reviewer that cannot approve answers `blocked <head sha> on <base sha>: <reason>` with the same body.
On `github` the reviewer also leaves its verdict on the pull request as an approving or a changes-requesting review,
but the quest's answer is what clears the `verdict` gate, relayed by the coordinator under reviewer `coordinator`.

### 4.2 What a verdict covers

- A verdict and an approval hold for the head they name,
  and for a rebase of that head onto a newer base commit whose `git range-diff` against it shows every commit unchanged (`=`).
- Checks and tests hold for the exact head only.
- Any other change to the head clears every gate:
  an appended fix, a fold, a conflict resolution, someone else's push.
- [[land]] checks the verdict in one step of its own, so a signature check can replace that step later (§14).

### 4.3 When the base moves after a verdict

- The head still merges cleanly, or rebases with every commit unchanged:
  no reviewer is needed, and checks run again on the new head wherever the method requires them (§6.3).
- It needed a conflict resolution, or a fix after CI on the rebased head:
  a successor reviewer reviews only the delta since the approved head.
  - On `github` it reconstructs the review from the pull request:
    its own earlier approval, and what changed since.
  - On `talk` or `local` the author's summon passes the earlier verdict verbatim, as `bro::quest_check` returns it, and delivers the approved head beside the new one.
    The successor reviews the `range-diff` between them and the checks of the new head, and answers a verdict on it.

The author already holds the landing in `talk` and `local`,
so relaying the verdict adds no assumption beyond the author's following its own spell.

## 5. The review conversation

### 5.1 Rounds

1. Before the first round, `run-pr` folds the branch into landing shape with `fold-branch`,
   with provisional footers (§9.2).
2. The reviewer judges a named head and sends its findings:
   on `github`, one review on the pull request;
   on `talk` or `local`, one question on its quest naming the round and the head.
   A round past the 16 KiB message bound goes as an artifact the reviewer mints and names, which its summoner can read.
3. The author answers the round as one batch:
   - a fix is an appended commit made with `git commit --fixup=<the landing commit it serves>`;
   - an answer settles a question;
   - a contested finding gets one re-explanation each way,
     and a thread still deadlocked goes to the user where questions reach them, while elsewhere the reviewer ends `blocked`.

   It verifies what the round changed with the repository's change-scoped selection against the head it last delivered, delivers the new head, and replies naming it;
   on `talk` and `local` the reply also reports the head's checks as far as they have run.
4. Rounds repeat until nothing is open (§5.2).

### 5.2 The landing fold and the reconfirmation

Nothing is open when the reviewer says so, possibly in round 1;
under reviewer `others`, when no trusted thread is open and the head's checks are green.

1. The reviewer says so, and that it waits for the landing fold, then keeps running:
   on `github` with a comment review ("No findings remain; waiting for the landing fold to give the final verdict"),
   on `talk` or `local` with its round question ("Round <n> on <head>: no findings remain. Fold for landing and reply with the folded head and its concluded checks").
   Neither clears a gate.
2. The author makes the landing fold:
   the fixups fold into the commits they serve, the messages are final, and the footers come from §9.
   It delivers the folded head, waits for its checks to conclude (the pull request's for `merge`, the dispatched run for `push` with `checks`) or runs the `tests` gate on it, and replies with the result.
3. The reviewer checks that `git diff <last reviewed head> <folded head>` is empty, judges the new commit split and messages but never the footer lines, waits for green checks and a clean merge, and answers its verdict.
4. A problem with the split or the messages is a finding:
   the author folds again, and the reviewer reconfirms.

Under reviewer `others`, the trusted accounts' approval of the folded head is the verdict.
The reconfirmation and the landing spend tokens after the landing fold, which its footers do not count.

### 5.3 Turns

In `talk` and `local`, the head the author's reply names belongs to the reviewer until its next question or answer.

- During the reviewer's turn the author changes nothing the head names:
  no commit, push or fold, and in a joined review nothing in the tree;
  pull request events that arrive meanwhile wait for the turn to return.
- A joined reviewer starts every turn by checking that `git status --porcelain` prints nothing and `git rev-parse HEAD` prints the named head, and asks the author to fix it otherwise.
- A joined reviewer never checks out, stages, commits, stashes or resets, and writes nothing inside the tree, keeping its scratch files outside it;
  its narrow checks may write files the repository ignores.
- A reviewer recognises a join by its banner's `party: joined` line.
- The verdict's head is the record that nothing unreviewed went in after it.
- A joined reviewer shares the author's container and user,
  so a joined review guards against the author's mistakes, not against an author acting in bad faith (§14).

### 5.4 How each head reaches the reviewer

| channel | the author delivers | the reviewer obtains |
|---|---|---|
| `github` | a push to the pull request | `gh pr checkout <n>`, resetting to `origin/<head branch>` when the branch was rewritten |
| `talk` | a push to the review branch | `git fetch origin <branch>` |
| `local`, started | round 1: the summon's base; later: `git bundle create .git/review/<head>.bundle <range>`, `artifact mint`, `bro::quest_share` | round 1: its workspace `HEAD`; later: `artifact get <ref>`, then `git fetch <bundle> <head>` |
| `local`, joined | a commit in the shared tree, left clean | the shared tree, checked as §5.3 says |

The bundle's range starts at the head the author last delivered, or at the base commit when the branch was rewritten.
A reviewer that cannot obtain the named head, or obtains another commit, asks the author about it and reviews nothing.

### 5.5 Trust on GitHub

The author acts only on comments, reviews and approvals from trusted accounts:

- accounts GitHub grants `write` or `admin` on the repository, `maintain` reading as `write`;
- the reviewers the plan engaged:
  a summoned reviewer asks its summoner `reviewing <pr> as <login>` before it posts anything,
  and a coordinator relays its own reviewer's login (§5.6);
- accounts named in `trust`.

`poll-pr` enforces it (§8),
so an untrusted account's comment never reaches the model, and its approval clears nothing:

- an `others` verdict is an approving review of the head from a trusted account;
- a human approval on GitHub is one from a trusted account GitHub marks as a user rather than a bot.

The reviewer's own watch trusts the pull request's author, accounts with write access, and accounts named to it,
and it adopts only trusted accounts' threads when it reconciles a pull request's history.
A watch still never reports its own account,
so a human sharing the author's account cannot be heard on the pull request and speaks in the session instead.
`talk` and `local` need no trust rule:
only the two ends of the quest speak on it.

### 5.6 Coordinator exchanges

Under reviewer `coordinator`:

1. once the pull request is open, the author asks `review requested: <pr> at <head>`;
   the coordinator summons a reviewer with `[[review <pr> on github]]` and talk `worker.question`, and replies with the login that reviewer announces;
2. once every other gate of the author's has cleared for a head, the author asks `verdict for <pr> at <head>?`;
   the coordinator replies with its reviewer's verdict line, summoning a successor (§4.3) when the head has moved past it.

Under approver `summoner`, the author asks `approve landing <ref> at <head>?` and the summoner replies `approved <head>` or with its refusal.
When the verdict and the approval both come from the summoner, the two questions are one.

### 5.7 Failures

- A reviewer summon denied, or a reviewer that raised, errored or timed out before a verdict:
  the `verdict` gate cannot clear;
  where questions reach the user, the author reports it and stops, and elsewhere it raises with the reason.
  No other reviewer or channel stands in for it unless the plan is changed (§3.8).
- A GitHub review under the author's own account:
  the reviewer refuses before reviewing anything, naming `talk` and `local`, and the author handles it as above.
- A `talk` reviewer that cannot fetch from origin refuses, naming `local`.
- A `talk` or `local` reviewer whose quest lacks `worker.question` refuses.
- A deadlocked thread ends the review `blocked` (§5.1).
- Untrusted feedback is never acted on;
  the author reports the `untrusted:` events in its answer, and to the user where questions reach them.
- A PR watch that ends with `watch_failed` is reported, or raised with when unattended;
  `run-pr` with `pr` resumes the pull request after the cause is fixed.

## 6. Landing

### 6.1 [[land]]

[[land]] runs in whichever session the lander is.
It takes the landing method,
and in a lander other than the author also the pull request (for `merge`) or the base (for `push`), and the head.

1. It checks out the head and confirms its workspace sits on it;
   for `merge`, the pull request's head must be that commit.
2. It re-checks what it can see itself:
   the platform rules, and the checks where the plan has the `checks` gate.
3. It takes the verdict, the approval and the tests result from its caller:
   the gates its own run retained where the author lands, otherwise the prompt that summoned it.
   The verdict check is the one step §4.2 reserves.
4. It lands by the method (§6.2, §6.3) and does the bookkeeping (§6.5).

### 6.2 `merge`

PRs stacked on the branch are retargeted to the base first,
then `land-pr` merges as it does today:
its preconditions (an open pull request, `reviewDecision`, unchecked test-plan boxes, the worktree on the reviewed head, rebase merging allowed),
its wait for the checks, the rebase merge with `--match-head-commit`, and the deletion of the remote branch.

### 6.3 `push`

1. Fetch the base;
   if it moved, rebase onto it, keeping the verdict and the approval only where §4.2 allows and otherwise returning to review (§4.3).
2. On GitHub with the `checks` gate, a head the checks have not run on gets them:
   push it to `land/<head sha>`, `gh workflow run <workflow> --ref land/<head sha>`, and wait for that run to conclude;
   a red run sends the change back to review (§4.3).
3. With the `tests` gate, a head the gate has not passed on gets the whole local gate.
4. `git push <remote> HEAD:<base>`, or `git push . HEAD:<base>` without a remote.
   A non-fast-forward refusal returns to step 1;
   any other refusal is reported, or raised with when unattended.
5. Delete the temporary branch and the review branch on the remote.

A remote other than GitHub gets neither the rules pre-check of §3.6 nor a dispatched run:
its refusal is its rule, and the plan check keeps `checks` off it.
A pull request whose head a push landed is expected to read as merged on GitHub (§16);
one that stays open is closed with a comment naming the pushed commits.

### 6.4 Handing the landing over

- Lander `summon`:
  the author summons a lander based on its own `HEAD`, or joined where the plan says so,
  with `[[land <pr or base> at <head> by <method>]]`, the `Delivery:` line, the cleared gates and the task's URL in its prompt;
  it relays the lander's answer and ends.
- Lander `coordinator`:
  the author answers `ready <ref> at <head>` with the `Delivery:` line, the cleared gates, and how the head is reached
  — the pull request, a pushed branch, or a bundle artifact its summoner can share onward —
  and ends.
- Lander `others`:
  the author reports the change ready to merge and keeps its PR watch running until `merged`, then does the bookkeeping.

### 6.5 Bookkeeping

Whichever session lands records it as [[land]] does today:
the `merged` task comment naming the pull request or the pushed commits, the task closed to done unless the request keeps it open, and the deploy handoff where the change needs one.
A lander is told the task's URL in its prompt.

## 7. Spells

### 7.1 Choice parameters

A spell parameter may declare a closed set of values for its body to render on.

- In a spell's frontmatter, a `parameters` value may be an object instead of a description:
  `{"description": "…", "choices": ["github", "talk", "local"], "default": "github"}`.
  An optional parameter's object carries a `default`, and a required parameter's carries none.
- Loading refuses an empty or repeated choice, a default outside the choices, and a name that collides with a fact spell bodies render with (`harness`, `creds`, `may_summon`, `hold`, `features`, `true`, `false`, and the harness's own facts).
- `SpellTool`'s schema and the cast roster both expose a choice parameter as an `enum` with its `default`.
- A call passing a value outside the choices is refused before rendering, and an omitted optional one takes its default.
- `get_spell_body` receives the call's choice values, and the body renders with each as a string variable whose domain is its choices,
  so `{{iff #channel = github}}…{{eliff #channel = talk}}…{{end}}` renders one branch and a misspelt literal fails every rendering.
- `call_text`'s `# Arguments` section lists every passed value as today, and each defaulted choice as `<name>: <value> (default)`.
- The cast path and the `spell::` tools both render through `_render_spell_call`,
  so a session without `mu`, whose `[[…]]` markers call the spell's own tool, renders the same way, and nothing returns unrendered.
- A test renders every shipped spell under every combination of its choices and holds each rendering to `BYTE_LIMIT`.

### 7.2 `run-pr`

Its parameters are §3.1's, `channel` and `landing` being choice parameters;
it requires [[land]], [[follow pr]] and [[watch]].
Its sections:

1. the arguments and the plan, its check and its `Delivery:` line (§3);
2. resuming with `pr` (§3.8);
3. preparing:
   the survey, the formatter and the policy audit, the docs, the commit splits and messages, the commits, the rebase, the fold before review, the change-scoped gate and the scope check, as today and without the accounting text;
4. publishing:
   a pull request where `channel` is `github` or `landing` is `merge`, with its `PR opened` task comment, its review link, and [[follow pr]] armed on it;
   a review branch for `talk`;
5. the review handoff of the plan's channel, the only branch that renders:
   on `github`, summoning the reviewer, running the coordinator exchange, or waiting on trusted accounts;
   on `talk` and `local`, the summon with its talk rights, the deliveries of §5.4 and the turns of §5.3;
6. the author's half of the rounds and the landing fold (§5.1, §5.2);
7. the gates:
   retaining them per head (§4.2), collecting the approval from the approver, and handing the landing over once all clear (§6.4).

### 7.3 `follow-pr`

A new spell taking `pr`, cast wherever a pull request is open, by `run-pr` and by a resume;
it requires [[watch]] and [[land]].

1. Reconcile what arrived while nothing watched, from `gh` state, trusted accounts only:
   today's `resume-pr` step 4.
2. Arm `poll-pr <owner>/<repo> <n> --trust-file <path>` through [[watch]].
3. React to every event:
   trusted feedback is a round (§5.1);
   `checks` and `conflicts` feed the gates and the verdict (§4);
   `pushed` from elsewhere clears every gate retained for the old head, and the branch resets onto the new one before the next commit;
   `untrusted:` is noted for the report (§5.7);
   `merged` and `closed` end the watch with bookkeeping or a report;
   `disconnected` and `reconnected` change nothing;
   `watch_failed` is §5.7's.

### 7.4 `land`

`landing` is a choice parameter, and `pr`, `head` and `base` are optional;
an author landing its own change passes none of them.
Its sections follow §6:
the head and what [[land]] re-checks, the one rendered method, the handover's receiving side, and the bookkeeping.
The no-pull-request review rounds its current text runs go, since review is `run-pr`'s.

### 7.5 The eyebro's `review`

`review` is the one entry point a summoner casts, taking `channel` (a required choice parameter) and `change`;
it replaces `review-pr` and requires [[review diff]] and [[watch]].

- The `github` branch is today's `review-pr`, with these changes:
  - its precondition refuses a shared identity before anything else, naming `talk` and `local`;
  - it announces its login to its summoner (§5.5);
  - it reconciles and adopts trusted accounts' threads only, under its own trust file;
  - it expects appended rounds and one landing fold, and announces that it waits for that fold (§5.2);
  - checks and conflicts are part of its verdict instead of information (§4.1);
  - it approves only the landing fold's head, green and merging cleanly, and answers in §4.1's form;
  - a deadlock ends it `blocked` instead of raising.
- The `talk` and `local` branch is the round loop over its quest:
  it obtains each head as §5.4 says, keeps the turns and the joined rules of §5.3, asks each round as a question, and answers its verdict.
- `review-diff` stays the judging method both branches run, and the standalone findings report.

`change` names the pull request on `github`;
on `talk` and `local`, the base commit and the head as `<base>..<head>`, the review branch on `talk`, and for a successor the approved head the delta starts from.

### 7.6 `orchestrate`

- Stage tasks state their delivery explicitly.
  The line the review-and-plan phase writes by default reads:
  "land via [[run pr]] with its base argument set to `<integration branch>`, reviewed on github by a reviewer you summon, with gates verdict and checks — the human approval comes at the integration landing".
- The integration phase's prompt passes `credit=<the lead's trail id>`, read off the lead's banner as it summons the phase, and otherwise keeps the defaults.
- The coordinator roles are the lead's option per stage:
  it answers §5.6's exchanges, and a stage that answers "ready" lands through a lander the lead summons with `[[land …]]`.
- A phase whose plan summons a reviewer or a lander is granted that target under the name `may_summon` gives.
- References move to [[follow pr]] and to `run-pr`'s `pr` argument, which the integration phase's re-fire already relies on.

### 7.7 Other references

- `fix`, `bump-bro` and `reflect` keep handing off to [[run pr]],
  and their references to its step numbers become references to its section names.
- `rebase_conflicts.md` names `run-pr`'s sections instead of its step numbers.
- The `dev` persona's prompt keeps "hand off to [[run pr]]".
- The `dev` persona's spells drop `resume-pr` and add `follow-pr`.

### 7.8 Sizes

Estimated renderings, which the test of §7.1 holds to the window once the spells are written:

| spell | per rendering |
|---|---|
| `run-pr` | about 26,000 characters |
| `follow-pr` | about 11,000 characters |
| `land` | about 9,000 characters |
| `review` on `github` | about 16,000 characters |
| `review` on `talk` or `local` | about 9,000 characters |

## 8. `poll-pr`

- `--trust-file <path>` names a file of trusted logins, one per line, which the watch reads again every cycle,
  so a session extends it while the watch runs, as an announcement arrives.
- Every other account is checked for write access through `repos/{owner}/{repo}/collaborators/{login}/permission`, once per login per watch.
- `comment` and `review` events fire only for trusted accounts, replacing today's rule of the pull request's parties (its author, the repository's owner, anyone who reviewed).
- An untrusted account's comment or review fires one bodiless `untrusted:` event with `user`, `kind` and `url`,
  and is held:
  once its account becomes trusted, the held items fire as their ordinary events, in order.
- A `review` event also carries `user_type`, `User` or `Bot`, as GitHub reports it.
- The watch's own account stays filtered out, as today.

## 9. Token accounting

### 9.1 What stays

The footer's format, the `commit-msg` and `post-commit` hooks with the usage file and the baseline state behind them, `commit-footer`, `usage-report` and `git golc` stay as they are,
except that the two reports read every footer line of a message (§9.4).
Working commits keep their per-commit footers.

### 9.2 The landing's total

`fold-branch --plan <path> [--base <branch>] [--credit <trail id>]…` asks the usage-totals mission (§9.5) for the total of the trails that contributed to the landing:

- the folding session's own trails;
- everything summoned under them, recursively:
  reviewers, landers, helpers, and `mu` calls once those record their summoner;
- every credited trail's whole tree.

Each trail counts once, however many of those it is reached through.
Orchestrate's integration landing credits the lead's trail (§7.6),
whose tree holds the design, the planning, the coordination, every stage with its reviewers, and the integration session itself.
The fold before review asks too, and the footers it writes are provisional:
the landing fold replaces them.
What sessions spend after the landing fold is not counted.

### 9.3 Splitting the total over the landed commits

- Each landed commit's footer line carries the sum of the footers of the commits folded into it.
- The rest
  — the total minus every folded commit's footer, per model label and token class —
  goes on the last landed commit:
  - without credits, added into its footer line;
  - with credits, on a coordination line of its own, `> coordinated with <agents> | <model>: …`, its agents being the ones the mission reports.
- A rest below zero for any label and class refuses the fold:
  some folded commit was made by a session the total does not count.
- The baseline promotion after a fold stays,
  so a commit made after a fold carries only what its session spent since,
  and a later fold reads the earlier landing's lines, the coordination line included, as folded footers.

For example, an integration landing that folds two stage commits carrying 3M and 2M tokens into one commit, under a lead's tree totalling 9M,
writes a footer line of 5M and a coordination line of 4M.
Rearranging the commits moves tokens between the landed commits' lines and never into or out of the landing's total.
The last landed commit carries the coordination line rather than an empty commit of its own,
because GitHub's rebase merge drops a commit that was empty to begin with (§16).

### 9.4 The reports

`bro.llm.usage.parse_footer` and `git golc`'s parser read every footer line of a message, `created with` and `coordinated with` alike, and sum them;
`usage-report` and `git golc` change in nothing else.

### 9.5 The usage-totals mission

The landing fold depends on a host-side, non-agentic mission whose own design is in the coordinator's plan (§14).
What this design requires of it:

- its input is trail ids, the folding session's current trail and the credits;
- its output is the total of every trail in their trees, each counted once, per model label and token class in the footer's vocabulary, with the agents behind them;
- the bro requesting it needs no trail read rights.

Its own design settles its authority, the tree it walks (forks, summons, `mu` calls), how a session continuing another's work counts its predecessor, its failures, and what `fold-branch` does where no mission can be reached.

## 10. Personas

- `bro-eyebro` is deleted, with its entry point and its tests.
  Its pinned `github: True` keeps it from running `local` without a credential,
  and its framework prompt and man pages duplicate what the reviewer reads from the checkout, in the version under review.
  The generic `eyebro` takes its place:
  `may_summon` grants `@eyebro`, and the host configuration's picks for it, `github+eyebro` among them, move to `eyebro`'s entry.
- `eyebro`'s spells are `review-diff` and `review`;
  its `github` feature stays gated on the credential, and the `github` branch names what is missing without it, as `review-pr` does today.
- `lead` and `dev` keep their tools.

## 11. Rollout

- The landing fold's accounting goes live only with the usage-totals mission it asks:
  the plan orders that mission's design and implementation before the accounting stage lands.
- Everything else goes live as installations re-sync;
  a session keeps the runtime it launched under, with its old spells and hooks, until it is relaunched.
- The footer format does not change, so a history mixing old and new landings reads the same way;
  only landings folded under the new rule carry coordination lines, which the new reports sum.
- Operator steps:
  moving the `bro-eyebro` entries of the host configuration to `eyebro`, granting `@eyebro` where `@bro-eyebro` was granted,
  and granting `bro.placement=join` to the bros that run joined reviews.

## 12. Testing

- Choice parameters:
  the load refusals, the `enum` and `default` in both schemas, the refusal of a value outside the set, defaulted rendering, the `# Arguments` listing,
  and every shipped spell rendered under every combination within `BYTE_LIMIT`.
- `fold-branch`, against a stubbed mission:
  a one-commit landing, a several-commit landing, a credited landing writing a coordination line, a rest below zero refused, and the tree still proven unchanged.
- The footer parsers summing several lines, in `bro.llm.usage` and `git golc`.
- `poll-pr`:
  the trust file read again each cycle, write access read once per login, bodiless `untrusted:` events, held items fired once trusted, `user_type`, and the own account still filtered.
- [[land]]'s `push`:
  a local base through `git push .`, a non-fast-forward refusal, the `range-diff` rule after a rebase, and the GitHub dispatch stubbed.
- The persona rosters and spell requirements of `dev`, `eyebro` and `lead`.

## 13. Deletions

- `dev/bro/dev/spells/resume-pr.md`, folded into `run-pr` (§3.8, §7.3).
- `dev/bros/eyebro/spells/review-pr.md`, folded into `review` (§7.5).
- `run-pr`'s "Hand the review to the eyebro" and its review-event handling, moved to §7.2's sections and to `follow-pr`.
- `land`'s review rounds for landings without a pull request.
- `local/bros/bro_eyebro/` and its `bro.bros.bro` entry point (§10).
- `commit_footer.group_footers`'s session remainder, replaced by the rest of §9.3.

## 14. For the coordinator's plan

- **The usage-totals mission and `mu` calls' summoner provenance**:
  their own design, which §9's accounting depends on (§9.5).
- **Signed review tags**:
  a follow-up.
  The reviewer signs an annotated tag at the approved head carrying §4.1's text, with a key the author cannot reach;
  the lander, and a server's pre-receive hook or, on GitHub, a required status check on the pull request's head, verify it against reviewer keys kept on the base branch.
  It needs a signing-key credential, an exception to the reviewer's rule of writing nothing for its own review tags, the tag's transport (pushed by the reviewer, or handed over as an artifact), and the check.
  §4.2's verdict step is where verification plugs in;
  a joined reviewer cannot sign anything that proves a review against the author (§5.3).

## 15. Decision record

The owner settled every decision below on 2026-11-08 and 2026-11-09.

| # | Decision | Applied in |
|---|---|---|
| 1 | The review channel is an explicit choice of `github`, `talk` or `local`, and `local` covers a started reviewer (the summon's base, then bundles) and a joined one (the shared tree, in turns) | §3.2, §5.3, §5.4 |
| 2 | `[[review]]` is the reviewer's single entry point, replacing `review-pr`, and the author chooses the channel through `run-pr`'s `channel` | §7.5 |
| 3 | Choice parameters are a core spell addition both the cast and the `spell::` tools render with, rather than a sub-spell per channel | §7.1 |
| 4 | The defaults are channel `github` and landing `merge` | §3.1 |
| 5 | The reviewer, the approver and the lander are roles chosen per plan, so author, reviewer and lander can be three bros | §3.3, §6.4 |
| 6 | A landing requires a human's approval by default, and landing without one is only ever explicit | §3.5 |
| 7 | Gates are chosen independently of the landing method, and platform rules are never chosen nor waived | §3.5, §3.6 |
| 8 | `push` lands onto any branch of any remote, or onto a local base where there is no remote, with GitHub's steps only on GitHub | §3.4, §6.3 |
| 9 | What the reviewer approves is what lands: fixes are appended, the branch folds before review and once more when nothing is open, and the reviewer stays alive to reconfirm the landing fold, saying it waits for it | §5.1, §5.2 |
| 10 | Checks and mergeability are part of the verdict | §4.1 |
| 11 | A verdict names its head and base commit and holds only for that head or a rebase of it with every commit unchanged; a moved base needing changes gets a successor that reviews only the delta, given the earlier verdict | §4.2, §4.3 |
| 12 | On GitHub the author acts only on trusted accounts (write access, announced reviewers, named accounts), enforced by `poll-pr` so that untrusted text never reaches the model | §5.5, §8 |
| 13 | `resume-pr` folds into `run-pr`'s `pr`, and `follow-pr` holds the PR watch | §3.8, §7.3 |
| 14 | Footers, hooks and reports stay; a landing's footers come from a host-side mission's total over the contributing trails, no bro holds trail read rights, and spend after the landing fold is not counted | §9 |
| 15 | The lead is counted through `credit` at the integration landing | §7.6, §9.2 |
| 16 | A landing's total splits into the folded footers plus a rest on the last landed commit, a credited rest going on a coordination line rather than an empty commit | §9.3 |
| 17 | The usage-totals mission and `mu` provenance are a design of their own in the coordinator's plan | §9.5, §14 |
| 18 | `bro-eyebro` is dropped in favour of the generic `eyebro` | §10 |
| 19 | Orchestrate's stages state `gates=verdict,checks`, the human approval coming at the integration landing | §7.6 |
| 20 | Signed review tags are a follow-up, and the verdict is shaped for them | §4.1, §14 |

## 16. Assumptions and evidence

Verified while designing:

- `master`'s ruleset (`gh api repos/dzhioev/bro/rulesets/20695416`) forbids deletion and non-fast-forward updates, and requires linear history and the `tests` check;
  it also requires a pull request with one approval, the last push approved by someone other than its pusher, and squash or rebase merges.
- GitHub's rebase merge always updates the committer and writes new commit ids, and drops commits that were empty to begin with
  ([About merge methods on GitHub](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/about-merge-methods-on-github)).
- The collaborator-permission endpoint answers under this checkout's GitHub App installation token:
  `dzhioev` reads `admin`, the App account `bbbbro[bot]` reads `none` with type `Bot`, and an unrelated account reads `read`.
  An App account therefore never qualifies through write access.
- A started child bases on its summoner's `HEAD` with local-only commits, while a joined child uses the party's tree with its uncommitted state (`bro/reference/ride.md`, "Summoning another bro").
- A mint is readable by its minter and its summoners, `share` and `bro::quest_share` hand a ref down, and a manual child receives no share (`bro/reference/ride.md`, "Sharing artifacts between peers").
- A quest message is bounded at 16 KiB (`bro/broker/journal.py`) and an answer at 64 KiB (`bro/run_lifecycle.py`).
- A spell body renders today without its call's arguments (`BaseBro.get_spell_body`), and the `spell::` tools already carry every declared parameter in their schema (`bro/spells.py`, `SpellTool`).
- `run-pr.md` is 29,976 bytes on `master`, and renders to 29,950 (the task's findings).
- `poll-pr` reports comments and reviews from the pull request's parties, a stranger who reviews becoming one.
- `mu` calls record no trail (`bro/llm/mu.py`), and `parse_footer` reads only the first footer line of a message (`bro/llm/usage.py`).

Unverified:

- That GitHub marks a pull request merged when its head is pushed straight onto its base (`github` with `push`).
- That a direct push to a base requiring checks is accepted once the same commit passed them on a dispatched run.
- The renderings of §7.8, which the test of §7.1 settles.
- That the reviewer's 14,400-second timeout covers the CI waits a verdict now includes.
