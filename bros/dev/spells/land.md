---
name: land
description: This spell should be used when the user signals that an open PR should be merged into master — "land it", "land", "merge it", "merge the PR", "merge to master". Decides the shape the branch lands in — one squashed commit by default, or one commit per logical change when the branch carries more than one — and runs `land-pr`, which merges the open PR for the current branch in one shot (precondition checks including green CI, per-landed-commit token-footer aggregation, remote branch cleanup), then records a `merged` comment on the task and closes it to done unless the user explicitly said to keep it open. On APPROVED, [[run pr]] chains into this spell. Direct push to master (no PR) is a one-liner (`git fetch origin && git rebase origin/master && git push origin HEAD:master`) — not this spell.
version: 3.5.2
---

# land

Merge an approved PR for the current branch into master. The terminal action of a dev session.

The mechanical work is one command (`land-pr`); your job is the judgment calls around it — the landing shape, waivers, task closure — and doing the whole thing in as few responses as possible. Post-approval latency is model round trips, so batch aggressively: never spend a turn on a single call that can share a response with another. The one thing worth waiting for is CI: `land-pr` blocks on it for you (step 2), so budget the tool timeout for that wait instead of trimming it.

No suite run before the merge: the branch was verified at its last push, nothing in the land changes its content (`land-pr` proves that before it publishes anything), and the merged result on master is verified anyway by the next session that builds on it — a pre-merge pass adds latency and no evidence.

## Step 1 — decide the landing shape

Master carries one commit per logical change, and the branch is rarely shaped that way already: [[fix]]'s checkpoints and the review round-trips are commits about *making* the change, not changes of their own. Read what the branch became and decide what it should land as:

```bash
git log origin/<base>..HEAD --oneline    # <base> is the PR's base branch, master unless it says otherwise
```

One commit is the default, and needs no further input — a branch that does one thing lands as one commit, review noise folded away. Land several when the branch carries a change that stands on its own beside the task: the unrelated bug fixed along the way, a drive-by cleanup worth its own line in the history. The user's own words settle it either way ("land these as two commits", "squash it all"). Grouping is then mechanical: every checkpoint and review fix folds into the unit it serves, wherever it sits in the log — a review fix at the tip usually belongs to the *first* fold.

For several, write the plan as a text file, one `fold` line per landed commit, oldest first:

```
fold <sha> <sha> <sha>
<subject of the commit they land as>

<body, written exactly as a commit message>

fold <sha>
```

The folds must cover every commit the PR adds to its base, exactly once. The lines under a `fold` are that commit's message; leave them out, as the second fold does, and it keeps the message of the first commit it folds — right when that commit already names the unit, worth writing when it doesn't. This text is what master carries, so it follows the repo's commit conventions, task metadata included.

When a single commit straddles two folds — it changed the feature and the unrelated thing together — no grouping of whole commits is clean. Restructure the branch first (`git rebase -i`, `edit` on that commit, split it into its two parts), then plan over the commits you now have. What `land-pr` verifies is the content, not the history, so a locally restructured branch is fine; what it refuses is a restructuring that changes what the review saw.

## Step 2 — merge: run `land-pr`

```bash
land-pr                      # one commit
land-pr --plan plan.txt      # one commit per fold
```

One shot, in order:

1. Resolves the PR for the current branch and enforces the preconditions — fails with a message and a nonzero exit when the PR is not `OPEN`, not `APPROVED`, or its body has unchecked `- [ ]` test-plan boxes. With `--plan`: also that the repo allows rebase merging and that the worktree is clean, the fold running in it — commit or stash first.
2. Waits for the PR's status checks to conclude (up to `--wait-checks` seconds, default 480) and refuses to merge while any is pending or failed. A PR with no checks passes straight through. **Give the command room to wait** — run it with a tool timeout above the wait budget, not the default. A timeout expiry is not a verdict: re-run `land-pr` (checks that slow are usually minutes from done), never reach for a waiver to get past it.
3. Aggregates the token-accounting footers of the commits folded into each landed commit, so the accounting survives the fold whichever way the branch is grouped. Commits carrying no footers land without one; warnings go to stderr — relay them to the user.
4. Merges. Without a plan: a server-side squash with the PR's own title and body as the commit subject/body. With one: folds the branch into the planned commits locally, checks that the fold's tree matches the PR head it replaces, force-pushes it, and rebase-merges so each fold lands as its own commit. Then deletes the remote feature branch (local branch and worktree stay untouched by the squash path; the fold leaves the local branch on what landed).
5. Prints a JSON result: `pr`, `url`, `title`, `base`, `merged_sha`, `merged_at`, `commits`, `branch_deleted`.

