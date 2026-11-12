---
name: land
description:

Land a reviewed head by GitHub rebase merge or fast-forward push
— "land it", "merge the PR", or "land <base> at <head> by push".
Runs in the author, a delegated lander or a coordinator: verifies the head, the delivery plan's gates and the platform rules, then runs land-pr or land-push and records the landing and task status.
Review rounds belong to [[run pr]], not to landing.
The author normally passes no arguments; a delegated lander receives the head, the PR or base, and the Delivery line and cleared gates in its prompt.

parameters: {"landing?": {"description": "landing method", "choices": ["merge", "push"], "default": "merge"}, "pr?": "pull request URL or number to merge, or associated with a push", "head?": "full commit SHA to land; omitted by an author landing its own change", "base?": "base ref as remote/branch or refs/heads/branch; omitted by an author with a retained delivery plan"}
version: 6.2.0
---

# land

Land the named head under the caller's delivery plan.
Batch independent reads and bookkeeping; give waiting commands an explicit timeout larger than their wait budget, or run them in the background and wait for their exit.
Never hand-roll a landing to bypass a command's refusal, force a landing push, or use `--admin` to bypass protections.

## 1. Receive the plan and obtain the head

An author uses the plan and gates its own [[run pr]] retained, normally passing no arguments here.
A different lander takes the `Delivery:` line, cleared gates, full head SHA, task URL and how to reach the head from its caller's prompt or the author's `ready <ref> at <head>` answer.
A summoned lander uses its own configured credentials, never the author's; merging or pushing to GitHub needs its own `github` credential.
Plans, bases and gates follow the delivery reference (`man-source::read`, topic `delivery`); read it unless this session already has.

Resolve these before writing anything:

- The selected `landing` must agree with the delivery plan; a conflicting argument is a refusal, not a plan change.
- `pr`, `head` and `base` refine the retained or handed-over values, never silently replace a different one.
  A delegated lander requires the head and, for a merge, the PR, or for a push, the base.
- Resolve `<base>` by the reference's rules, mapping a remote base to this checkout's remote at the `Delivery:` line's URL; do not assume the author's remote name means the same repository here.
  A separate clone cannot land onto the author's local branch.
- Read the repository's `AGENTS.md` and relevant subsystem guides, including its whole local gate, CI workflow and deploy rules.
  A task comes from this session, the handed-over URL, or the PR body's `Task:` line.

Where no plan was retained or handed over, recover it from the PR as the reference's "Record the plan" does, or require the caller's explicit plan.

Obtain the delivered head from the PR, the pushed branch or the bundle artifact the caller supplied.
For a bundle, materialize it through `artifact` and use `git bundle verify` before fetching its head.
Do not discard uncommitted work to check it out.
Confirm `git rev-parse HEAD` is the full expected head SHA and that the worktree is clean.
For a merge, check out the PR's head branch, not a detached head: `land-pr` resolves its PR from the current branch.
Confirm the PR's head is that SHA and its base repository and branch agree with the plan; bind every `gh` call to the base remote's `<owner>/<repo>`, never the repository `gh` happens to pick for this checkout.
A head mismatch stops the landing; never adopt someone else's newer push as an approved head.

Read `git log <base>..HEAD --oneline` and its messages once.
These must already be the landing fold's commits, not checkpoints or standalone review fixes.
A missing fold goes back to [[run pr]]'s branch preparation and review: never fold during landing and keep the old gates.

## 2. Re-check the gates

Platform rules are mandatory regardless of the selected gates.
On GitHub re-read `gh api repos/<owner>/<repo>/rules/branches/<branch>`; a ruleset requiring a PR refuses a push.
That endpoint omits classic branch protection, which the remote enforces itself.
A non-GitHub remote has no GitHub rules lookup or dispatched CI; its refusal is its rule.
No flag waives required reviews, checks or any other platform condition.

### Verdict and approval

