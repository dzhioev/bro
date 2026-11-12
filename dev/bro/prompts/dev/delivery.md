# Delivery

The delivery plan a change ships under, and the gates that clear its landing.
[[run pr]] resolves, checks and records the plan, [[land]] recovers it where none was handed over, and a coordinator composes one for each stage it delegates.
`<base>` throughout is **the ref as given**, never a branch with an implied origin prefix.

## The plan

| parameter | values | default |
|---|---|---|
| `base` | the ref to land on: `<remote>/<branch>` or `refs/heads/<branch>` | `origin/<the branch default-branch prints>`, only where origin exists |
| `channel` | `github`, `talk` or `local`: where the review conversation and its code travel | `github` |
| `reviewer` | `summon`, `coordinator` or `others` | `summon` where the banner's `may_summon` grants a target answering to `eyebro`, otherwise `others` |
| `approver` | `human` or `summoner` | `human` |
| `lander` | `self`, `summon`, `coordinator` or `others` | `self` |
| `landing` | `merge`, a GitHub rebase merge of a PR, or `push`, a fast-forward of the base | `merge` |
| `gates` | a subset of `verdict`, `checks`, `tests` and `approval` replacing the default whole; an explicitly empty subset selects none | "Gates" below |
| `placement` | `boxed`, `unboxed` or `join`, for the bros the author summons | `boxed` |
| `trust` | GitHub logins explicitly trusted for feedback and approvals | none |

The roles:

- reviewer `summon` is a granted eyebro the author summons, whose quest answer is the verdict;
  `coordinator` is a reviewer the author's summoner arranges, relaying its verdict;
  `others` is the trusted accounts reviewing the PR.
- approver `human` is the user;
  `summoner` is the session that summoned the author.
- lander `self` is the author landing through [[land]];
  `summon` is a granted dev the author summons to land;
  `coordinator` is the summoner, taking over once the author answers ready;
  `others` is a person merging the PR, which is approval and landing together.

Without `verdict` the plan engages no reviewer and records `reviewer none`;
its channel, reviewer and reviewer placement are moot.
The plan opens a PR only for `landing=merge` or an engaged `github` reviewer.

### Resolve the base

Resolve `<base>` with the same rules as `fold-branch`, `check-head` and `land-push`:

- `<remote>/<branch>` means that remote's branch where its first component names a configured remote.
  Fetch that remote before reading the base tip or rebasing; never assume origin.
- `refs/heads/<branch>` means this checkout's local branch; fetch nothing.
- A bare branch is accepted only in a repository without remotes.
  With remotes, refuse it, naming `refs/heads/<branch>` and each `<remote>/<branch>` that exists.
- Refuse an omitted base without origin, naming `base=refs/heads/<branch>` or `base=<remote>/<branch>`; refuse a ref that does not exist, naming existing explicit alternatives, not creating a base.
  For example, an absent `integration/x` names `origin/integration/x` where that exists.

Retain the base ref, its full commit and, for a remote, its URL.
A remote base travels with that URL: another checkout maps it to its own remote at that URL, adding one under an unused name if necessary.
A joined session shares the checkout and adds nothing;
a local base never leaves its checkout.
For a GitHub remote resolve its `<owner>/<repo>` and base branch, and bind **every** `gh` call to that repository, never the one `gh` happens to pick for the checkout.
Use `gh`, never `curl` against `api.github.com`.

### Check the plan

Check a plan before preparing anything for it, refusing with the explicit choice that would work:

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
- `checks` needs CI covering this method, as "Gates" defines it; name a dispatch-enabled documented workflow where applicable, or an explicit gates list using `tests` instead.
- For a push onto GitHub, read `gh api repos/<owner>/<repo>/rules/branches/<base branch>`.
  A rule requiring a PR refuses push, naming `landing=merge`.
  That endpoint omits classic branch protection; the remote enforces it at the push.

Channel and reviewer checks apply **only with `verdict`**, not to moot reviewer choices.
Platform rules always bind, independent of gates.
A refusal stops the delivery where questions reach the user;
unattended, the session rescues its unpushed work and raises with it.
A plan changed by the user or a coordinator is checked and recorded again, never silently substituted.

