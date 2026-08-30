---
name: land
description:

This spell should be used when the user signals that an open PR should be merged into the PR's base branch
— "land it", "land", "merge it", "merge the PR", "merge to master".
Checks that the branch still carries the commits [[run pr]] folded it into and runs `land-pr`,
which merges the open PR for the current branch in one shot (precondition checks including green CI, a rebase merge that writes nothing to the branch, remote branch cleanup),
then records a `merged` comment on the task and closes it to done unless the user explicitly said to keep it open.
On APPROVED, [[run pr]] chains into this spell.
Also covers landing without a pull request
— a repo that takes changes straight onto its target branch:
the rebase-and-push one-liner, and the CI dispatch that stands in for the checks no PR is there to run.

version: 5.1.0
---

# land

Merge an approved PR for the current branch into the PR's base branch.
The terminal action of a dev session.

The mechanical work is one command (`land-pr`);
your job is the judgment calls around it
— waivers, task closure
— and doing the whole thing in as few responses as possible.
Post-approval latency is model round trips, so batch aggressively:
never spend a turn on a single call that can share a response with another.
The one thing worth waiting for is CI:
`land-pr` blocks on it for you (step 2), so budget the tool timeout for that wait instead of trimming it.

No suite run before the merge:
the branch was verified at its last push, the land publishes nothing of its own, and the merged result on the PR's base branch is verified anyway by the next session that builds on it
— a pre-merge pass adds latency and no evidence.

## Step 1 — check the branch is in landing shape

What `<base>` gets is the commits the branch carries:
[[run pr]] folded them into that shape before review, and `land-pr` writes nothing to the branch.
Read them once before merging:

```bash
git log origin/<base>..HEAD --oneline    # <base> is the PR's base branch, master unless it says otherwise
```

Messages and all, that is what lands.
If they still read as working commits
— a checkpoint, a review fix standing on its own
— the branch never went through [[run pr]]'s fold step, and merging would put that noise on `<base>`.
Fold it now per that step and force-push, then wait for the approval the push costs where the repo requires the last push to be approved.

## Step 2 — merge: run `land-pr`

Where an eyebro reviewed this change, its verdict gates the merge before anything else does.
That gate is this session's, not `land-pr`'s:
only the session that summoned the reviewer knows it did, so [[run pr]] carries the verdict back as the summon's result and nothing on GitHub records the delegation.
A reviewer that ran and did not approve blocks the merge whatever `reviewDecision` says
— stop and ask where questions reach the user, `raise` when unattended.
A reviewer that never ran at all (no grant, or a summon denied at launch) leaves the merge to the gate below.

```bash
land-pr
```

One shot, in order:

1. Resolves the PR for the current branch and enforces the preconditions
   — fails with a message and a nonzero exit when the PR is not `OPEN`, its base branch refuses the merge, its body has unchecked `- [ ]` boxes, the worktree sits off the reviewed head, or the repository disallows rebase merging.
   The review half of that is GitHub's own verdict and nothing else:
   changes requested and a review the base requires both refuse, and a base whose rules ask for no review reports none, which `land-pr` merges.
2. Waits for the PR's status checks to conclude (up to `--wait-checks` seconds, default 480) and refuses to merge while any is pending or failed.
   A head no check reported on at all waits the same way and is refused the same way:
   what an empty rollup says is that nothing verified the commits about to land, which is a reason to hold, not a repo without CI.
   **Give the command room to wait**
   — run it with a tool timeout above the wait budget, not the default.
   A timeout expiry is not a verdict:
   re-run `land-pr` (checks that slow are usually minutes from done), never reach for a waiver to get past it.
3. Merges:
   GitHub replays the branch onto the base, so every commit lands with the message and accounting footer it carried through review.
   Then deletes the remote feature branch
   — the local branch and worktree stay untouched.
4. Prints a JSON result:
   `pr`, `url`, `title`, `base`, `merged_sha`, `merged_at`, `commits`, `branch_deleted`.
   Its `base` field supplies `<base>` for the bookkeeping and report below.

Waiver flags map to explicit user statements from this session
— never pass them when [[run pr]]'s APPROVED event chained into this spell.
No flag waives a review:
where the base branch requires one, GitHub refuses the merge whatever this command was told.

- `--allow-unchecked` — the user said to land despite unchecked test-plan boxes.
  Otherwise an unchecked box means nobody verified that item:
  surface the failure output (it lists the boxes) and wait.
- `--ignore-checks` — the user said to merge whatever CI says.
  It covers every state the gate refuses — pending, failed, and nothing reported at all — and `land-pr` names on stderr what it merged past
  — relay that to the user.
  Reach for it only on the user's explicit say-so about *this* PR's checks;
  a red check is otherwise something to fix or re-run (`gh run rerun --failed <run-id>`), never something to route around.
  A repo with no CI is the one standing case where the waiver is the normal answer rather than an exception:
  there is no run to wait for, and the user says so once.