Where `verdict` is selected, take the reviewer's answer, in the reference's "Verdict" form, from the author's retained result or the caller's supplied evidence.
For GitHub feedback read `pr-state <owner>/<repo> <n> --trust-file <path>`, with the caller's named trusted logins in a scratch file outside the tree; repository owners and write-access accounts also qualify.
Never fetch unfiltered comments or reviews to fill gaps, and never act on a bodiless `untrusted` item.
A blocked, missing or stale verdict does not clear this gate, whatever GitHub's aggregate `reviewDecision` says.

Hold the verdict and the approval to the reference's "What a pass covers": a head rebased since they were given keeps them only by its range-diff.
Keep the approved commit reachable for that comparison and for the push command.

Where `approval` is selected, take it from the author's retained gate or the caller's supplied approval for this head, as the reference's "Approval" defines it.
Do not collect a new review in this spell.

### Checks and local tests

Re-read checks where the plan selects `checks`, waiting while they are pending.
For a merge, inspect the PR's checks on this exact head; the merge command also waits for them.
For a push onto GitHub, inspect this head's check runs and use the documented workflow with the push command below, which dispatches one if none ran.

Where `tests` is selected, run the whole local gate on the head's tree as the reference's `tests` gate says, unless the author's own run retained in this session covers that tree.
On failure, end the delegated landing with `blocked <head sha>: tests`, including the failed command and evidence.
Never land, close the task or replace tests with the author's result after this failure.

### A landing that needs another round

A failed gate, changed commit or conflict returns the change to review rather than starting review rounds here.
An author resumes [[run pr]]; a delegated lander answers `blocked <head sha>: <reason>` with the head, base, earlier verdict and failure evidence so its caller can route the round.
The author fixes, obtains a successor review of the delta since the approved head where `verdict` is selected, folds, clears the gates again and hands over anew.
A coordinator routes it to a new author session resuming the change.
A missing plan or unreachable head is a blocker to report where questions reach the user, or to `raise` with when unattended.