### Record the plan

State the plan in one line, recorded in the PR body, a lander's prompt and a coordinator's ready answer:

```text
Delivery: base <base> (<remote URL, omitted for a local base>) · channel <channel> · reviewer <role or none> · approver <role> · lander <role> · landing <method> · gates <selected gates>
```

Explicit placement and trust choices join the line when non-default, so a recovered plan keeps them.

A PR's plan is recovered from its body's `Delivery:` line, and a PR without one takes the defaults above.
Either way **the base comes from the PR itself**: its base branch on a remote at its base repository's URL, whatever the line records.
Recorded `reviewer none` denotes no verdict gate, not a reviewer role.
Without a PR or a retained plan, require the caller's explicit plan rather than inventing one.

## Gates

The gates are independent of the landing method:

- `verdict`: the engaged reviewer approved the landing fold's head.
- `checks`: CI concluded green on this exact head.
  CI covers a merge where `gh api 'repos/<owner>/<repo>/actions/runs?event=pull_request&per_page=1' --jq .total_count` is above zero.
  It covers a push only to a GitHub remote whose documented CI workflow accepts `workflow_dispatch`, so a local base or a non-GitHub remote has none.
- `tests`: the whole local gate passed on this head's tree (`<head>^{tree}`) in the landing session's workspace, reporting unavailable stages skipped.
  The author's own run is the reviewer's evidence and its own landing's;
  a different lander runs the gate again itself, even when its prompt carries the author's pass.
- `approval`: the chosen approver's go-ahead for this head.

Absent `gates`, the plan selects `verdict`, `approval`, and `checks` where CI covers the method, otherwise `tests`.
Omitting approval must be explicit, never inferred from an unattended hold.
A gate is absent only where the resolved plan omits it;
missing evidence never removes one.

### What a pass covers

Gates are retained **per full head SHA**, and `tests` per tree:

- `verdict` and `approval` cover their named head, or a rebase of it onto a newer base whose complete one-to-one range-diff shows every commit unchanged (`=`):

  ```bash
  git range-diff --no-color <merge-base of approved head and current base>..<approved head> <current base tip>..HEAD
  ```

  A changed, added or dropped commit ends that coverage;
  keep the approved object reachable for the comparison.
- `checks` cover the exact head they ran on, and no other, even after an unchanged rebase.
- `tests` cover every head naming the tree they passed on, since commit metadata is the verdict's and CI's to check.

An appended fix, a fold, a conflict resolution or someone else's push clears **every gate** but a `tests` pass on an unchanged tree;
never carry an approval across one because the final diff looks similar.
Pending checks mean waiting, and red checks or failed tests return the change to review rather than removing the gate.
Silence clears nothing, whether a watch's, a reviewer's or CI's, and untrusted feedback clears no gate.

### Verdict

The engaged reviewer approves only the landing fold's head, with every selected checks and tests gate passed on it and a clean merge into the base commit the verdict names;
a plan selecting neither needs no checks or tests result.
A summoned reviewer's verdict is its quest answer, relayed by the coordinator for reviewer `coordinator`, never its GitHub approval:

```text
approved <head sha> on <base sha>
checks: <each selected checks/tests result and where it ran; not selected for absent gates>
<each finding>: <how it settled>
```

`blocked <head sha> on <base sha>: <reason>`, with the same body, blocks landing.
Under reviewer `others` the verdict is a trusted account's approving review whose `commit_id` names the landing fold's head.
GitHub's aggregate review decision proves platform standing, never a verdict or an approval.

### Approval

- Approver `human`, where questions reach the user: their go-ahead to land this change, their own request to land it included, clears approval.
  Elsewhere it takes a trusted approving GitHub review of this head from an account marked `User`, never a `Bot`.
  A summoner's prompt is **never** a human's go-ahead.
- Approver `summoner`: its `approved <head>` reply to the author's landing-approval question, not the bare fact that a session was summoned.
