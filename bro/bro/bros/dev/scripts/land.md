---
name: land
description: This skill should be used when the user signals that an open PR should be merged into master — "land it", "land", "merge it", "merge the PR", "merge to master". Runs `land-pr`, which squash-merges the open PR for the current branch in one shot (precondition checks, aggregated token footer injected into the squash body, remote branch cleanup), then records a `merged` comment on the task and closes it to done unless the user explicitly said to keep it open. On APPROVED, `/pr` chains into this skill. Direct push to master (no PR) is a one-liner (`git fetch origin && git rebase origin/master && git push origin HEAD:master`) — not this skill, and host-only: agent sessions commit as the bro identity, and in a container a pre-push fence blocks that identity from pushing to master, so even a manual container session lands through a PR (`land-pr`'s server-side `gh pr merge` never runs the hook).
version: 1.4.0
---

# /land

Merge an approved PR for the current branch into master. The terminal action of a dev session.

The mechanical work is one command (`land-pr`); your job is the judgment calls around it — waivers, task closure — and doing the whole thing in as few responses as possible. Post-approval latency is model round trips, so batch aggressively: never spend a turn on a single call that can share a response with another.

## Step 1 — merge: run `land-pr`

```bash
land-pr
```

One shot, in order:

1. Resolves the PR for the current branch and enforces the preconditions — fails with a message and a nonzero exit when the PR is not `OPEN`, not `APPROVED`, or its body has unchecked `- [ ]` test-plan boxes.
2. Aggregates the branch commits' token footers over the PR's actual base (`setup/claude_commit_footer.py --squash`) and appends the result to the PR body, so the server-side squash commit keeps the session spend. Footerless-commit warnings pass through on stderr — relay them to the user.
3. Squash-merges with the PR's own title and body as the commit subject/body, then deletes the remote feature branch (local branch and worktree stay untouched).
4. Prints a JSON result: `pr`, `url`, `title`, `base`, `squash_sha`, `merged_at`, `merged_at_minutes`, `branch_deleted`.

Waiver flags map to explicit user statements from this session — never pass them when `/pr`'s APPROVED event chained into this skill:

- `--no-review` — the user said to merge without waiting for approval. A `CHANGES_REQUESTED` review is refused regardless; that needs the review resolved, not a waiver.
- `--allow-unchecked` — the user said to land despite unchecked test-plan boxes. Otherwise an unchecked box means nobody verified that item: surface the failure output (it lists the boxes) and wait.

If `land-pr` exits nonzero, surface its stderr and stop — do not hand-roll the merge with raw `gh` commands, do not invent state. If the PR description has materially drifted from what shipped (the user pushed content between `/pr` and `/land`), surface it *before* running — the PR body is what becomes the squash commit body.

## Step 2 — task bookkeeping + report: one response

First decide task closure. Two cases block or defer the close:

- **The change needs a deploy or migration to take effect.** If it touches code/config that runs in a deployed service (the ECS services / emails pipeline — see `infra/CLAUDE.md`) or adds a migration/backfill, the merge alone doesn't make it live — the task closes only after the deploy succeeds. An explicit instruction in the initial request or task body to close without holding for the deploy (the `/feature` stage flow's "the feature deploys once after integration") overrides this whole case: close as instructed and note the deferred deploy in the report. Otherwise hand the rollout to devoops: summon it (per `/ask`) with a terse deploy request — `deploy <service or feature>`, naming the target, not the steps; devoops infers the commit, scripts, and sequence itself — and a timeout adequate for the rollout (the 1800s default is sized for a typical deploy; raise it when the target plausibly needs longer). Then:
  - deploy succeeded → close the task Done as usual and include devoops's answer in the report;
  - deploy failed (raised / error / timeout) → leave the task Live and report the failure and its reason;
  - no summon client in the session (no broker channel) → leave the task Live and report the pending deploy, naming the exact `call devoops "…"` command.
- **The user said to keep it open.** Phrases in the initial prompt like "keep this Live", "leave open with notes", or "only landing a subset" mean the task stays in its current status; note it in your report.

Then emit everything in a single response — the report text plus, for `dive-in` sessions with a task, both brog calls in parallel:

- `brog::add_comment(task_id, topic='merged', body='[PR:<n>](<pr-url>) merged to master')` — `<n>` and `<pr-url>` are the `pr` and `url` fields of `land-pr`'s JSON result.
- `brog::update_task(task_id, status='done')` — unless a bullet above said to keep it open.
- The report, one line: PR URL, "merged to master", and task status — closed-to-Done (after a successful deploy handoff when one was needed), left open per instruction, or left Live on a failed or pending deploy (with the reason, or the `call devoops "…"` command when the deploy is pending).

## Safety rules

- Never bypass GitHub merge requirements beyond the two explicit waiver flags, and only with the user's say-so from this session.
- Never `--admin` your way past branch protections.