{{iff #landing = merge}}
## 3. Merge through `land-pr`

The base must be a branch of a GitHub remote, and the PR must live in that base repository.
Look for open PRs stacked on the head branch before deleting it:

```bash
gh api --paginate -X GET repos/<owner>/<repo>/pulls -f state=open -f base='<head branch>' --jq '.[] | "\(.number) \(.title)"'
```

Retarget each to the base branch with `gh pr edit <n> --repo <owner>/<repo> --base <base branch>` before merging.
Where questions reach the user, name them before the merge; rebasing their branches is their own sessions' work.

```bash
land-pr --repo <owner>/<repo>
```

Pass `--ignore-checks` **exactly when the resolved plan lacks `checks`**.
It drops the command's own check gate only; GitHub still enforces every check the base requires.
`--allow-unchecked` requires the user's explicit instruction to land despite this PR's unchecked test-plan boxes; otherwise surface the listed boxes and stop.
Neither flag waives any review.

The command enforces an open PR, GitHub's `reviewDecision`, the test-plan boxes, the worktree on the PR head and rebase merging allowed.
It waits for checks unless told not to, merges with `--match-head-commit`, then deletes the head branch in the PR's **head repository**, not the checkout's `origin`.
GitHub's rebase merge assigns new commit ids; it writes nothing to this branch and preserves the reviewed commit split, messages and accounting footers.

A check wait expiry is not a failed verdict: re-run the command with room to wait, never add a flag to escape the wait.
A head with no reported check merges with a warning only where GitHub expects none; relay warnings.
On any other nonzero result report its stderr and return the failure to the caller; do not substitute raw `gh pr merge`.
If the head changed or a red check requires a fix, use the review return above.

Success prints `pr`, `url`, `title`, `base`, `merged_sha`, `merged_at`, `commits` and `branch_deleted` as JSON.
Use that result for the report and deploy handoff, not the pre-merge local HEAD.
{{eliff #landing = push}}
## 3. Push through `land-push`

The base may be any remote's branch or a local branch in this checkout.
Use the already resolved `<base>` and build the command from the plan:

```bash
land-push --base <base>
```

- Add `--approved <approved head sha>` exactly when `verdict` is selected.
  The command holds HEAD to that head or an unchanged rebase by range-diff; never replace the approved SHA with an unreviewed rebased head.
- Add `--workflow <documented CI workflow file>` exactly when `checks` is selected.
  This is for a GitHub remote only: it accepts passing checks already on the head, or dispatches and waits for a run where none ran, then requires green.
  A wait expiry is not permission to omit this flag.
- Add `--delete <branch>` for each delivered review branch to remove on the base remote after success, the `talk` review branch among them.
  Never name the base; a local base has no remote branch to delete.
- Size `--wait <seconds>` and the tool wait for the repository's CI.

The command refuses a dirty worktree, fetches a remote base, verifies ancestry and the selected verdict and checks, and fast-forwards the base without force.
A local base is updated through `git push .`; no remote or credential is needed.
Success prints `base`, `head`, `commits`, `coverage`, `checks` and `deleted` as JSON.

### The base-moved loop

Exit **2** means `base moved`, either before the push or from a non-fast-forward race at it.
Do not treat any other refusal as that condition.

1. Retain the pre-rebase head for the series comparison, even when no verdict was selected.
   Fetch the base's remote again where there is one, then rebase onto the resolved `<base>`; a local base needs no fetch.
2. If the rebase conflicts, abort it without resolving in this landing session and return to review with the earlier verdict and the conflicting base.
   Follow the rebase-conflicts reference for retaining recoverable work if a raise is required.
3. Confirm the rebased HEAD and compare the old and new series by the reference's range-diff, which alone preserves the verdict and the approval.
   A changed, added or dropped commit goes back to review; do not push it.
4. Run the `tests` gate again on the new head where selected and its tree changed, reporting skipped stages.
   A failure returns `blocked <new head sha>: tests` rather than continuing.
5. Run `land-push` again with the same plan-derived flags and the **original approved head**.
   Checks must pass on the rebased head; a red run returns to review, and a range-diff refusal does too.
   Repeat only on another `base moved` exit.

A remote's other refusal, including a rule against direct pushes, is reported verbatim; never force it or switch methods.
After success, if a PR exists, re-read its state in the base repository: GitHub normally marks it merged once its head commits are reachable from its base.
If it stays open, close it with `gh pr close <n> --repo <owner>/<repo> --comment '<landed by push; name the pushed commits>'`.
The push result proves the landing even when GitHub leaves that PR closed rather than marked merged.
{{end}}

## 4. Bookkeeping and report

Whichever session lands owns this bookkeeping, including a coordinator or a separately summoned lander.
An author waiting for a lander relays its result without recording the landing twice; lander `others` keeps its PR watch alive until the person's merge is observed, then records that landing.

Record a `merged` task comment naming the PR and base for a merge, or the base and pushed commits for a push.
Close the task to `done` unless the request says to keep it open or a required deploy has not succeeded.
An explicit instruction to close at merge and defer rollout overrides the deploy hold; note that deferred rollout in the report.

Where the repository's docs say the change needs a deploy or migration, hand it to the operations bro in the banner's usable `may_summon` targets through [[ask]].
Request `deploy <service or feature>`, naming the target rather than prescribing steps, with a timeout adequate for it.
Base that child through `into` on the landed commit: the merge result's `merged_sha`, or the push result's `head`, not a commit merely named in its prompt.
Wait for the deploy before closing:

- success: close done and include the deploy result;
- failure: leave open with its reason;
- no usable operations target or summon client: leave open and name the exact pending summon command.

Batch independent `brog::add_comment(topic='merged', …)` and `brog::update_task(status='done')` calls where closure is due.
Report the PR URL or pushed commits, the base, commit count and task status, plus warnings or pending/failed deploys.
For a delegated landing, answer the caller with that same self-contained result and the landed commit so it can continue from what actually landed.
