---
name: review-pr
description:

This spell should be used when the user asks to review a GitHub pull request and drive it to a verdict
— "[[review pr]] 57", "review the PR", "review <pr-url> and approve when it's ready"
— including picking up a review that another reviewer or a died session started.
Reconciles the PR's existing review state, reviews the head, posts findings as PR review comments, watches for the author's answers and pushes with `poll-pr`, re-reviews round by round, and approves once every finding is addressed or conceded.

parameters: {"pr": "pull request URL or number to review"}
version: 1.5.0
---

{{iff #features contains github}}
# review-pr

Drive a GitHub pull request through review to a verdict:
findings land as review comments on the PR, the author's answers and pushed fixes are judged round by round, and approval comes when the change meets the bar
— not before, and not never.

Use `gh` for everything GitHub-related;
do not use `curl` against `api.github.com`.

## 1. Preconditions

`pr-state` answers who you are and what the PR is, in one JSON object.
A PR given as a URL spells `<owner>/<repo>` and `<n>`;
a bare number belongs to the checkout you are in:

```bash
pr-state <owner>/<repo> <n>
```

`viewer` is you.
Compare logins only within `pr-state`'s output:
`gh pr view --json` spells the same accounts differently, so a comparison across the two silently never matches.

- `viewer` equals `pull_request.author`:
  stop — GitHub refuses self-approval, and a review conversation with yourself reviews nothing.
  Report it where questions reach the user;
  `raise` when unattended.
- `pull_request.state` is `merged` or `closed`:
  nothing left to review;
  report and stop.
- `pull_request.draft` is true:
  the author has not asked for review yet — confirm with the user before reviewing a draft.

## 2. Reconcile the existing review state

Always, before reviewing anything
— a PR may already carry review history:
an earlier reviewer session that died, a human who started and handed off, or your own previous rounds.
A fresh PR just reconciles to empty.

The `reviews` and `comments` of the same `pr-state` output are that history:
`comments` is every inline comment, each reply carrying its thread's root in `in_reply_to`.
Threads whose `user` is `viewer` are your own earlier rounds;
the rest are another party's.

Classify every existing thread, whoever opened it:

- **settled** — a later reply or commit addresses it convincingly;
  carry it as resolved.
- **open** — adopt it:
  it joins your findings slate, to be driven to resolution like your own
  — verified against the code, not against a "done" reply.
- **awaiting the author** — an unanswered question;
  adopt it likewise.

The latest review on the PR being `APPROVED` ends your round without a fresh one only when all three hold:
it is **yours** (`user` equal to `viewer` in the same `pr-state` output),
its `commit_id` is still `pull_request.head_sha`,
and nothing is open.
Then report that and stop.

An approval of your own that the head has moved past is not a complete review:
the delta since that commit is what you review.
Another account's approval is that account's verdict and never becomes yours by being read
— anyone who can see a public repository can leave one, and whoever delegated this review is owed a judgement of the branch rather than a relay of someone else's.
Review it in full and reach your own.

A body naming pull requests whose review approved commits this one carries
— an integration PR assembling reviewed stages
— points at earlier rounds of this review held elsewhere.
Read each with `pr-state`:
an approval there is yours on the terms above, when its `user` is `viewer`, and covers the commit it was given for;
another account's carries nothing here either.
What yours still cover is settled by a range-diff of the branch those commits landed on, which the body names, against this PR's head
— fetched refs on both sides, since nothing is checked out yet and a local base may be stale:
`git fetch origin <landed> <pull_request.base> refs/pull/<n>/head && git range-diff $(git merge-base origin/<pull_request.base> origin/<landed>)..origin/<landed> origin/<pull_request.base>..<pull_request.head_sha>`.
A commit shown unchanged is reviewed, and one shown altered
— a conflict resolution, a context shift
— has that delta to judge, the way the delta past your own moved approval is.

## 3. Review the head

1. Check out the code:
   `gh pr checkout <n>`.
   The checkout is for reading and for the narrow checks [[review diff]] step 3 bounds, never for running the PR's own gate;
   the branch is the author's
   — never commit to it, never push it.
2. Judge the PR's diff (`<pull_request.base>...HEAD`) per [[review diff]]
   — its grounding in the repo's standards and its method and criteria (steps 2–3)
   — reviewing the commits as what the base branch will carry.
   Commits step 2 reconciled as reviewed elsewhere are judged on their range-diff deltas and on the integration, which no stage review could see:
   - the seams
     — a mechanism one stage built and a later one consumes, a callback a stage hands to machinery outside the diff
     — judged against the invariant the other side states;
   - the invariants the task's design states for the whole, probed against the integrated behaviour with a read-only check in the checkout wherever one can run it, rather than read for;
   - the design changelog's claims, a deferral's named coverage doing what it says;
   - the docs as one, with no stage's intermediate vocabulary left standing;
   - what a later stage made dead;
   - a finding that recurred across the stage reviews' threads, which points at a root cause no stage saw.
3. This pass's findings plus the adopted open threads are the round's slate.
   Note the head SHA the slate judges — later rounds diff against it.

## 4. Post the round's review

One review per round
— a hail of single-comment reviews spams the author with notifications and scatters the round's context.

Write the review as a JSON file and post it;
the file lives in a scratch directory outside the repo or stays untracked, never staged.
`gh pr review` cannot attach inline comments, the reviews API can:

```bash
cat > /tmp/review.json <<'EOF'
{
  "event": "REQUEST_CHANGES",
  "body": "<the round in one place: what blocks approval, what is minor, anything general>",
  "comments": [
    {"path": "src/x.py", "line": 42, "side": "RIGHT", "body": "<finding, per review's report format>"},
    {"path": "src/y.py", "start_line": 10, "line": 14, "side": "RIGHT", "body": "<multi-line finding>"}
  ]
}
EOF
gh api repos/<owner>/<repo>/pulls/<n>/reviews --input /tmp/review.json
```

- Any blocking finding → `REQUEST_CHANGES`.
  Only questions and minor suggestions → `COMMENT`.
  An empty slate → approve (step 7), with nothing to watch for.
- Inline comments only land on lines the diff touches;
  a finding elsewhere goes into the review body with its `file:line` spelled out.
- An adopted thread gets a reply in place rather than a duplicate comment:
  ```bash
  gh api -X POST repos/<owner>/<repo>/pulls/<n>/comments/<thread_root_id>/replies -f body='<taking this over: …>'
  ```

## 5. Watch for answers

The watcher is one long-lived `poll-pr` process, authenticated through the credential store and filtered to everyone but you:

```bash
poll-pr <owner>/<repo> <n>
```

Events fire for the review parties
— the author (human or app account), the repo owner, and any other reviewer
— and arrive as JSON lines on stdout:

- `{"event": "comment", "id": N, "user": "…", "body": "…", "path": "…", "url": "…"}`
  — a new comment;
  replies to existing review threads fire here too.
- `{"event": "review", "id": N, "user": "…", "state": "APPROVED|CHANGES_REQUESTED|COMMENTED|DISMISSED", "body": "…", "url": "…", "comments": […]}`
  — a new review, its inline comments bundled under `comments`.
- `{"event": "pushed", "pr": N, "head": "…"}`
  — the PR's head moved to a new commit.
- `{"event": "checks", "pr": N, "failing": […]}`
  — the head's checks reached a red or green edge;
  a non-empty `failing` array reports failures, while an empty one reports that every run concluded without failure.
- `{"event": "conflicts", "pr": N}` — the PR became unmergeable into its base.
- `{"event": "merged", "pr": N}` / `{"event": "closed", "pr": N}` — the PR is terminal.
- `{"event": "watch_failed", "pr": N, "source": "…", "reason": "…"}`
  — terminal:
  an event source kept failing past the grace window and the watch ended;
  the process exits right after.

{{iff #wire = bare}}
Start it with `bro::job("poll-pr …", mode="watch")` and keep the returned job id.
Its JSON lines arrive as background-job notifications.
React to every line per step 6, use `bro::poll` when a pending marker says more output remains, then call `bro::chill()` whenever nothing else remains.
A notification that the job exited right after `merged`, `closed`, or `watch_failed` is terminal;
an exit without one means the watcher died.
Reconcile first (step 2) before starting another watch, because a restart baselines existing events as seen.
Stop it with `bro::kill(id=job_id)` when the review ends.

**The watch loop is the rest of the run, until the verdict.**
Approval (step 7) or a terminal PR event ends it, and nothing before does:
return to `bro::chill()` however quiet the PR stays
— the idling is the run working as designed, not a stall to wrap up.
{{eliff #harness = bro}}
The raw MCP surface has no notification wake or `chill`, so it cannot own this persistent review loop.
Do not start a watcher that the run cannot observe;
raise that the review must continue under bro-native or a full Claude session.
{{eliff #harness = claude}}
**MUST launch via the `Monitor` tool with `persistent: true`.
Do NOT use Bash `run_in_background`**
— that only notifies on process exit, so the author's answers would sit silently in the output file.
The harness wakes you on each output event;
react per step 6.
Stop the watcher with `TaskStop` when the review ends.
{{end}}

## 6. React to events

**`comment`** — typically the author responding to a finding:

- a fix claim ("done in `abc123`") — note it;
  the claim settles nothing until the push arrives and the code shows it.
- an answer to a question — judge it on the merits:
  satisfied → reply acknowledging, mark the thread settled;
  it reframes the finding → say what remains, in the thread.
- pushback — weigh it honestly:
  the author being right is a fine outcome;
  concede in the thread and drop the finding.
  Re-explain a finding you still hold at most once
  — a thread deadlocked after one re-explanation each way goes to the user where questions reach them;
  unattended, leave the standing `REQUEST_CHANGES` in place and `raise` naming the deadlocked thread, so the verdict stays truthfully blocked while a human breaks the tie.

**`pushed`** — the author pushed fixes.
Expect a rewritten branch, not appended commits:
the author's flow re-folds the branch into landing shape each round.

1. Re-sync the checkout:
   `gh pr checkout <n>`;
   if the rewritten branch confuses it, `git fetch origin <pull_request.head> && git reset --hard origin/<pull_request.head>` (safe here — the checkout never holds your own commits).
2. See what the round changed:
   `git range-diff <last-reviewed-sha>...<new-head>` when the branch was rewritten, an ordinary diff when it grew.
3. Verify every open finding against the new code
   — the code settles claims, replies do not
   — and review the round's changes themselves for new problems.
4. Post the next round (step 4):
   remaining slate, or approval (step 7) when it is empty.
   Reply in each settled thread so the author sees which points landed.

**`review`** — another party reviewed:

- the repo owner `APPROVED`:
  note it and keep reviewing.
  Their verdict answers what GitHub asks of the merge;
  it does not stand in for the judgement you were delegated, and the session that summoned you waits on yours whatever the PR says.
  Name in your next round which of your findings that approval does not cover, so nobody reads it as covering them.
- another reviewer's review:
  a live co-reviewer, not an abandoned one — their threads are theirs to drive;
  don't duplicate them, and keep your verdict to your own slate.

**`checks` / `conflicts`** — the author's to fix, informational for you:
your verdict judges the code;
the merge gate separately refuses red checks and conflicts, so green CI never substitutes for approval and red CI never blocks it.

**`merged` / `closed`** — the PR is terminal:
report how it ended (and what was still open, if anything) and stop.

**`watch_failed`** — the watch is over and the PR is not.
Reconcile once from `gh` state (step 2), handle what arrived, then report the failing source and reason where questions reach the user, or `raise` with them when unattended.
Do not restart the watcher on the same credential and hope.

## 7. Approve

Every finding on the slate settled — fixed in code, answered convincingly, or conceded:

```bash
gh pr review <n> --approve --body '<one paragraph: what the change does, what the review covered, anything minor left as noted>'
```

The approval is the verdict, and the verdict ends the run:
stop the watcher where one is running, then report the outcome
— the PR, the rounds, what was found, and how each point settled
— and stop.
The merge is the author's flow, not yours to watch.

## Safety rules

- The PR branch is the author's:
  never commit to it, never push it, never resolve its conflicts.
- Scratch files (review JSON drafts, notes) live outside the repo or stay untracked;
  never stage them.
- Never approve to end a long loop:
  an unfinished review ends with the truth on the PR, not with a courtesy verdict.

{{else}}
# review-pr

This run has no GitHub identity:
no `github` credential is in its scope, so nothing here can read the pull request, post a review, or watch for the author's answers.
Judging a checkout still works ([[review diff]]);
driving a pull request to a verdict does not.

The identity comes from whatever launched the run, never from inside it:

- a managed session or a summon reads the project's entry in the host's `~/.bro.json`:
  `projects.<identity>.bros.<bro>.creds` with `github+<instance>`, `<bro>` being the name your banner's `bro` line gives, so every launch of this bro resolves one;
- a summon also takes it on the request:
  grant `github+<instance>`, an instance the summoner's own scope resolves;
- a direct `bro run` or `bro chat` uses the host's own store:
  an instance selected under `user.creds` (or `user.tools.<command>.creds`) or `defaults.creds` in `~/.bro.json`, or the kind's empty instance when nothing selects one.

Whichever account it is, it must differ from the PR author's, which GitHub refuses to let approve.

Report what is missing and how to supply it where questions reach the user;
`raise` with the same when unattended.
The spell cannot run here.
{{end}}
