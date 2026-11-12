---
name: run-pr
description:

Deliver a finished change through an explicit review channel, roles, gates and landing method
— "open a PR", "[[run pr]]", "send for review", "ship it", or "resume PR <url-or-number>".
Resolves and checks the delivery plan before preparing, folds the branch into landing shape, publishes only what the plan needs, and drives review rounds and the landing fold to a head-bound verdict.
Channels are github, talk and local; landing is a GitHub rebase merge or a fast-forward push onto a remote or local base ref.
Human approval is required by default; omitting it requires an explicit plan.
With pr, recovers the task and recorded plan and resumes at review through [[follow pr]]'s replay-safe watch.
Hands cleared gates to [[land]] or to the chosen lander, and stays until landing or a terminal result.

parameters: {"base?": "base ref as remote/branch or refs/heads/branch; default origin/<default-branch>; bare names only without remotes", "pr?": "pull request URL or number to resume at review", "channel?": {"description": "review conversation and code delivery channel", "choices": ["github", "talk", "local"], "default": "github"}, "reviewer?": "summon, coordinator or others; default summon where an eyebro is granted, otherwise others", "approver?": "human (default) or summoner", "lander?": "self (default), summon, coordinator or others", "landing?": {"description": "landing method", "choices": ["merge", "push"], "default": "merge"}, "gates?": "replacement subset of verdict, checks, tests, approval; an empty list selects none", "placement?": "boxed (default), unboxed or join, for summoned reviewer and lander", "trust?": "GitHub logins explicitly trusted for feedback and approvals"}
version: 9.1.0
---

# run-pr

Deliver the finished change under the resolved plan, not a substitute that happens to be available.
With `pr`, resume at review; otherwise prepare the branch.
Batch independent reads and bookkeeping.

## Delivery plan

Read the appended `# Arguments`; resolve choices before preparing or publishing and refuse unknown role, placement or gate values.

