---
name: land
description: This spell should be used when the user signals that an open PR should be merged into master — "land it", "land", "merge it", "merge the PR", "merge to master". Checks that the branch still carries the commits [[run pr]] folded it into and runs `land-pr`, which merges the open PR for the current branch in one shot (precondition checks including green CI, a rebase merge that writes nothing to the branch, remote branch cleanup), then records a `merged` comment on the task and closes it to done unless the user explicitly said to keep it open. On APPROVED, [[run pr]] chains into this spell. Direct push to master (no PR) is a one-liner (`git fetch origin && git rebase origin/master && git push origin HEAD:master`) — not this spell.
version: 4.0.0
---

# land

Merge an approved PR for the current branch into master. The terminal action of a dev session.

The mechanical work is one command (`land-pr`); your job is the judgment calls around it — waivers, task closure — and doing the whole thing in as few responses as possible. Post-approval latency is model round trips, so batch aggressively: never spend a turn on a single call that can share a response with another. The one thing worth waiting for is CI: `land-pr` blocks on it for you (step 2), so budget the tool timeout for that wait instead of trimming it.

No suite run before the merge: the branch was verified at its last push, the land publishes nothing of its own, and the merged result on master is verified anyway by the next session that builds on it — a pre-merge pass adds latency and no evidence.

## Step 1 — check the branch is in landing shape

What master gets is the commits the branch carries: [[run pr]] folded them into that shape before review, and `land-pr` writes nothing to the branch. Read them once before merging:

```bash
git log origin/<base>..HEAD --oneline    # <base> is the PR's base branch, master unless it says otherwise
```

Messages and all, that is what lands. If they still read as working commits — a checkpoint, a review fix standing on its own — the branch never went through [[run pr]]'s fold step, and merging would put that noise on master. Fold it now per that step and force-push, then wait for the approval the push costs where the repo requires the last push to be approved.

## Step 2 — merge: run `land-pr`

```bash
land-pr
```

One shot, in order:

1. Resolves the PR for the current branch and enforces the preconditions — fails with a message and a nonzero exit when the PR is not `OPEN`, not `APPROVED`, its body has unchecked `- [ ]` test-plan boxes, the worktree sits on something other than the reviewed head, or the repository disallows rebase merging.
2. Waits for the PR's status checks to conclude (up to `--wait-checks` seconds, default 480) and refuses to merge while any is pending or failed. A PR with no checks passes straight through. **Give the command room to wait** — run it with a tool timeout above the wait budget, not the default. A timeout expiry is not a verdict: re-run `land-pr` (checks that slow are usually minutes from done), never reach for a waiver to get past it.
3. Merges: GitHub replays the branch onto the base, so every commit lands with the message and accounting footer it carried through review. Then deletes the remote feature branch — the local branch and worktree stay untouched.
4. Prints a JSON result: `pr`, `url`, `title`, `base`, `merged_sha`, `merged_at`, `commits`, `branch_deleted`.

Waiver flags map to explicit user statements from this session — never pass them when [[run pr]]'s APPROVED event chained into this spell:

- `--no-review` — the user said to merge without waiting for approval. A `CHANGES_REQUESTED` review is refused regardless; that needs the review resolved, not a waiver.
- `--allow-unchecked` — the user said to land despite unchecked test-plan boxes. Otherwise an unchecked box means nobody verified that item: surface the failure output (it lists the boxes) and wait.
- `--ignore-checks` — the user said to merge whatever CI says. It covers both states, pending and failed, and `land-pr` names on stderr every check it merged past — relay that to the user. Reach for it only on the user's explicit say-so about *this* PR's checks; a red check is otherwise something to fix or re-run (`gh run rerun --failed <run-id>`), never something to route around.

A waived review does not imply waived checks: "land it without review" means skip the approval wait, and CI still has to go green.

If `land-pr` exits nonzero, surface its stderr and stop — do not hand-roll the merge with raw `gh` commands, do not invent state. Someone pushing to the branch between [[run pr]] and [[land]] is what step 1 catches: what lands is the reviewed head, and `land-pr` refuses outright when the worktree is not on it.

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
