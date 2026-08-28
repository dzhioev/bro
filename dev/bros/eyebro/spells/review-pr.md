---
name: review-pr
description:

This spell should be used when the user asks to review a GitHub pull request and drive it to a verdict
— "[[review pr]] 57", "review the PR", "review <pr-url> and approve when it's ready"
— including picking up a review that another reviewer or a died session started.
Reconciles the PR's existing review state, reviews the head, posts findings as PR review comments, watches for the author's answers and pushes with `poll-pr`, re-reviews round by round, and approves once every finding is addressed or conceded.

parameters: {"pr": "pull request URL or number to review"}
version: 1.0.0
---

# review-pr

Drive a GitHub pull request through review to a verdict:
findings land as review comments on the PR, the author's answers and pushed fixes are judged round by round, and approval comes when the change meets the bar
— not before, and not never.

Use `gh` for everything GitHub-related;
do not use `curl` against `api.github.com`.

## 1. Preconditions

Resolve who you are and what the PR is:

```bash
gh api user --jq .login
gh pr view <pr> --json number,url,state,isDraft,title,body,baseRefName,headRefName,author
```

- Your login equals the author's:
  stop — GitHub refuses self-approval, and a review conversation with yourself reviews nothing.
  Report it where questions reach the user;
  `raise` when unattended.
- `state` is `MERGED` or `CLOSED`:
  nothing left to review;
  report and stop.
- `isDraft` is true:
  the author has not asked for review yet — confirm with the user before reviewing a draft.

## 2. Reconcile the existing review state

Always, before reviewing anything
— a PR may already carry review history:
an earlier reviewer session that died, a human who started and handed off, or your own previous rounds.
A fresh PR just reconciles to empty.

```bash
gh pr view <n> --json reviews
gh api repos/<owner>/<repo>/pulls/<n>/comments   # every inline comment, all threads
```

Classify every existing thread, whoever opened it:

- **settled** — a later reply or commit addresses it convincingly;
  carry it as resolved.
- **open** — adopt it:
  it joins your findings slate, to be driven to resolution like your own
  — verified against the code, not against a "done" reply.
- **awaiting the author** — an unanswered question;
  adopt it likewise.

If the latest review on the PR is `APPROVED` and nothing is open, the review is already complete:
report that and stop
— unless the head moved after that approval, in which case the delta since the approved head is what you review.

## 3. Review the head

1. Check out the code:
   `gh pr checkout <n>`.
   The checkout is for reading and running checks;
   the branch is the author's
   — never commit to it, never push it.
2. Judge the PR's diff (`<baseRefName>...HEAD`) per [[review diff]]
   — its grounding in the repo's standards and its method and criteria (steps 2–3)
   — reviewing the commits as what the base branch will carry.
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
  An empty slate → approve (step 7).
- Inline comments only land on lines the diff touches;
  a finding elsewhere goes into the review body with its `file:line` spelled out.
- An adopted thread gets a reply in place rather than a duplicate comment:
  ```bash
  gh api -X POST repos/<owner>/<repo>/pulls/<n>/comments/<thread_root_id>/replies -f body='<taking this over: …>'
  ```

## 5. Watch for answers

The watcher is one long-lived `poll-pr` process, authenticated through the credential store and filtered to everyone but you:

```bash
poll-pr <owner>/<repo> <n> --self <your-login>
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
- `{"event": "checks", "pr": N, "failing": […]}` — a status check on the head concluded as a failure.
- `{"event": "conflicts", "pr": N}` — the PR became unmergeable into its base.
- `{"event": "merged", "pr": N}` / `{"event": "closed", "pr": N}` — the PR is terminal.
- `{"event": "watch_failed", "pr": N, "source": "…", "reason": "…"}`
  — terminal:
  an event source kept failing past the grace window and the watch ended;
  the process exits right after.

{{iff #harness = bro}}
Run it as a background job and read it iteratively
— a plain `dev::bash` call would kill it at its timeout:

1. `dev::job("poll-pr …")` → note the job id.
2. Loop on `dev::watch(job_id, wait_seconds=1500)`:
   new output → react to every JSON line per step 6, then watch again;
   a bare `running` state line → watch again;
   `exited` right after a `merged`/`closed`/`watch_failed` event → react per step 6, stop looping;
   `exited` with no terminal event → the watcher died:
   reconcile first (step 2 — a restarted watch baselines everything as seen), then start a new job.
3. Stop the watcher with `dev::kill(job_id)` when the review ends.

**The watch loop is the rest of the run.**
Your terminal answer comes only after the verdict is delivered or the PR reaches a terminal state;
keep calling `dev::watch` however quiet the PR stays
— the idling is the run working as designed, not a stall to wrap up.
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
   if the rewritten branch confuses it, `git fetch origin <headRefName> && git reset --hard origin/<headRefName>` (safe here — the checkout never holds your own commits).
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
  their verdict outranks yours.
  Report anything you still held open, stop the watcher, and end the review
  — the standing objections are on the PR for the record.
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

The verdict is yours;
the merge is the author's flow.
Report the outcome:
the PR, the rounds, what was found, and how each point settled.

## Safety rules

- The PR branch is the author's:
  never commit to it, never push it, never resolve its conflicts.
- Scratch files (review JSON drafts, notes) live outside the repo or stay untracked;
  never stage them.
- Never approve to end a long loop:
  an unfinished review ends with the truth on the PR, not with a courtesy verdict.
