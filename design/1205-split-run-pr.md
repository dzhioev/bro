# Design: review channels, roles and landing gates for `run-pr`

This file is the specification of [#1205](https://github.com/dzhioev/bro/issues/1205).
It lives on the branch `design/1205-split-run-pr`, which is never merged:
the pull request that carries it is a design review only.

## 1. Goal, scope and constraints

Goal: `run-pr` takes a change through an explicitly chosen review and an independently gated landing, with its roles and the accounts it listens to made explicit.
The task's `## Goal` states the outcomes the verification phase checks.

What it enables, for example:

- A developer whose reviewer bro would share their GitHub identity gets a verdict over the reviewer's quest (`talk` or `local`) instead of a summon that fails.
- A benchmark box with neither Docker nor a remote reviews a change with a joined reviewer and lands it onto its local base branch, with no credential at all.
- A coordinator summons the developer and the reviewer as two unlinked children and holds the verdict itself.
- A landing requires a human's approval, a bro's verdict, CI, the local gate, or any subset stated explicitly, whichever method lands it.
- A comment from an account the review does not trust never reaches the author's model.

The owner settled the design's decisions from 2026-11-08 to 2026-11-10;
§15 records each one and the section that applies it.

Out of scope, each with where it belongs:

- what a review judges, which stays `review-diff`'s;
- the substance of preparing a branch (survey, formatter, policy audit, docs, commit splits, messages, rebase, the change-scoped gate), which `run-pr` keeps as it is;
- deploys, which stay `land`'s handoff;
- token accounting, whose footers stay as they are (§9), counting every session that contributed to a landing being [#1227](https://github.com/dzhioev/bro/issues/1227)'s;
- signed review tags, [#1230](https://github.com/dzhioev/bro/issues/1230), a follow-up the verdict of §4 is shaped for;
- live-LLM runs of the routes §12 names, which are [#1228](https://github.com/dzhioev/bro/issues/1228)'s.

Constraints:

- framework code stays consumer-neutral, so the core addition (§7.1) names no development concept;
- core imports no other workspace member;
- backward compatibility is not a constraint (`AGENTS.md`, "Conventions");
- a violated assumption fails fast (the development style policy);
- every rendering of a spell fits the 30,000-character tool-result window (`bro.base.text_window.BYTE_LIMIT`, counted in characters) that both `bro::cast` and the `spell::` tools return through, past which a result pages;
- GitHub's rebase merge writes new commit ids and drops commits that were empty to begin with (§16);
- this repository's `master` requires, through its ruleset (§16):
  a pull request with one approval from an account its rules count, and the last push approved by someone other than its pusher;
  an extra approval for changes no GitHub account is credited with;
  linear history, and the `tests` check.

## 2. Concepts

Terms the repository already defines, as this design uses them:

- **Change** — the commits a branch adds over its base, landing together.
- **Head** — the commit at the tip of a change;
  verdicts, checks and approvals are each about one head.
- **Base** — the ref a change lands on, written `<base>`:
  a remote's branch such as `origin/master`, or a local branch of the repository the author works in, written `refs/heads/main`, or `main` where the repository has no remote (§3.1);
  the **base commit** is the tip of the base a verdict or check was made against.
- **Landing shape** — one commit per logical change, each with the message the base will carry (`run-pr`, "Fold the branch").
- **Fold** — `fold-branch` rewriting the branch into landing shape, proving the final tree unchanged, and summing the folded commits' footers onto the commits that land.
- **Footer** — the token-accounting line `> created with <agents> | <model>: ↑(<input> <cache-write> <cache-read>) ↓<output>, …` owned by `bro.llm.usage`, appended to agent commits by the `commit-msg` hook.
- **Quest**, **talk rights** and **placement** — a summon's conversation, its four rights, and `boxed`, `unboxed` or `join` (`bro/reference/ride.md`, "Summoning another bro").
- **Artifact** — a content-addressed file or directory passed between peers of one ride (`bro/reference/ride.md`, "Sharing artifacts between peers").
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
- **Successor** — a reviewer that takes over a review from one that cannot finish it (§4.3, §5.7).
- **Round** — one exchange of findings and answers about a named head (§5.1).
- **Landing fold** — the last fold before landing, made once the review has nothing open (§5.2).
- **Turn** — in `talk` and `local`, the span in which the named head belongs to the reviewer (§5.3).
- **Trusted account** — a GitHub account whose comments, reviews and approvals the author acts on (§5.5).
- **Choice parameter** — a spell parameter with a closed set of values its body renders on (§7.1).
- **Route** — one end-to-end path through a plan's choices, as §12 names them.

## 3. The delivery plan

### 3.1 Parameters

`run-pr` resolves its plan from these parameters before it touches anything:

| parameter | values | default |
|---|---|---|
| `base` | a ref: a remote's branch, or a local branch | `origin/<the branch default-branch prints>`, where the repository has an `origin` |
| `pr` | an open pull request to resume (§3.8) | none |
| `channel` | `github`, `talk`, `local` (choice parameter) | `github` |
| `reviewer` | `summon`, `coordinator`, `others` | `summon` where a reviewer is granted, else `others` |
| `approver` | `human`, `summoner` | `human` |
| `lander` | `self`, `summon`, `coordinator`, `others` | `self` |
| `landing` | `merge`, `push` (choice parameter) | `merge` |
| `gates` | any subset of `verdict`, `checks`, `tests`, `approval` | §3.5 |
| `placement` | `boxed`, `unboxed`, `join` | `boxed` |
| `trust` | GitHub logins | none |

`<base>` is used as given, and never guessed:
`origin/master` lands on origin's `master`, `upstream/main` on the `upstream` remote's `main`, and `refs/heads/main` on the local branch `main`.
A base whose first component names one of the repository's remotes is that remote's branch, and a step that needs its current tip fetches that remote first;
a base written `refs/heads/<branch>` is a local branch, and nothing is fetched for it.
A bare name such as `main` is a local branch only in a repository without remotes, where nothing else can be meant;
in a repository with a remote it refuses, naming `<remote>/main` for each remote that has the branch and `refs/heads/main`,
so that a `master` meant as origin's never fast-forwards a stale local `master` and leaves the change unpublished.
A base that names no ref refuses the same way:
`integration/1205-x` with no such local branch is refused, naming `origin/integration/1205-x` where that exists.
`fold-branch`, `check-head` and `land-push` resolve a base through one shared function that applies these rules.
No step assumes an `origin`, nor anything about how the repository was prepared:
`default-branch` serves only the default, so a repository without an `origin` names its base.

A remote's name exists only in the checkout that defines it, so a base travels to another session with its remote's URL, which the `Delivery:` line records beside it (§3.8).
The receiving session names the base through its own remote at that URL, adding one under a name its checkout does not use where it has none;
a joined session shares the author's checkout and its remotes, and adds nothing.

A reviewer is a granted summon target that answers to `eyebro`, and a lander one that answers to `dev`, whose roster carries [[land]].
A spell tests the grant with `#may_summon contains eyebro`, which holds for `eyebro` and for every bro deriving from it,
and the summon names the granted bro itself, since the host admits exactly the names granted (§16).
`placement` applies to the bros the author summons, the reviewer and a lander.

### 3.2 Review channels

- `github` — the pull request carries the conversation:
  reviews, inline comments, replies and approvals.
  The code under review is the PR head.
  Trusted accounts can take part (§5.5).
- `talk` — the conversation is the reviewer's quest:
  findings are the reviewer's questions on it, and the author's replies answer them.
  The code travels over the review branch, a branch the author pushes to the base's remote and the reviewer fetches;
  fetching it needs a GitHub credential of any account, or a public repository.
- `local` — the `talk` conversation, with the code delivered through the ride and never through a remote:
  - a started reviewer, boxed or unboxed, reads round 1 from its summon's base, the author's `HEAD` with local-only commits included;
    later heads arrive as a `git bundle` the author mints as an artifact and hands over with `bro::quest_share`;
  - a joined reviewer (`placement=join`) reads the shared tree under the turn rules of §5.3.

  `local` needs no credential on either side.

### 3.3 Roles

The reviewer:

- `summon` — the author summons a reviewer bro with talk `worker.say` and `worker.question`, a 14,400-second timeout and the plan's placement;
  its answer is the verdict, and a reviewer that times out hands the review to a successor (§5.7).
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

- `merge` — GitHub's rebase merge of the pull request, through `land-pr` (§6.2);
  the base is a branch of a GitHub remote, whose repository the pull request lives on, and every `gh` call and `land-pr` is bound to that repository rather than to the one `gh` would pick for the checkout.
- `push` — a fast-forward of `<base>` to the head, through `land-push` (§6.3):
  a push to the base's remote, or an update of the local branch the base names.
  No push of a landing is ever forced.

### 3.5 Gates

- `verdict` — the reviewer approved this head (§4).
- `checks` — CI concluded green on this head:
  the pull request's checks for `merge`;
  for `push` onto a GitHub remote's branch, a run of the repository's CI workflow that `check-head` dispatches on a temporary branch at the head (§6.3).
- `tests` — the repository's whole local gate passed on this head, in the landing session's workspace, with whatever stages that workspace cannot run reported skipped.
  Before a handover, the author's run of the gate on the head is the reviewer's evidence for it (§4.1), and a lander other than the author runs it again (§6.1).
- `approval` — the approver's go-ahead for this head.

Without a `gates` parameter, the gates are `verdict`, `approval`, and `checks` where CI covers the method, otherwise `tests`.
CI covers `merge` where a workflow has ever run on a pull request in the repository (`gh api 'repos/{owner}/{repo}/actions/runs?event=pull_request&per_page=1' --jq .total_count` above zero),
and it covers `push` where the base's remote is GitHub and the repository's docs name its CI workflow, whose triggers include `workflow_dispatch`.
A `gates` parameter replaces the default whole,
so a landing no human takes part in is always explicit:
a `gates` list without `approval`, or approver `summoner`.
A plan without the `verdict` gate engages no reviewer:
its `channel`, `reviewer` and a reviewer's `placement` are moot, the plan check skips the rules about them (§3.7), and the `Delivery:` line records `reviewer none`;
its one fold before landing is its landing fold.

### 3.6 Platform rules

No plan chooses or waives them:

- a `merge` obeys the base's GitHub rules, which `land-pr` reads off `reviewDecision` and the checks;
- a `push` obeys the base's remote, and a refusal (a non-fast-forward, or a rule against direct pushes) ends that attempt with the remote's message;
- on GitHub, a base whose rules require a pull request refuses `push` at plan time, read from `gh api repos/{owner}/{repo}/rules/branches/<base>`.
  That endpoint reports rulesets only:
  classic branch protection, which takes admin access to read, is enforced by the remote's refusal alone.

### 3.7 Plan check

`run-pr` refuses a plan before preparing anything when any of these holds, the rules about the channel and the reviewer applying only to a plan with the `verdict` gate:

- `channel` is `talk` or `local` and `reviewer` is not `summon`;
- a `summon` role has no granted target that answers to its bro (§3.1);
- a `coordinator` role or approver `summoner` is chosen in a session that was not summoned, or whose quest lacks the `worker.question` that role asks with;
- `placement` is `join` for a reviewer outside `local`, or the session's authority lacks `bro.placement=join`;
- lander `others` is chosen with `landing` other than `merge`;
- `base` is omitted in a repository without an `origin`, is a bare name in a repository with a remote, or names no ref (§3.1);
- `channel` is `talk` and the base is a local branch, which no remote carries to the reviewer;
- `landing` is `merge` and the base is not a branch of a GitHub remote;
- the base is a local branch and the lander works in another repository, which cannot update it:
  lander `summon` with a `placement` other than `join`, or lander `coordinator` where the author is not a joined member of its coordinator's party;
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
A base on a remote carries that remote's URL in parentheses.
For example:

```
Delivery: base origin/master (https://github.com/dzhioev/bro.git) · channel github · reviewer summon · approver human · lander self · landing merge · gates verdict, approval, checks
```

With `pr`, `run-pr` resumes an open pull request:

1. it checks out the pull request's head branch, unless the session already sits on it;
2. it reads the task from the body's `Task:` line and the plan from its `Delivery:` line;
   the base is the pull request's base branch on its repository's remote, whatever the line records, and a body without the line resumes its other choices under the defaults of §3.1;
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
  The integration landing keeps the defaults, so the user's approval on the pull request is required, as `master` requires anyway.
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
- every one of `checks` and `tests` the plan has holds on it, the checks concluded green and the tests passed:
  the checks read off the reviewer's own PR watch on `github`, and on `talk` or `local` as the author's reply reports them, and the tests as the author's run on the head reports them, each checked wherever the reviewer can read it itself;
  a plan with neither leaves the verdict to the head and its mergeability;
- it merges cleanly into the base commit the verdict names:
  GitHub's mergeability for a pull request, a fast-forward or a conflict-free rebase for a push.

Red checks, failed tests or a conflict before then are rounds like any other, with the same reviewer.
Under reviewer `others` the verdict is the approval of §5.5,
and the `checks` gate and the platform rules cover what such an approval does not.

A reviewer bro's verdict is its answer, and its text is what a signed review tag would carry (#1230):

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
- [[land]] checks the verdict in one step of its own, where a signature check can plug in later (#1230);
  for `push`, `land-push` holds the head to it mechanically (§6.3).

### 4.3 When the base moves after a verdict

- The head still merges cleanly, or rebases with every commit unchanged:
  no reviewer is needed, and checks run again on the new head wherever the method requires them (§6.3).
- It needed a conflict resolution, or a fix after CI on the rebased head:
  a successor reviews only the delta since the approved head.
  - On `github` it reconstructs the review from the pull request:
    its own earlier approval, and what changed since.
  - On `talk` or `local` the author's summon passes the earlier verdict verbatim, as `bro::quest_check` returns it, and delivers the approved head beside the new one.
    The successor reviews the `range-diff` between them and the checks of the new head, and answers a verdict on it.

The author already holds the landing in `talk` and `local`,
so relaying the verdict adds no assumption beyond the author's following its own spell.

## 5. The review conversation

### 5.1 Rounds

1. Before the first round, `run-pr` folds the branch into landing shape with `fold-branch`, which sums the footers as a fold does today (§9).
2. The reviewer judges a named head and sends its findings:
   on `github`, one review on the pull request;
   on `talk` or `local`, one question on its quest naming the round and the head.
   A round past the 16 KiB message bound goes as an artifact the reviewer mints and names, which its summoner can read.
3. The author answers the round as one batch:
   - a fix is an appended commit made with `git commit --fixup=<the landing commit it serves>`;
   - an answer settles a question;
   - a contested finding gets one re-explanation each way,
     and a thread still deadlocked goes to the user where questions reach them, while elsewhere the reviewer ends `blocked`.

   It verifies what the round changed with the repository's change-scoped selection against the head it last delivered, delivers the new head, and replies naming it, an unchanged head being delivered by naming it alone (§5.4);
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
   the fixups fold into the commits they serve, the messages are final, and `fold-branch` sums the folded footers onto the landed commits (§9).
   It delivers the folded head, waits for its checks to conclude where the plan has `checks` (the pull request's for `merge`, a `check-head` run for `push`), runs the `tests` gate on it where the plan has `tests`, and replies with the results.
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
- A joined reviewer starts every turn by checking that `git --no-optional-locks status --porcelain` prints nothing and `git rev-parse HEAD` prints the named head, and asks the author to fix it otherwise;
  the option keeps the check from refreshing the index the two share.
- A joined reviewer never checks out, stages, commits, stashes or resets, and writes nothing inside the tree, keeping its scratch files outside it;
  its narrow checks may write files the repository ignores.
- A reviewer recognises a join by its banner's `party: joined` line.
- The verdict's head is the record that nothing unreviewed went in after it.
- A joined reviewer shares the author's container and user,
  so a joined review guards against the author's mistakes, not against an author acting in bad faith (#1230).

### 5.4 How each head reaches the reviewer

| channel | the author delivers | the reviewer obtains |
|---|---|---|
| `github` | a push to the pull request | `gh pr checkout --force <n>`, which resets a rewritten branch to the pull request's head |
| `talk` | a push to the review branch on the base's remote | `git fetch <remote> <branch>` |
| `local`, started | round 1: the summon's base; later: `git bundle create .git/review/<head>.bundle <last delivered head>..HEAD`, `artifact mint .git/review/<head>.bundle`, `bro::quest_share` | round 1: its workspace `HEAD`; later: `artifact get <ref>`, then `git fetch <bundle path> HEAD` |
| `local`, joined | a commit in the shared tree, left clean | the shared tree, checked as §5.3 says |

A bundle's range always starts at the head the author last delivered, which the reviewer holds, and ends at `HEAD`, since `git bundle` refuses a range that ends at a bare commit id;
such a range carries a rewritten or a rebased branch whole (§16).
A head unchanged since the last delivery is delivered by naming it alone, with no push and no bundle, since the reviewer already holds it and `git bundle` refuses an empty range.
A reviewer that cannot obtain the named head, or obtains another commit, asks the author about it and reviews nothing.

### 5.5 Trust on GitHub

The author acts only on comments, reviews and approvals from trusted accounts:

- accounts GitHub marks `OWNER` on the comment or review;
- accounts the repository grants `write` or `admin`, `maintain` reading as `write`, where the watch's credential can read the grant (§8);
- the reviewers the plan engaged:
  a summoned reviewer asks its summoner `reviewing <pr> as <login>` before it posts anything,
  and a coordinator relays its own reviewer's login (§5.6);
- accounts named in `trust`.

An App account, a reviewer's included, is no repository's collaborator, so it is trusted only as an engaged reviewer or a named account.
`poll-pr` and `pr-state` enforce the rule through one trust predicate (§8), the session writing the engaged and named accounts into its trust file,
so an untrusted account's comment never reaches the model, and its approval clears nothing:

- an `others` verdict is an approving review of the head from a trusted account;
- a human approval on GitHub is one from a trusted account GitHub marks as a user rather than a bot.

The reviewer's own watch trusts the pull request's author and the accounts named to it, through its trust file, and the `OWNER` and write-access accounts as above,
and it adopts only trusted accounts' threads when it reconciles a pull request's history.
A watch whose permission lookups GitHub refuses hears the owner and the accounts its trust file names;
GitHub answers an App's installation token holding `metadata: read`, and is reported to refuse a user's token without push access (§16).
A watch still never reports its own account,
so a human sharing the author's account cannot be heard on the pull request and speaks in the session instead.
The rule covers the text of comments and reviews;
a pull request's title, body, commits and check names come from its author and from accounts that can push to it.
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

- A reviewer summon denied, or a reviewer that raised or errored before a verdict:
  the `verdict` gate cannot clear;
  where questions reach the user, the author reports it and stops, and elsewhere it raises with the reason.
  No other reviewer or channel stands in for it unless the plan is changed (§3.8).
- A reviewer that timed out before a verdict is succeeded on the same channel and plan:
  the author summons a successor as it summoned the first, handing it the predecessor's conversation as `bro::quest_history` returns it for the ended quest, an artifact past the message bound;
  on `github` the pull request carries the rounds as well.
  The successor holds what settled there as settled and reviews the head the author names now;
  where the history was truncated, it judges the whole change.
  A successor that times out is succeeded the same way, within the author's own run.
- A GitHub review under the author's own account:
  the reviewer refuses before reviewing anything, naming `talk` and `local`, and the author handles it as a denied summon.
- A `talk` reviewer that cannot fetch from the base's remote refuses, naming `local`.
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
3. It takes the verdict and the approval from its caller:
   the gates its own run retained where the author lands, otherwise the prompt that summoned it.
   The verdict check is the one step §4.2 reserves.
   A lander other than the author runs the `tests` gate itself on the head it lands, where the plan has it, since that gate is about the landing session's workspace (§3.5);
   a failure ends its landing with `blocked <head sha>: tests`, which the session it reports to takes as a red check:
   an author that summoned the lander starts a round with the same reviewer (§4.1), and a coordinator routes the change back to a new author session resuming it (§3.8).
4. It lands by the method, through `land-pr` (§6.2) or `land-push` (§6.3), and does the bookkeeping (§6.5).

### 6.2 `merge`

PRs stacked on the branch are retargeted to the base first,
then `land-pr --repo <owner>/<repo>`, bound to the base remote's repository, merges as it does today:
its preconditions (an open pull request, `reviewDecision`, unchecked test-plan boxes, the worktree on the reviewed head, rebase merging allowed),
its wait for the checks, the rebase merge with `--match-head-commit`, and the deletion of the head branch,
which it deletes in the pull request's head repository rather than on the checkout's `origin`.
The plan's gates decide its flags:
a plan without `checks` passes `--ignore-checks`, which leaves only the checks the base's rules require, a platform rule GitHub enforces at the merge.

### 6.3 `push`

Two commands of `bro.workflow`, beside `land-pr`, carry the push's mechanics.
`check-head` produces the `checks` evidence without landing anything, so it runs before any verdict:

```
check-head --base <ref> --workflow <file> [--wait <seconds>]
```

It pushes `HEAD` to `land/<head sha>` on the base's remote and runs `gh workflow run <file> --ref land/<head sha>` in that remote's repository;
it waits up to `--wait` seconds for the run to conclude, deletes `land/<head sha>` whatever the outcome, and prints the head, the run and its conclusion as JSON.
The author runs it on the landing fold's head (§5.2);
the run's checks attach to the head commit, so they count for that commit wherever it lands.

`land-push` lands:

```
land-push --base <ref> [--approved <head sha>] [--workflow <file>] [--wait <seconds>] [--delete <branch>]…
```

1. It refuses a dirty worktree.
2. It fetches the base's remote where the base has one, and exits with a distinct status, `base moved`, when `HEAD` does not descend from the base's tip.
3. With `--approved`, which the plan's `verdict` gate supplies, it refuses unless the verdict covers `HEAD` (§4.2):
   `HEAD` is the approved head, or `git range-diff <merge base of the approved head and the base tip>..<approved head> <base tip>..HEAD` shows every commit unchanged.
4. With `--workflow`, which the plan's `checks` gate supplies, it refuses unless `HEAD`'s check runs concluded green, dispatching them as `check-head` does for a head none ran on.
5. It pushes `HEAD` onto the base without force, `git push <remote> HEAD:<branch>` for a remote's branch and `git push . HEAD:<branch>` for a local one, and exits with `base moved` on a non-fast-forward refusal;
   any other refusal is the remote's rule, reported with its message.
6. It deletes the branches `--delete` names on the base's remote, the review branch on `talk` among them, and prints a JSON result:
   the base, the pushed commits, whether the verdict covered `HEAD` as the approved head or by range-diff, the check run, and the deleted branches.

[[land]] runs the loop around it.
On `base moved` it rebases onto the base, runs the `tests` gate again where the plan has it, and runs `land-push` again;
a rebase that conflicts, a range-diff showing a changed commit, or a red check run sends the change back to review (§4.3).
A remote other than GitHub gets neither the rules pre-check of §3.6 nor a dispatched run:
its refusal is its rule, and the plan check keeps `checks` off it.
A pull request whose head a push landed reads as merged, since GitHub marks a pull request merged once its head commits are reachable from its base (§16);
one that stays open is closed with a comment naming the pushed commits.

### 6.4 Handing the landing over

- Lander `summon`:
  the author summons a lander based on its own `HEAD`, or joined where the plan says so,
  with `[[land <pr or base> at <head> by <method>]]`, the `Delivery:` line, the cleared gates and the task's URL in its prompt;
  it waits for the lander's answer, relaying a landing and ending, and taking a `blocked` answer as a round (§6.1).
  A summoned lander resolves its own configured credentials, never the author's (§16),
  so a lander that merges or pushes to GitHub needs a `github` credential configured for it.
- Lander `coordinator`:
  the author answers `ready <ref> at <head>` with the `Delivery:` line, the cleared gates, and how the head is reached
  — the pull request, a pushed branch, or a bundle artifact its summoner can share onward —
  and ends.
- Lander `others`:
  the author reports the change ready to merge once every gate but `approval` has cleared, the person's merge being the approval and the landing at once (§3.3),
  and keeps its PR watch running until `merged`, then does the bookkeeping.

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
- Loading refuses an empty or repeated choice, a choice outside the template's literal grammar (`[A-Za-z0-9_.-]+`), a default outside the choices,
  and a name that collides with a fact spell bodies render with:
  `harness`, `creds`, `may_summon`, `talk`, `hold`, `features`, and the template's own `true` and `false`.
  A harness's own facts are known only once a harness is loaded, which constructing a bro never does, so a collision with one refuses at rendering.
- `SpellTool`'s schema and the cast roster both expose a choice parameter as an `enum` with its `default`.
  Neither binds a call by itself:
  the Claude-side tool server validates `enum` but the native tool registry does not, the cast's interpreting model only reads its roster, and nothing applies a JSON-schema `default`.
  The spell layer enforces both, in `SpellTool.call` and in the cast's call validation:
  a value outside the choices is refused before rendering, and an omitted optional one takes its default.
- `get_spell_body` receives the call's choice values, and the body renders with each as a string variable whose domain is its choices, as it renders `hold`.
  Every condition's literal is checked against that domain on every rendering, and an `iff` whose branches miss the rendered value raises,
  so `{{iff #channel = github}}…{{eliff #channel = talk}}…{{eliff #channel = local}}…{{end}}` renders one branch;
  a misspelt literal fails every rendering, and a missing branch the renderings of its value.
- A fragment a body includes renders with the including spell's variables, so a choice it tests is declared by every spell that includes it.
- `call_text`'s `# Arguments` section lists every passed value as today, and each defaulted choice as `<name>: <value> (default)`.
- The cast path and the `spell::` tools both render through `_render_spell_call`,
  so a session without `mu`, whose `[[…]]` markers call the spell's own tool, renders the same way, and nothing returns unrendered.
- `bro/spells_test.py`'s `test_every_spell_call_fits_one_window` renders every shipped spell under every combination of its choices and holds each rendering to `BYTE_LIMIT`;
  its other parameters are filled as today, and the cast's `spell: spell::<name>` prefix counts.

### 7.2 `run-pr`

Its parameters are §3.1's, `channel` and `landing` being choice parameters;
it requires [[land]], [[follow pr]] and [[watch]].
Its sections:

1. the arguments and the plan, its check and its `Delivery:` line (§3);
2. resuming with `pr` (§3.8);
3. preparing:
   the survey, the formatter and the policy audit, the docs, the commit splits and messages, the commits, the rebase, the fold before review, the change-scoped gate and the scope check, as today;
   every step names the base as the ref it is (§3.1), `fold-branch --base <base>` among them;
4. publishing:
   a pull request where `channel` is `github` or `landing` is `merge`, opened on the base remote's repository from a head branch pushed to that remote, with its `PR opened` task comment, its review link, and [[follow pr]] armed on it;
   a review branch on the base's remote for `talk`;
5. the review handoff of the plan's channel, the only branch that renders:
   on `github`, summoning the reviewer, running the coordinator exchange, or waiting on trusted accounts;
   on `talk` and `local`, the summon with its talk rights, the deliveries of §5.4 and the turns of §5.3;
6. the author's half of the rounds, the landing fold and a timed-out reviewer's successor (§5.1, §5.2, §5.7);
7. the gates:
   retaining them per head (§4.2), collecting the approval from the approver, and handing the landing over once all clear, or under lander `others` once all but `approval` clear (§6.4).

### 7.3 `follow-pr`

A new spell taking `pr`, cast wherever a pull request is open, by `run-pr` and by a resume;
it requires [[watch]] and [[land]].

1. Arm `poll-pr <owner>/<repo> <n> --trust-file <path> --replay` through [[watch]], the trust file kept outside the tree.
   The replay is the reconciliation:
   every comment and review the pull request already carries arrives as an event before the live ones, so nothing posted while nothing watched falls between a snapshot and the watch's start.
   The author acts on none of them, starting no round and clearing no gate, until `replay_complete:` closes the replay;
   then it settles the whole replay at once, one item a later commit, reply or review already answers being settled and the rest open feedback.
2. React to every event:
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
the head and what [[land]] re-checks, the one rendered method with its command (`land-pr` or `land-push` and its loop), the handover's receiving side, and the bookkeeping.
The no-pull-request review rounds its current text runs go, since review is `run-pr`'s.

### 7.5 The eyebro's `review`

`review` is the one entry point a summoner casts, taking `channel` (a required choice parameter) and `change`;
it replaces `review-pr` and requires [[review diff]] and [[watch]].

- The `github` branch is today's `review-pr`, with these changes:
  - its precondition refuses a shared identity before anything else, naming `talk` and `local`;
  - it announces its login to its summoner (§5.5);
  - its trust file holds the pull request's author and the accounts named to it, it reads the pull request through `pr-state --trust-file`, and it reconciles and adopts trusted accounts' threads only;
  - it arms its watch with `--replay` before posting anything, the replay carrying the pull request's existing history, so nothing posted before or during its start is lost;
    it reconciles the whole replay, up to `replay_complete:`, before its first round;
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
  "land via [[run pr]] with its base argument set to `origin/<integration branch>`, reviewed on github by a reviewer you summon, with gates verdict and checks — the human approval comes at the integration landing".
- The coordinator roles are the lead's option per stage:
  it answers §5.6's exchanges, and a stage that answers "ready" lands through a lander the lead summons with `[[land …]]`.
- A phase whose plan summons a reviewer or a lander is granted that target under the name `may_summon` gives.
- References move to [[follow pr]] and to `run-pr`'s `pr` argument, which the integration phase's re-fire already relies on.
- A lead resuming an orchestration amends every pending stage task whose delivery line names a bare branch, as the old runtime writes it, to `origin/<branch>` before it launches the next stage, and records the amendment as a comment on that task.

### 7.7 Other references

- `fix`, `bump-bro` and `reflect` keep handing off to [[run pr]],
  and their references to its step numbers become references to its section names.
- `rebase_conflicts.md` names `run-pr`'s sections instead of its step numbers, and rebases onto `<base>` as the ref it is.
- `review-diff`'s default target stays the current branch against origin's default branch, since `review` always passes the range.
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

`run-pr`'s widest rendering today is 29,950 characters, 50 under the window (§16).

## 8. `poll-pr` and `pr-state`

- `poll-pr --trust-file <path>` names a file of trusted logins, one per line, which the watch reads again every cycle,
  so a session extends it while the watch runs, as an announcement arrives.
  The watch reads it outside its GitHub sources:
  a file it cannot read ends the watch with a `watch_failed` naming the file, never as a GitHub source's failure.
- An account the file does not name is trusted where GitHub marks the item `OWNER` (`author_association`),
  and otherwise where the repository grants it `write` or `admin`, `maintain` reading as `write`, through `repos/{owner}/{repo}/collaborators/{login}/permission`, read once per login per watch.
  GitHub answers that endpoint for an App's installation token holding `metadata: read`, and is reported to refuse a user's token without push access;
  an App account is no collaborator.
  A refused or missing answer (403 or 404) leaves the login untrusted, cached like any answer, and never fails the watch.
- `comment` and `review` events fire only for trusted accounts,
  replacing today's rule of the pull request's parties (its author, the repository's owner, anyone who reviewed), under which any account's review made its comments fire.
- An untrusted account's comment or review fires one bodiless `untrusted:` event with `user`, `kind` and `url`,
  and is held:
  once its account becomes trusted, the held items fire as their ordinary events, in order, a held review with its inline comments;
  an item deleted while held is dropped.
- `--replay` emits every comment and review the pull request carries at the watch's start as events, oldest first and marked `replayed`, before the live ones, where a watch without it marks them seen silently,
  and closes the replay with one `replay_complete:` event carrying the count, before any live event.
  `comment` events carry `in_reply_to`, so a replay rebuilds the threads.
- `comment` and `review` events also carry `user_type`, `User` or `Bot`, as GitHub reports it,
  and `review` events the `commit_id` the review was given for, replayed and live alike, so an approval clears only the head it names (§4.2).
- The watch's own account stays filtered out, as today.
- `pr-state --trust-file <path>` applies the same predicate, a function both commands share:
  it prints an untrusted account's review or comment as its `user`, kind and `url` alone.
  It gains the conversation comments, and each review carries `user_type`, `author_association`, `state` and `commit_id`, from which a resumed run decides whether a person approved the head.

## 9. Token accounting

Footers stay as they are:
the `commit-msg` and `post-commit` hooks with the usage file and the baseline state behind them, `commit-footer`, `fold-branch`'s aggregation onto the landed commits, the baseline promotion after a fold, `usage-report` and `git golc`.

The rounds of §5 change only when the branch folds:
working commits and fixups keep their per-commit footers, the fold before review aggregates them as a fold does today, and the landing fold aggregates what the rounds appended.
A summoned reviewer's, lander's or helper's spend reaches no footer, as today,
and nothing spent after the landing fold is counted.
Counting every session that contributed to a landing is #1227's, which takes the landing fold as its hook point.

## 10. Personas

- `bro-eyebro` is deleted, with its entry point and its tests.
  Its pinned `github: True` keeps it from running `local` without a credential,
  and its framework prompt and man pages duplicate what the reviewer reads from the checkout, in the version under review.
  The generic `eyebro` takes its place:
  `may_summon` grants `@eyebro`, and the host configuration's picks for it, `github+eyebro` among them, move to `eyebro`'s entry (§11).
- `eyebro`'s spells are `review-diff` and `review`;
  its `github` feature stays gated on the credential, and the `github` branch names what is missing without it, as `review-pr` does today.
- `lead` and `dev` keep their tools.

## 11. Rollout

The change ships in the installation, and nothing is deployed separately:
the spells, `poll-pr`, `pr-state`, `land-push` and the personas reach a session through its root's runtime bundle.

- **Runtime.** Every session of a tree runs its root's frozen runtime bundle and its root's reading of the host configuration, summoned, joined and manual children alike (§16),
  so an author and the reviewer, lander or coordinator it works with always run the same spells and commands.
  The new spells go live for roots started after the host installation is synced to a default branch carrying them;
  running roots keep the old ones until they end, and a review that spans the sync finishes under the runtime that started it.
- **Host configuration.** An entry or `@` grant naming a bro the installation lacks refuses every root start (§16):
  1. before the sync, add `eyebro`'s entries (its `github+eyebro` pick and its recipe) and grant `@eyebro` wherever `@bro-eyebro` is granted, which is valid while both bros are installed;
  2. sync the installation;
  3. before any root starts on it, remove every `bro-eyebro` entry and `@bro-eyebro` grant.

  Running roots keep their reading, and with it `bro-eyebro`, until they end.
- **Joins.** A root's authority is fixed at its start, so joins are granted in the configuration a root starts under.
  This repository grants none, its reviews running on `github`;
  a deployment that runs joined reviews, such as a benchmark box, grants `bro.placement=join for @eyebro` in its own settings.
- **Pull requests across the sync.** One opened before it carries no `Delivery:` line;
  a resume takes its base from the pull request itself and its other choices from the defaults (§3.8).
- **Stage tasks.** Orchestrate's delivery line (§7.6) passes parameters only the new `run-pr` accepts, its base as `origin/<integration branch>`,
  so only a lead started on the new runtime writes it, for stages that run under that same runtime.
  A stage task written for the old runtime passes a bare branch name, which the old `run-pr` reads as origin's branch;
  a new `run-pr` refuses it, naming `origin/<branch>` (§3.1), and a lead resuming on the new runtime amends the pending ones before its next stage (§7.6).
- **`fold-branch --base`.** It takes a ref (§3.1) instead of origin's branch name;
  every caller is a spell of the same runtime bundle, so the change needs no transition.
- **GitHub.** No rule changes;
  `land-push`'s `checks` gate needs the CI workflow to accept `workflow_dispatch`, which this repository's `tests.yml` does.
- **Footers.** Unchanged (§9), so history reads the same before and after.

## 12. Testing

Deterministic tests, in the stages that build each piece:

- Choice parameters:
  the load refusals, the `enum` and `default` in both schemas, a value outside the set refused on the Claude and the native path and through the cast, defaulted rendering, the `# Arguments` listing,
  and every shipped spell rendered under every combination of its choices within `BYTE_LIMIT`.
- `poll-pr`:
  the trust file read again each cycle, and its read failure;
  `OWNER` trusted without a lookup, write access read once per login, and a refused lookup leaving the login untrusted without failing the watch;
  bodiless `untrusted:` events, and held items fired once trusted (a review with its inline comments) and dropped once deleted;
  `user_type`, and the own account still filtered.
- `poll-pr --replay`: the existing comments and reviews emitted once, in order, before the live ones, untrusted ones bodiless, threads carried by `in_reply_to`, and `replay_complete:` before the first live event;
  `review` events carrying `commit_id`, replayed and live.
- `land-pr --repo`: the pull request resolved and merged in the named repository, and the head branch deleted in its head repository.
- `pr-state`: the trust predicate shared with `poll-pr`, untrusted items printed without their text, the conversation comments, and each review's `user_type`, `author_association`, `state` and `commit_id`.
- `check-head`:
  the dispatched run stubbed green, red and past its wait, with `land/<head sha>` deleted after each, and the JSON result.
- `land-push`:
  a local base through `git push .` and a remote's branch, a dirty worktree refused, and `base moved` before and at the push;
  the range-diff rule with `--approved`, an unchanged rebase covered and a changed commit refused, and no coverage check without it;
  `--workflow` accepting a head an earlier run made green and dispatching for one none ran on;
  and the JSON result.
- The base resolution `fold-branch`, `check-head` and `land-push` share:
  a remote's branch, fetched;
  `refs/heads/<branch>`, not fetched;
  a bare name accepted as a local branch in a repository without remotes and refused, naming every explicit form, in one with a remote;
  a name with no ref refused;
  and an omitted base refused in a repository without an `origin`.
- The persona rosters and spell requirements of `dev`, `eyebro` and `lead`, and `bro-eyebro`'s absence.

The routes below are the design's end-to-end paths.
The stage that completes each one covers its mechanics deterministically:
its commands against a stubbed GitHub, and the renderings its choices select.
Live runs of them are #1228's, whose first scenarios they are;
it lands before this design's verification, so verification is never where a route first runs.

| route | path |
|---|---|
| R1 | `github` · reviewer `summon` · approver `human` · lander `self` · `merge`: a fix session's default delivery |
| R2 | `github` · reviewer `summon` · gates `verdict, checks` · `merge` into an integration branch: an orchestrated stage |
| R3 | `github` · reviewer `coordinator` · approver `summoner`: the exchanges of §5.6 |
| R4 | `github` · reviewer `others`: a trusted account's approval as the verdict, and an untrusted comment held as an `untrusted:` event |
| R5 | `talk` · `merge`: the review branch, and rounds on the quest |
| R6 | `local` with a started reviewer · `push` onto a local branch: bundle deliveries |
| R7 | `local` with a joined reviewer · `push` onto a local branch in a repository without a remote: turns |
| R8 | `push` onto a GitHub base with `checks`: the dispatched run |
| R9 | lander `summon`, `coordinator` and `others`: the handovers of §6.4 |
| R10 | a base that moves after a verdict: an unchanged rebase keeping the verdict, and a conflict reviewed by a successor |
| R11 | `run-pr` with `pr`: a resume, with and without a `Delivery:` line |
| R12 | a reviewer that times out, and its successor |
| R13 | the plan check's refusals, a base naming no ref among them, and a plan without `verdict` passing whatever its channel |

## 13. Deletions

- `dev/bro/dev/spells/resume-pr.md`, folded into `run-pr` (§3.8, §7.3).
- `dev/bros/eyebro/spells/review-pr.md`, folded into `review` (§7.5).
- `run-pr`'s "Hand the review to the eyebro" and its review-event handling, moved to §7.2's sections and to `follow-pr`.
- `land`'s review rounds for landings without a pull request.
- `local/bros/bro_eyebro/` and its `bro.bros.bro` entry point (§10).

## 14. Follow-ups

- [#1227](https://github.com/dzhioev/bro/issues/1227) counts every session that contributed to a landing in its footers, taking the landing fold (§5.2) as its hook point.
- [#1228](https://github.com/dzhioev/bro/issues/1228) runs §12's routes live, ahead of this design's verification.
- [#1229](https://github.com/dzhioev/bro/issues/1229) restores the banner's lineage marks, which tell a model which granted target answers to `eyebro` when its name does not say so (§3.1).
- [#1230](https://github.com/dzhioev/bro/issues/1230) signs verdicts:
  the verdict's text (§4.1) is what a signed tag carries, and [[land]]'s verdict step (§4.2) is where its verification plugs in.

## 15. Decision record

The owner settled every decision below from 2026-11-08 to 2026-11-10.

| # | Decision | Applied in |
|---|---|---|
| 1 | The review channel is an explicit choice of `github`, `talk` or `local`, and `local` covers a started reviewer (the summon's base, then bundles) and a joined one (the shared tree, in turns) | §3.2, §5.3, §5.4 |
| 2 | `[[review]]` is the reviewer's single entry point, replacing `review-pr`, and the author chooses the channel through `run-pr`'s `channel` | §7.5 |
| 3 | Choice parameters are a core spell addition both the cast and the `spell::` tools render with, rather than a sub-spell per channel | §7.1 |
| 4 | The defaults are channel `github` and landing `merge`, and no repository rule restricts the channel: the default keeps this repository's reviews on GitHub | §3.1 |
| 5 | The reviewer, the approver and the lander are roles chosen per plan, so author, reviewer and lander can be three bros | §3.3, §6.4 |
| 6 | A landing requires a human's approval by default, and landing without one is only ever explicit | §3.5 |
| 7 | Gates are chosen independently of the landing method, and platform rules are never chosen nor waived | §3.5, §3.6 |
| 8 | `push` lands onto any branch of any remote, or onto a local branch, with GitHub's steps only on GitHub, through a `land-push` command beside `land-pr`, `check-head` producing its checks before any verdict | §3.4, §6.3 |
| 9 | What the reviewer approves is what lands: fixes are appended, the branch folds before review and once more when nothing is open, and the reviewer stays alive to reconfirm the landing fold, saying it waits for it | §5.1, §5.2 |
| 10 | Checks and mergeability are part of the verdict | §4.1 |
| 11 | A verdict names its head and base commit and holds only for that head or a rebase of it with every commit unchanged; a moved base needing changes gets a successor that reviews only the delta, given the earlier verdict | §4.2, §4.3 |
| 12 | A reviewer that times out is succeeded on the same channel and plan, given its predecessor's conversation | §5.7 |
| 13 | On GitHub the author acts only on trusted accounts (the owner, write access, announced reviewers, named accounts), enforced by one predicate in `poll-pr` and `pr-state` so that untrusted text never reaches the model, and a watch's replay loses nothing at its start; a permission lookup GitHub refuses leaves the account untrusted without failing the watch | §5.5, §8 |
| 14 | `resume-pr` folds into `run-pr`'s `pr`, and `follow-pr` holds the PR watch | §3.8, §7.3 |
| 15 | Footers, their hooks, `fold-branch`'s aggregation and the reports stay as they are, and counting every session that contributed to a landing moves to #1227 | §9 |
| 16 | `bro-eyebro` is dropped in favour of the generic `eyebro` | §10 |
| 17 | `local` stays in this design; this repository grants no joins, and a deployment running joined reviews grants them in its own settings | §11 |
| 18 | Orchestrate's stages state `gates=verdict,checks`, the human approval coming at the integration landing | §7.6 |
| 19 | Live-LLM runs of the routes are #1228's, which lands before this design's verification | §12 |
| 20 | Signed review tags are a follow-up, #1230, and the verdict is shaped for them | §4.1, §14 |
| 21 | The base is a ref used as given, a remote's branch or a local branch, so no step assumes an `origin` or how the repository was prepared; a bare name means a local branch only where the repository has no remote, and refuses elsewhere | §3.1 |

## 16. Assumptions and evidence

Verified while designing:

- `master`'s ruleset (`gh api repos/dzhioev/bro/rulesets/20695416`) forbids deletion and non-fast-forward updates, and requires linear history and the `tests` check;
  it also requires a pull request with one approval, the last push approved by someone other than its pusher, and squash or rebase merges.
- GitHub's rebase merge always updates the committer and writes new commit ids, and drops commits that were empty to begin with
  ([About merge methods on GitHub](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/about-merge-methods-on-github)).
- The collaborator-permission endpoint answers under this checkout's GitHub App installation token:
  `dzhioev` reads `admin`, the App account `bbbbro[bot]` reads `none` with type `Bot`, and an unrelated account reads `read`.
  An App account therefore never qualifies through write access.
- A mint is readable by its minter and its summoners, `share` and `bro::quest_share` hand a ref down, and a manual child receives no share (`bro/reference/ride.md`, "Sharing artifacts between peers").
- A quest message is bounded at 16 KiB (`bro/broker/journal.py`) and an answer at 64 KiB (`bro/run_lifecycle.py`).
- A spell body renders today without its call's arguments (`BaseBro.get_spell_body`), and the `spell::` tools already carry every declared parameter in their schema (`bro/spells.py`, `SpellTool`).
- `poll-pr` reports comments and reviews from the pull request's parties, a stranger who reviews becoming one.

Verified in review:

- The ruleset also sets `require_extra_approval_for_unattributed_changes`, which GitHub's documentation does not describe;
  repositories using it report a pull request held for a person's approval while it carries a commit no GitHub account is credited with, as this repository's `bro-dev <bro-dev@bro>` commits are.
  No ruleset covers another branch, so `integration/*` and `land/*` take direct pushes.
- GitHub marks a pull request merged when its head commits become reachable from its base outside it, protection rules notwithstanding
  ([Pull request merges](https://docs.github.com/en/pull-requests/reference/pull-request-merges), "Indirect merges").
- Once a commit's required checks pass, it may be pushed directly onto a protected branch
  ([About protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)),
  and checks attach to the commit, so a run on `land/<head sha>` counts for the push of that commit.
- `tests.yml` runs on every pull request whatever its base, on pushes to `master`, and on `workflow_dispatch`;
  its recent runs took five to six minutes, so the reviewer's 14,400 seconds are at risk from human latency rather than CI (§5.7).
- `gh api repos/{owner}/{repo}/rules/branches/<branch>` reports rulesets only, `master` reading its five rules and an integration branch none;
  classic branch protection takes admin access to read.
- Under the reviewer App `eyebbbbro`'s installation token, which holds `contents: read` and `metadata: read`, the collaborator-permission endpoint answered `200` with `admin` for `dzhioev` (this review's eyebro probe),
  advertising `X-Accepted-GitHub-Permissions: metadata=read`;
  it is reported to refuse a user's token without push access ("Must have push access to view collaborator permission").
- Every review and comment carries `author_association`:
  on recent pull requests `dzhioev` reads `OWNER`, `eyebbbbro[bot]` `NONE`, and `bbbbro[bot]` `CONTRIBUTOR`.
- Every session of a tree, summoned, joined or manual, runs its root's frozen runtime bundle (`ride/ride/bro_worker.py`, `SummonSpawner`),
  and a root freezes its reading of the host configuration for all its descendants (`bro/reference/ride.md`, "Home settings").
- A host-configuration entry or `@` grant naming a bro the installation lacks refuses every launch (`ride/ride/authority_reading.py`).
- The host admits exactly the granted target names,
  and only the `#may_summon` template fact answers to a granted bro's lineage (`bro/mcp.py`, `_answers_to`);
  live banners print the host's raw entries without lineage marks (#1229).
- A started child bases on its summoner's `HEAD` with local-only commits, its clone pointing `origin` at the attachment's remote and carrying none of the summoner's local branches, so nothing it pushes reaches the summoner's tree;
  a joined child uses the party's tree with its uncommitted state, sharing the summoner's container, user, index and `.token_accounting_state.json`.
  A join needs `bro.placement=join` at its root's start, which `ride override` cannot grant.
- A summoned child resolves its own configured credentials, never its summoner's.
- `artifact mint <path>` takes a file or directory inside the workspace, and the store is capped at 32 GiB per ride and ends with it.
- `git bundle create` refuses a range ending at a bare commit id and an empty range, both with "Refusing to create empty bundle", and accepts `<old head>..HEAD` after a rewrite, which a clone holding the old head verifies and fetches.
- `default-branch` reads origin's `HEAD` (`git ls-remote --symref origin HEAD`), and `fold-branch` fetches `origin/<base>` and folds against it (`dev/bro/workflow/fold_branch.py`), so both fail in a repository without an `origin`;
  `run-pr`, `land`, `review-pr` and `rebase_conflicts.md` name `origin/<base>` throughout.
- Ride's clone preparation reads the attachment's origin URL (`ride/ride/workspace/clones.py`, `_prepare_clone`), so today a started child cannot start on a repository without an `origin`;
  how a repository is prepared is ride's, nothing in this design depends on it, and a joined child needs no clone.
- `gh pr checkout --force` resets an existing local branch to the pull request's head.
- `land-pr` resolves and merges the pull request in the repository `gh` picks for the checkout, and deletes its head branch with `git push origin --delete` (`dev/bro/workflow/land_pr.py`);
  `poll-pr`'s review events carry no `commit_id` today, while `pr-state`'s reviews do.
- The root broker retains quest results up to 2 MiB and 256 records, a quest keeps 16 pending questions and a 32-entry history tail, and a summoner's end ends its live children `failed:orphaned` (`bro/broker/journal.py`, `bro/broker/dispatcher.py`).
- Choice parameters:
  `_parse_parameters` refuses a value that is not a string;
  `talk` is a framework fact name;
  `true` and `false` are the template's own variables;
  a domain-closed `StringVariable` refuses a literal outside its domain but does not check its own value;
  an `iff` whose branches miss the rendered value raises;
  the Claude-side tool server validates `enum`, the native tool registry does not, and nothing applies a JSON-schema `default`
  (`bro/spells.py`, `bro/template.py`, `bro/condition.py`, `bro/runtime/mcp_server.py`, `bro/llm/mcp.py`).
- `run-pr.md` is 29,976 bytes on `master`, and its widest rendering is 29,950 characters.
- `land-pr` merges with `gh pr merge --rebase --match-head-commit` and has no push path, and `land`'s landing without a pull request names no dispatch command.

Unverified:

- The renderings of §7.8, which the test of §7.1 settles.