If `land-pr` exits nonzero, surface its stderr and stop
— do not hand-roll the merge with raw `gh` commands, do not invent state.
Someone pushing to the branch between [[run pr]] and [[land]] is what step 1 catches:
what lands is the reviewed head, and `land-pr` refuses outright when the worktree is not on it.

## Step 3 — task bookkeeping + report: one response

First decide task closure.
Two cases block or defer the close:

- **The change needs a deploy or migration to take effect.**
  If it touches code or config that runs in a deployed service, or adds a migration/backfill
  — the repo's own docs say what is deployed
  — the merge alone doesn't make it live:
  the task closes only after the deploy succeeds.
  An explicit instruction in the initial request or task body to close without holding for the deploy (e.g. a staged feature flow that deploys once after integration) overrides this whole case:
  close as instructed and note the deferred deploy in the report.
  Otherwise hand the rollout to the operations bro in your summon allow-list:
  summon it (per [[ask]]) with a terse deploy request
  — `deploy <service or feature>`, naming the target, not the steps;
  the ops bro infers the spells and sequence itself
  — and a timeout adequate for the rollout (the 1800s default is sized for a typical deploy; raise it when the target plausibly needs longer).
  Base that child on the merged commit
  — pass `land-pr`'s `merged_sha` as the summon's base ref (`--into <merged_sha>`), not as a commit named in the prompt;
  your own HEAD is not what `<base>` now points at.
  Then:
  - deploy succeeded → close the task done as usual and include the ops bro's answer in the report;
  - deploy failed (raised / error / timeout) → leave the task open and report the failure and its reason;
  - no summon client in the session (no broker channel), or no operations bro in the allow-list → leave the task open and report the pending deploy, naming the exact summon command (`call <ops-bro> "deploy …"`).
- **The user said to keep it open.**
  Phrases in the initial prompt like "keep this open", "leave open with notes", or "only landing a subset" mean the task stays in its current status;
  note it in your report.

Then emit everything in a single response
— the report text plus, for sessions with a task, both brog calls in parallel:

- `brog::add_comment(task_id, topic='merged', body='[PR:<n>](<pr-url>) merged into <base>')`
  — `<n>` and `<pr-url>` are the `pr` and `url` fields of `land-pr`'s JSON result.
- `brog::update_task(task_id, status='done')` — unless a bullet above said to keep it open.
- The report, one line:
  PR URL, "merged into `<base>`" (as N commits when the result's `commits` is more than one), and task status
  — closed to done (after a successful deploy handoff when one was needed), left open per instruction, or left open on a failed or pending deploy (with the reason, or the pending summon command).

## Landing without a pull request

A repo that takes its changes straight onto the target branch never opens one, so nothing above applies:
`land-pr` resolves a pull request, and there is none to resolve.
The merge is a fast-forward push of the rebased branch
— but the pull request was carrying both the review and the CI gate, and neither goes with it, so both fall due *before* that push rather than after it.
The push is the last thing this section does.

Rebase first:

```bash
git fetch origin && git rebase origin/<base>
```
{{when #may_summon contains eyebro}}

Then the review.
There is no PR for the eyebro to review afterwards, so its diff review is the whole review.
[[ask]] it for one and wait on the findings
— the child bases on this workspace's HEAD and shares no context, so the relayed request names what to diff against:

    [[review diff of HEAD against origin/<base>, in terms of the development style policy and the repository's own guides]]

Then repeat the round
— fix, re-ask
— until it comes back clean;
each round is a fresh child that re-reads the branch, so what it judges is always the tree as it now stands.
Its verdict gates the push the way it gates a merge:
a reviewer that ran and did not come back clean stops the landing, and that is a question for the user or a `raise`, never something to push past.
A summon denied at launch is no review either, and nothing here supplies one in its place:
report that none ran and leave the push to the user, `raise` when unattended.
{{end}}

Then the CI gate, which is yours to run here.
A repo's CI commonly triggers on pull requests and on pushes to its trunk, so a branch that opens no PR runs nothing of its own:
dispatch the repo's CI against the branch (its own docs name the workflow and how it is dispatched).

Dispatch it on the ref that carries the merge result.
The rebase moves the tree, so a run against the branch as it stood before it green-lit a tree the push no longer delivers:
push the rebased branch to the remote as a branch of its own and dispatch against that ref.

Only once the review is clean and that run is green does `<base>` get written:

```bash
git push origin HEAD:<base>
```

Step 3's bookkeeping applies unchanged, with the pushed commits in place of the PR link the report and the `merged` comment would otherwise carry.

## Safety rules

- The waiver flags drop this command's own preconditions and nothing beyond them:
  where the repository's rules block a merge, GitHub refuses it whatever was waived.
  Each of them takes the user's say-so from this session.
- Never `--admin` your way past branch protections.