Two things only the `--plan` path can hit:

- **The force-push is new commits as far as GitHub is concerned.** A repo that dismisses approvals on a push has just dismissed this one, and `land-pr` stops rather than merge unapproved. Say so plainly — the content is unchanged and only the commit boundaries moved, so it needs a re-approval, not rework.
- **A merge that fails after the push** (that dismissal, a required check re-running) leaves the branch already folded into the planned commits. Re-run once the blocker clears with a plan of one fold per commit: it rebuilds the same commits, so nothing churns.

Conflicts are the fold's own failure mode — grouping reorders the branch, and hoisting one fold above another can collide. `land-pr` aborts the rebase, leaves the branch untouched, and names what collided; regroup so the colliding commits keep their relative order, or land it squashed.

Waiver flags map to explicit user statements from this session — never pass them when [[run pr]]'s APPROVED event chained into this spell:

- `--no-review` — the user said to merge without waiting for approval. A `CHANGES_REQUESTED` review is refused regardless; that needs the review resolved, not a waiver.
- `--allow-unchecked` — the user said to land despite unchecked test-plan boxes. Otherwise an unchecked box means nobody verified that item: surface the failure output (it lists the boxes) and wait.
- `--ignore-checks` — the user said to merge whatever CI says. It covers both states, pending and failed, and `land-pr` names on stderr every check it merged past — relay that to the user. Reach for it only on the user's explicit say-so about *this* PR's checks; a red check is otherwise something to fix or re-run (`gh run rerun --failed <run-id>`), never something to route around.

A waived review does not imply waived checks: "land it without review" means skip the approval wait, and CI still has to go green.

If `land-pr` exits nonzero, surface its stderr and stop — do not hand-roll the merge with raw `gh` commands, do not invent state. If the PR description has materially drifted from what shipped (the user pushed content between [[run pr]] and [[land]]), surface it *before* running — with no plan, the PR body is what becomes the commit body.

## Step 3 — task bookkeeping + report: one response

First decide task closure. Two cases block or defer the close:

- **The change needs a deploy or migration to take effect.** If it touches code or config that runs in a deployed service, or adds a migration/backfill — the repo's own docs say what is deployed — the merge alone doesn't make it live: the task closes only after the deploy succeeds. An explicit instruction in the initial request or task body to close without holding for the deploy (e.g. a staged feature flow that deploys once after integration) overrides this whole case: close as instructed and note the deferred deploy in the report. Otherwise hand the rollout to the operations bro in your summon allow-list: summon it (per [[ask]]) with a terse deploy request — `deploy <service or feature>`, naming the target, not the steps; the ops bro infers the spells and sequence itself — and a timeout adequate for the rollout (the 1800s default is sized for a typical deploy; raise it when the target plausibly needs longer). Base that child on the merged commit — pass `land-pr`'s `merged_sha` as the summon's base ref (`--into <merged_sha>`), not as a commit named in the prompt; your own HEAD is not what master now points at. Then:
  - deploy succeeded → close the task done as usual and include the ops bro's answer in the report;
  - deploy failed (raised / error / timeout) → leave the task open and report the failure and its reason;
  - no summon client in the session (no broker channel), or no operations bro in the allow-list → leave the task open and report the pending deploy, naming the exact summon command (`call <ops-bro> "deploy …"`).
- **The user said to keep it open.** Phrases in the initial prompt like "keep this open", "leave open with notes", or "only landing a subset" mean the task stays in its current status; note it in your report.

Then emit everything in a single response — the report text plus, for sessions with a task, both brog calls in parallel:

- `brog::add_comment(task_id, topic='merged', body='[PR:<n>](<pr-url>) merged to master')` — `<n>` and `<pr-url>` are the `pr` and `url` fields of `land-pr`'s JSON result.
- `brog::update_task(task_id, status='done')` — unless a bullet above said to keep it open.
- The report, one line: PR URL, "merged to master" (as N commits when the result's `commits` is more than one), and task status — closed to done (after a successful deploy handoff when one was needed), left open per instruction, or left open on a failed or pending deploy (with the reason, or the pending summon command).

## Safety rules

- Never bypass GitHub merge requirements beyond the two explicit waiver flags, and only with the user's say-so from this session.
- Never `--admin` your way past branch protections.