| parameter | meaning and default |
|---|---|
| `base` | the ref to land on; default `origin/<the branch default-branch prints>` only where origin exists |
| `pr` | an existing PR to resume, otherwise none |
| `channel` | {{insert #channel}} in this rendering |
| `reviewer` | {{iff #may_summon contains eyebro}}`summon` by default: an eyebro target is granted{{else}}`others` by default: no eyebro target is granted{{end}}; also `coordinator` |
| `approver` | `human` by default, or `summoner` |
| `lander` | `self` by default, or `summon`, `coordinator`, `others` |
| `landing` | {{insert #landing}} in this rendering |
| `gates` | replaces the default whole, including an explicitly empty subset |
| `placement` | `boxed` by default, or `unboxed`, `join`; applies to bros you summon |
| `trust` | explicitly trusted GitHub logins, otherwise none |

`<base>` below is **the ref as given**, never a branch with an implied origin prefix.
A remote base travels with that remote's URL; in another checkout map the URL to its own remote, adding one under an unused name if necessary.
Joined sessions share the checkout and add nothing.

### Resolve the base and gates

Read `AGENTS.md` and relevant subsystem guides, including the gate, CI workflow and deploy rules.
Resolve `<base>` with the same rules as `fold-branch`, `check-head` and `land-push`:

- `<remote>/<branch>` means that remote's branch where its first component names a configured remote.
  Fetch that remote before reading the base tip or rebasing; never assume origin.
- `refs/heads/<branch>` means this checkout's local branch; fetch nothing.
- A bare branch is accepted only in a repository without remotes.
  With remotes, refuse it, naming `refs/heads/<branch>` and each `<remote>/<branch>` that exists.
- Refuse an omitted base without origin, naming `base=refs/heads/<branch>` or `base=<remote>/<branch>`; refuse a ref that does not exist, naming existing explicit alternatives, not creating a base.
  For example, an absent `integration/x` names `origin/integration/x` where that exists.

Retain the base ref, full base commit and, for a remote, its URL.
For a GitHub remote resolve its `<owner>/<repo>` and base branch; bind **every** `gh` call to that repository, never the repository gh happens to pick for this checkout.
Use `gh`, never `curl` against `api.github.com`.

The gates are independent of the method:

- `verdict`: the engaged reviewer approved the landing fold's head.
- `checks`: CI concluded green on this exact head.
  CI covers a merge where `gh api 'repos/<owner>/<repo>/actions/runs?event=pull_request&per_page=1' --jq .total_count` is above zero.
  It covers a push only to a GitHub remote whose documented CI workflow accepts `workflow_dispatch`.
- `tests`: the whole local gate passed on this exact head in the landing session's workspace, reporting unavailable stages skipped.
  Your own run is the reviewer's evidence before a handover; another lander must run it again itself.
- `approval`: the chosen approver's go-ahead for this head.

Absent `gates`, select `verdict`, `approval`, and `checks` where CI covers the method, otherwise `tests`.
Omitting approval must be explicit, never inferred from an unattended hold.
Without `verdict`, engage **no reviewer**, record `reviewer none`, skip review handoff and rounds, and make one landing fold.
Its channel, reviewer selection and reviewer placement are moot.
The plan opens a PR only for `landing=merge` or an engaged `github` reviewer.

### Check the plan

Refuse before preparing anything, naming the explicit choice that would work:

- An engaged reviewer on `talk` or `local` must be `summon`; name `reviewer=summon` or `channel=github`.
- A `summon` role needs a granted target answering to `eyebro` for review, or to `dev` with [[land]] for landing.
  Use the banner's granted target name, not an ungranted ancestor it answers to.
  Without it, name the needed grant or an explicit `reviewer=others` on github / `lander=self`; never silently switch.
- An engaged reviewer or lander `coordinator`, or approver `summoner` with approval selected, needs a summoned session and `worker.question` on its quest.
  Name the missing talk right or `reviewer=others`, `lander=self`, `approver=human` as appropriate.
- `placement=join` for an engaged reviewer requires `channel=local`, and any selected join needs `bro.placement=join` authority.
  Name `channel=local`, `placement=boxed`, or the needed launch grant.
- Lander `others` requires `landing=merge`; name `landing=merge` or `lander=self`.
- A local base cannot be carried to a different checkout's lander: `lander=summon` needs `placement=join`; `lander=coordinator` needs the author already joined to its coordinator's party.
  Name a joined lander, `lander=self`, or an explicit remote base.
- An engaged `talk` reviewer needs a remote base; name a remote base or `channel=local`.
- A merge needs a GitHub remote base; name one or `landing=push`.
- Where approval is selected, `approver=human` needs questions that reach the user or a plan that opens a PR.
  Without either, name `approver=summoner` with a speaking summoner, or an explicit `gates` list without approval.
- `checks` needs CI covering this method as defined above; name a dispatch-enabled documented workflow where applicable, or an explicit gates list using `tests` instead.
- For a push onto GitHub, read `gh api repos/<owner>/<repo>/rules/branches/<base branch>`.
  A rule requiring a PR refuses push, naming `landing=merge`.
  That endpoint omits classic branch protection; the remote enforces it at the push.

Channel/reviewer checks apply **only with `verdict`**, not to moot reviewer choices.
Platform rules always bind, independent of gates.
Report a refusal and stop where questions reach the user; unattended, rescue unpushed work as below and `raise` with it.
A plan changed by the user or coordinator is checked and recorded again, never silently substituted.

### State and record the plan

State the plan in one line; record it in the PR body, lander prompt and coordinator ready answer:

```text
Delivery: base <base> (<remote URL, omitted for a local base>) · channel <channel> · reviewer <role or none> · approver <role> · lander <role> · landing <method> · gates <selected gates>
```

Also retain explicit placement and trust choices with the delivery context; include them on the line when non-default so a resume recovers them.

## Resume with `pr`

Resolve the PR repository from its URL or, for a number, the checkout; read metadata:

```bash
gh pr view <pr> --repo <owner>/<repo> --json number,url,state,headRefName,headRefOid,headRepository,headRepositoryOwner,baseRefName,title,body
gh api repos/<owner>/<repo>/pulls/<n> --jq .base.repo.clone_url
```

Read feedback only through trusted `pr-state` or [[follow pr]].

1. Recover the task from the body's `Task:` line and the plan from its `Delivery:` line.
   Without a Delivery line use the defaults above.
   **The base comes from the PR itself**, its base branch on a remote at its base repository's URL, whatever the Delivery line records.
2. If the recorded `channel` or `landing` differs from this rendering, cast [[run pr]] again with `pr` and those recorded choices before continuing.
   Recover other roles, gates, placement and trust; recorded `reviewer none` denotes no verdict gate, not an invocation role.
   Apply an explicit plan change through the plan check and update the line.
3. A merged PR gets [[land]]'s "Bookkeeping and report" only, then stops; a closed PR gets a report and stops.
   Do not attempt a second landing or checkout for a terminal PR.
4. Otherwise check out the PR's head branch unless already on it, with `gh pr checkout <pr> --repo <owner>/<repo>`.
   Do not discard uncommitted work.
   Confirm its SHA matches the PR and recover its push destination, including a fork's head repository.
5. Check the resolved plan, state and record it, then enter at "Review handoff", not branch preparation.
   [[Follow pr]] reconciles the whole replay before any round or gate is acted on.

A reviewer's verdict does not survive the session that owned it.
Start the reviewer again: on github it reconstructs earlier rounds from trusted PR history; talk/local starts afresh on the current head unless this live session retains a predecessor verdict commissioning delta review.
A lost watch resumes here too; silence clears no gate.

## Prepare the branch

Work in the managed workspace or a branch other than the base, never the main checkout's working copy.
Surface unfinished or red work instead of publishing it.

### Survey and format

Read `git status` (no `-uall`), staged and unstaged diffs, and `git log <base>..HEAD --oneline` in parallel.
Stop only if the worktree is clean, nothing untracked needs adding, and no commits are ahead of the base.
A clean branch with unlanded checkpoints continues normally.
Run the repository's formatter and include its changes with their logical units.
Do not pay the gate before rebasing: "Pre-review check" verifies the tree that ships.

### Policy audit

Immediately before **each commit**, call `dev-style-source::read` and the `man-source::read` guides for every area it reaches, then audit that commit's diff against the returned policy text.
State a visible one-line `clean` verdict, or the violations found and fixed, before committing.
Repeat for each commit.

### Docs, splits and commit messages

Sync the repository's docs wherever architecture, modules, commands, spells or behavior changed; bundle them with the relevant concern.
Keep completed checkpoint units until the fold.
Group uncommitted work by concern, not file: feature with docs, rename with consumers, unrelated fixes apart.

Follow repo commit conventions, or `git log --oneline -10` where none exist.
Resolve the task's URL through `brog::get_task` where a task exists, and add the repo's required task metadata.
Do not invent task metadata without a task.
Never hand-write, edit or strip generated trailers, co-author lines or token footers; the hooks own them.

### Commit

Stage **specific paths**, never `git add -A`, `git add .` or `git add -N` (intent-to-add breaks stash-based tooling).
Never stage credentials, key files or minting configs; surface suspected secrets first.
Use a quoted HEREDOC to preserve the message:

```bash
git add path/to/file1 path/to/file2
git commit -m "$(cat <<'EOF'
<imperative subject in the repo's style>

<why and shape where needed>

<task metadata required by the repo>
EOF
)"
```

If a hook fails, fix, re-stage and create a new commit, never `--amend` (that failed commit did not happen) or `--no-verify`.
To verify a test catches a bug by reverting the fix, use `git stash push <fix-file>`, never bare stash which also hides the test.

### Rebase onto `<base>`

Fetch the base's remote if it has one, then `git rebase <base>`; a local base needs no fetch.
When it conflicts, call `rebase-conflicts-source::read` and follow its in-band resolution, escalation and task-comment rules.
Never prepend origin to the given ref.

### Fold the branch

Fold before first review, and once more for landing when nothing is open; **not after every fix**.
Without verdict this one preparation fold is the landing fold.

Read `git log <base>..HEAD --oneline` and decide the landing shape.
One commit is the default for one task; checkpoints are not independent changes.
Keep multiple commits for independent changes, a split already settled by the user or the handing-off workflow (such as a framework bump and its adoptions).

Write an untracked scratch plan, never staged, with one `fold` line per landing commit, oldest first:

```text
fold
<landing subject>

<body and task metadata, exactly as the base will carry them>

fold <sha>
```

A `fold` naming no commits takes everything the other lines leave; alone it folds the whole branch.
A line without a message keeps the first folded commit's message.
Messages follow "Docs, splits and commit messages".

```bash
fold-branch --plan <scratch plan> --base <base>
```

It requires a clean tree and a partition of the commits, and proves the tree unchanged.
A grouping conflict leaves the branch untouched; preserve relative order or group together.
A commit straddling two landing units must first be split with interactive rebase, then grouped.
The fold aggregates the existing accounting footers; spend after the landing fold is not counted.

### Pre-review check

Where the plan selects `checks` and CI covers a push to `<base>`, run `check-head --base <base> --workflow <documented CI workflow file>` on the rebased, folded head instead of a local gate, sizing its wait and tool timeout for CI.
Otherwise run the repository's gate on it, scoped to the diff against **`<base>`** where offered, else the affected tests and documented environment flags;
without CI coverage run the whole gate.
Give a long-running gate an explicit timeout large enough for the full run.

A red or timed-out result blocks publication:
diagnose it (on CI, `gh run view <run id> --repo <owner>/<repo> --log-failed`), fix with new commits, fold again and check the new head, or surface the pre-existing failure;
do not publish over it.
{{iff #landing = push}}A green result is that head's `checks` evidence.{{eliff #landing = merge}}The PR's own run then runs the whole gate; the review handoff does not wait for it.{{end}}
Published CI failures are rounds.

### Verify scope

Read `git log <base>..HEAD --oneline` and confirm the full change matches the intended title and body.
Move unrelated work to its own branch, widen the stated scope explicitly, or ask where questions reach the user; never silently publish a narrower title over a wider change.

## Publish the change

Publish only what this plan needs; a push landing with no engaged reviewer publishes nothing yet.
The review code on `local` travels through the ride, never a review-branch push or remote fetch.
A PR required by `landing=merge` is still published normally; local constrains review delivery, not a separately chosen landing method.

A PR is needed for `landing=merge` or an engaged reviewer on github.
Open it **on the base remote's repository**, from a head branch pushed to that remote, not an assumed origin:

```bash
git push -u <base remote> HEAD:<head branch>
gh pr create --repo <owner>/<repo> --base <base branch> --head <head branch> --title '<title>' --body-file <outside-tree body file>
```

An already-published branch rewritten by the preparation fold needs `--force-with-lease`; never force the base.
A resume uses the PR's head destination, including a fork.
For an engaged talk reviewer, push a review branch on the base remote, reusing the PR head branch where one was opened.
For local review or no reviewer with a push landing, create no remote review branch.

A one-commit PR takes that commit's title; several commits get a concise summary in the same style.
The body carries review context rather than repeating commit text: `Task:` with the task URL where known, the `Delivery:` line, and a `## Test plan` checklist with verified items ticked.
Do not list an unavailable local step as an unchecked promise: unchecked test-plan boxes block landing.
An integration branch of reviewed stages names their PRs and landing branches, so a reviewer can reconcile those approvals.

Report `[#<n>](<pr-url>/files)` at creation and after review-fix pushes.
Record `brog::add_comment(topic='PR opened')` on the task where available, with the PR link and each commit's hash link and subject, using the base remote's repository URL for links.
Show the task hyperlink and what was recorded.
Arm [[follow pr <pr>]] before engaging a reviewer; consume its replay through `replay_complete:` before starting any round or clearing any gate.
Keep that watch alive throughout review and gating.

## Review handoff

For every open PR, including a no-verdict resume, arm [[follow pr <pr>]] if no watch is alive and reconcile its replay before any round or gate.
Then, without `verdict`, skip the reviewer engagement below and "Review rounds"; continue to the final head's selected gates.
Retain the named head, base commit, delivery plan, previous deliveries and the live review's conversation.

{{iff #channel = github}}
### GitHub review

The conversation and code live on the PR.
Push appended fixes normally to its head destination; use `--force-with-lease` only after a fold or rebase.
Keep a trust file **outside the tree**, initially holding `trust` logins, one per line, and pass its path to [[follow pr]].
Only named accounts, engaged reviewers and repository owners or accounts with write access are trusted; never read unfiltered feedback to fill a gap.

- `reviewer=summon`: {{iff #may_summon contains eyebro}}use `bro::summon` targeting the eyebro your banner grants, with timeout `14400`, the plan's placement, and talk `worker.say` and `worker.question`.
  Its self-contained prompt casts `[[review <pr-url> on github]]` and carries the Delivery line, selected gates, named trust, base URL, current full head, and any predecessor evidence.
  Keep its quest id; its **answer**, not its GitHub approval, is the verdict.
  When it asks `reviewing <pr> as <login>`, append that login to your trust file before acknowledging with `bro::quest_say` and the question id.
  The watch rereads the file and releases held events.{{else}}refuse unless a reviewer target is granted; `reviewer=others` is the explicit GitHub alternative.{{end}}
- `reviewer=coordinator`: ask the summoner `review requested: <pr> at <head>`.
  It arranges the reviewer and replies with its announced login; append it to the trust file.
  Once every other author-clearable gate is clear, ask `verdict for <pr> at <head>?` and require its reviewer's verdict line for this head.
- `reviewer=others`: wait on trusted accounts' PR reviews.
  Their approving review with `commit_id` naming the landing fold's head is the verdict; no bot quest is implied.

A GitHub reviewer sharing the author's identity refuses before reviewing, naming `talk` and `local`.
That blocks this plan; never fall back to human review or a different channel silently.
{{eliff #channel = talk}}
### Talk review

The reviewer must be `summon`.
{{iff #may_summon contains eyebro}}Use `bro::summon` targeting the eyebro your banner grants, timeout `14400`, the plan's placement, talk `worker.say` and `worker.question`.
Its prompt casts `[[review <base commit>..<head> on talk]]` and supplies the full head and base SHAs, base ref and **remote URL**, review branch, Delivery line, gates and predecessor evidence.
Keep the quest id; questions carry rounds and its answer is the verdict.{{else}}Refuse until an eyebro target is granted; do not silently use github or others.{{end}}

Deliver each changed head by pushing the **review branch on the base remote**; append fixes with a normal push, and use `--force-with-lease` only after a fold or rebase rewrites that branch.
The reviewer fetches through the supplied remote URL, never an assumed origin.
A fetch refusal names `local` and blocks the plan until explicitly changed.
An unchanged head is delivered by naming it alone, with no push.
{{eliff #channel = local}}
### Local review

The reviewer must be `summon`; neither side uses a remote or credential **for review delivery**.
{{iff #may_summon contains eyebro}}Use `bro::summon` targeting the eyebro your banner grants, timeout `14400`, the plan's placement, talk `worker.say` and `worker.question`.
Its prompt casts `[[review <base commit>..<head> locally]]` and carries the full SHAs, base ref, Delivery line, gates and predecessor evidence.
Keep the quest id; questions carry rounds and its answer is the verdict.{{else}}Refuse until an eyebro target is granted; do not silently use github or others.{{end}}

- Started reviewer (`boxed` or `unboxed`): round 1 is based on your current HEAD, including local-only commits.
  Later changed heads are bundles rooted at the **last head actually delivered**, which the reviewer holds:
  ```bash
  mkdir -p .git/review
  git bundle create .git/review/<head>.bundle <last delivered head>..HEAD
  artifact mint .git/review/<head>.bundle
  ```
  Share the printed ref with `bro::quest_share`, then name it and the full new SHA in the reply.
  End the range at `HEAD`, not a bare SHA; this carries rewrites and rebases too.
  Supply new base objects and a successor's approved predecessor head beside the new head where needed, using additional bundle refs if they are not already reachable.
- Joined reviewer (`placement=join`): leave the shared tree clean at the named head.
  It checks `git --no-optional-locks status --porcelain` and HEAD every turn and never checks out, stages, commits, stashes or resets.
  Ordinary scratch stays outside the tree; its ignored check outputs and the review spell's narrow oversized-report staging exception are its only allowed writes.
- An unchanged head is delivered by naming it alone: **no bundle**, since an empty range is refused.

Never fix a local review delivery failure by pushing or fetching from a remote.
{{end}}

## Review rounds

### Turns and batched answers

On talk/local the named head belongs to the reviewer from your delivery until its next question or final answer.
During its turn change nothing that head names: no commit, push or fold, and in a joined review nothing in the tree.
Queue PR events until your turn returns, even a red check or conflict; do not race the reviewer.
Read oversized round artifacts the reviewer names; a progress message is not a question returning the turn.

Handle all pending feedback as **one batch**, the body and every inline comment even on an APPROVED review:

1. Read each finding, answer questions, fix the ones you accept, and re-explain a contested finding at most once each way.
   A deadlock goes to the user where questions reach them; otherwise the reviewer answers `blocked`, which does not clear verdict.
2. Format and run "Policy audit" immediately before each commit.
   Append fixes with `git commit --fixup=<the landing commit it serves>`, retaining repo-required task metadata and hook-owned footers; never amend or re-fold the whole branch each round.
3. Verify what this round changed: the repo's change-scoped selection against the **last delivered head**, otherwise the affected test files.
   Do not run the full gate for each finding; selected whole tests belong on the landing head.
4. Deliver the exact new full SHA through the rendered channel, or name an unchanged SHA alone without a push or bundle.
   Reply to the batch with each fix/answer and the SHA; talk/local also reports the head's checks as far as they have run.
   Use `bro::quest_say` with the round question's id as `reply_to` on talk/local; the reply gives the reviewer its turn.

On GitHub reply in the PR's threads (inline replies use the thread root id), or a conversation comment for general points; bind calls to the base repository.
Use a quoted HEREDOC or body file for Markdown, never backticks or `$()` inside a double-quoted shell argument.
After a push surface `[#<n>](<pr-url>/files)` again.

### Landing fold and reconfirmation

Nothing is open when the reviewer says it waits for the landing fold; this is **not a verdict**.
Under reviewer `others`, require no trusted open thread and green head checks before folding.
Even a first round without findings requires this landing fold and reconfirmation:

1. Run "Fold the branch" again, grouping fixups into the commits they serve and finalizing the messages.
   Retain the last reviewed head and prove `git diff <last reviewed head> <folded head>` is empty.
   A content change requires another round, not blind reconfirmation.
2. Deliver the folded head through the channel; a rewritten published branch uses `--force-with-lease`, never the base.
   Wait for its selected checks to conclude and run the selected whole `tests` gate on it, reporting skipped stages.
   {{iff #landing = merge}}For `checks`, use the PR's current-head checks from [[follow pr]], not watcher silence or GitHub's review decision.{{eliff #landing = push}}For `checks`, run:
   ```bash
   check-head --base <base> --workflow <documented CI workflow file>
   ```
   Size its wait and tool timeout for CI; retain the JSON head, run and conclusion.
   Red or timed-out checks are not green and cannot be waived.{{end}}
3. Announce the **landing fold**, full head, current base SHA and concluded checks/tests to the reviewer, replying to its round question on talk/local or commenting on the PR on github.
   The reviewer checks the unchanged tree, final split/messages (not footer lines), selected green gates and clean merge into the base commit it names.
4. Split/message findings cause another fold and reconfirmation; content/check/conflict findings cause rounds with the same live reviewer.
   Only its final answer clears verdict; for `others`, a trusted approval must name this folded head.

A verdict reads:

```text
approved <head sha> on <base sha>
checks: <each selected checks/tests result and where it ran>
<each finding>: <how it settled>
```

A `blocked <head sha> on <base sha>: <reason>` answer blocks landing.
Collect a summoned reviewer's answer on its ended notification with `bro::quest_check` even if no PR event fires; a running state means keep waiting, not infer approval from GitHub.

### Failures and successors

A denied summon or reviewer that raises/errors before a verdict blocks verdict: report and stop where questions reach the user, rescue and `raise` unattended.
No reviewer or channel stands in unless the plan is explicitly changed and rechecked.
A timed-out reviewer instead gets a successor **on the same channel and plan**, with the same timeout, placement and talk rights.
Pass its `bro::quest_history` conversation, verbatim, as an artifact if past the message bound; truncated history requires judging the whole change.
Settled findings stay settled where the evidence supports them; github also carries the rounds on the PR.
Repeat for a successor that times out, within this author's run.

After a verdict, a moved base that needs conflict resolution or a CI fix, or a delegated landing's red tests, gets a **successor reviewing only the delta since the approved head**.
Keep that approved head reachable, pass the earlier verdict verbatim as `bro::quest_check` returned it, obtain both heads for the successor, and have it judge their base-to-head range-diff, changed integration and new checks.
On github it also reconstructs its earlier approval from the PR; talk/local receives the approved head beside the new one.
Then fold, clear all selected gates anew and hand over again.
A base move that still merges cleanly, or a rebase whose every commit is unchanged (`=`), needs no new reviewer; checks/tests must rerun on any new head.

## Gates and landing handover

Retain gates **per full head SHA**:
verdict and approval cover the named head or a rebase onto a newer base whose complete one-to-one `git range-diff` shows every commit unchanged (`=`); checks and tests cover the exact head only.
An appended fix, fold, conflict resolution or someone else's push clears **every gate**; never carry an approval across one just because the final diff looks similar.
An unchanged rebase comparison uses `<merge-base of approved head and current base>..<approved head>` against `<current base tip>..HEAD`; keep the approved object reachable.

Without verdict, the preparation fold is the landing fold: run the selected final-head checks/tests without starting review rounds.
{{iff #landing = merge}}Use the PR watch's exact-head checks for a merge; GitHub's platform review decision must also permit the merge.
{{eliff #landing = push}}For push checks, run `check-head --base <base> --workflow <documented CI workflow file>` before gating or handover unless its exact-head green result is retained.{{end}}
Selected tests always mean the whole local gate on the final head, reporting unavailable stages skipped.

- Approver `human`, where questions reach the user: their go-ahead to land this change, including their own initial request, clears approval.
  Elsewhere it requires a trusted GitHub approving review naming this head from `user_type: User`, never a Bot.
  A summoner's prompt is **never** a human's go-ahead; an aggregate `review_decision` proves platform standing, not a human approval.
- Approver `summoner`: ask `approve landing <ref> at <head>?` and require `approved <head>`.
  With reviewer `coordinator`, combine this with `verdict for <pr> at <head>?` once the other author-clearable gates hold; require both the reviewer verdict and approval in the reply.
- Under reviewer `coordinator`, collect its verdict only once the other author-clearable gates hold, combining approval as above; do not wait on a verdict before asking for it.
- Missing human or platform approval with an open PR is an ordinary wait, not a broken run: report what remains and keep watching.
  Record a durable task comment if blocked on a review the base counts; an App's approval may not count for it.
  Untrusted feedback never clears a gate; retain its account/kind/URL for the final report.

Every gate you can clear must hold before handover: all for `self`; all but `tests` for a different lander, which receives your passing tests as evidence and reruns them itself; all but `approval` for `others`, whose merge is the approval.
Platform rules still bind independently.

- `lander=self`: stop your PR watch if one exists, then cast [[land]] with this rendering's `landing` in the **same response**, retaining the plan and gates, and follow it through bookkeeping.
- `lander=summon`: summon the granted dev target with [[land <pr or base> at <head> by <method>]], the plan's placement and current HEAD as its base (or join).
  Supply the Delivery line and remote URL, cleared gates and evidence, task URL and how to obtain the head.
  It resolves its own credentials, not yours; GitHub landing requires a github credential configured for that target.
  Wait for its answer while your PR watch remains alive.
  Relay a successful landing without duplicate bookkeeping; a `blocked <head>: tests` or changed-head failure returns to the successor flow above.
- `lander=coordinator`: answer `ready <ref> at <head>` with the Delivery line, cleared gates, task URL and how the head is reached: PR, pushed branch, or a bundle artifact the summoner can share onward.
  Stop your PR watch and end; the coordinator owns the landing and bookkeeping.
- `lander=others`: report ready once every gate but approval holds, then **keep the PR watch running until `merged`**.
  Its merge is approval and landing; do [[land]]'s bookkeeping then.

A delegated lander's failed attempt is never a landing, and never closes the task.
A self landing that returns to review resumes this live plan and successor flow; do not start again with defaults.

{{when #hold = unattended}}
## Rescue committed work before a raise

Before raising with unpushed commits, abort a conflicted rebase to restore them and retain recoverable work.
For a remote delivery push the working branch, never the base, to the plan's remote (or a fresh `rescue/<session name>` ref if rejected); record a `rescue` task comment and name that ref in the raise.
Without a remote, retain a local rescue ref and mint a complete bundle artifact for the summoner; report it through the quest without adding a remote or borrowing credentials.
Skip when nothing is unpushed.
{{end}}
