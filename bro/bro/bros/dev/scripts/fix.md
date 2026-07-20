---
name: fix
description: This script should be used when the user points you at a task and asks you to work on it — "@:fix <task-ref>:@", "пофикси", "fix it", "fix this", "work on this task", "tackle X", "let's do <url>", "do this task". Accepts either an existing task ref or seed text for a new task, reads the task description and comment stream, gathers project + sibling context, plans an approach, records development events as task comments, implements + verifies, and hands off to `@::pr` when the change is ready. The canonical entry point for task-driven PPP work; `dive-in` seeds this script as its first user message.
parameters: {"task?": "ref of the existing task to work on", "new?": "seed text for a new task to create first"}
version: 3.0.0
---

# fix

Resolve a task, plan an approach, implement, and hand off to `@::pr`. Task access goes through the `brog::` tools; a task ref is any form the session's backend accepts natively (URL or id).

## Invocation forms

The script receives these optional arguments:

- `task` — operate on the existing task named by this ref.
- `new` — create a task from this seed first; an empty value means collect the seed details from the user.
- no arguments — ask the user which task to work on.

`task` and `new` are mutually exclusive. Passed values appear in the `# Arguments` section appended by the script tool.

Natural-language triggers ("fix it", "tackle X", "work on this task") follow the same flow; infer which form fits from the phrasing.

## Step 1 — resolve the task

Read the appended `# Arguments` section. Use `task` as the existing task ref, or use `new` as the seed for task creation. If both arguments appear, stop and ask the user to choose one form.

`dive-in` pre-fetches the task metadata, description, and comments and includes them in the launch message. **If they are present inline, use them as your initial read — skip `brog::get_task` / `brog::read_task` / `brog::read_comments` for the task itself.** Otherwise, pick the right tool for the invocation form:

- Existing task: `brog::get_task("<ref>")` + `brog::read_task("<ref>")` + `brog::read_comments("<ref>")`.
- New task: discuss with the user as needed to pin down name, tags, and body (names start with a lowercase letter except proper nouns); call `brog::create_task` with the agreed properties — the task is born open (workable) — and use the returned id for the rest of the flow. Anything richer than name/tags/body (importance, deadline, project) is post-hoc triage on the tracker's own surface, not part of this flow.

**Already-done guard.** For an existing task, check the status and comment stream you just read before doing any work: if the status is terminal (`done`/`dropped`), or the comments already carry a `merged`-topic entry, the work has most likely already landed. Stop and surface it — e.g. *"this task is already `done` (its comments show it was merged) — want me to reopen it, or did you mean a different task?"* — and wait for the user. Don't silently re-do or reopen completed work.

**Workability guard.** If `blocked_by` is non-empty, the task has open blockers. Name them and confirm with the user before starting work — a live blocker may make the work premature or moot.

**Rescue-ref guard.** Scan the comment stream for a `rescue`-topic entry from a prior raised attempt — it names a pushed branch ref carrying that attempt's unlanded commits. If one is present, that work is recoverable: fetch the ref (`git fetch origin <ref>`) and build on it — reset or cherry-pick those commits onto the current base so you continue from where it stopped rather than re-implementing from scratch. If they conflict with intervening changes, resolve them as part of the work, and note in your plan that you're resuming from a rescue ref.

## Step 2 — gather context

- If the task has a project, `get_task().project` already carries its name and summary; `brog::list_tasks(project=<project.id>, status='open')` with a small `limit` shows the open sibling tasks.
- Note any tags — they classify the task domain.

## Step 3 — plan

Synthesize what you learned. What is this task about, what is the goal, what is the project context? Figure out how to achieve it — for coding tasks, explore the codebase; for tasks that need external information, say what you need. Present your understanding and proposed approach, then start working.

## Step 4 — development log

Record development events on the task throughout the session via `brog::add_comment(task_id, topic, body)`. The comment stream gives the task a persistent record of what happened and why.

After the plan is confirmed, record it under the topic `plan`:

```
brog::add_comment(<task-id>, topic='plan', body='<concise summary of what you're going to do and why>')
```

During implementation, add an entry when something non-obvious happens — a design choice changed, a blocker was hit, an unexpected dependency was found — with a short lowercase topic naming the event. Skip routine progress; a future reader only needs the *why*. Keep bodies 2-5 lines.

## Step 5 — implement

Make the change. For anything beyond a small, single-commit edit, **commit completed logical units as you go** rather than leaving the whole change uncommitted until `@::pr` — the session can exhaust its output budget mid-implementation, and uncommitted work is then lost, while committed units survive on the branch as a recoverable checkpoint. Keep each checkpoint green and conventional (run `run-tests`; follow the commit style + footer from the pr script's steps 6-7) so it's landable as-is.

Stop and ask if the approach turns out to need a different direction than you proposed.

### Rescue commits before a raise

Implementing is where the recoverable checkpoints accumulate — so if an unresolvable blocker forces a `raise` here:

{{include fragments/rescue_before_raise.md}}

## Step 6 — verify

Run `./format.sh`, then `run-tests` (`--no-docker` inside a container).{{when #harness = bro}} Give `run-tests` — and any other long command — an explicit large `timeout_seconds` (600 fits); `dev::bash`'s default kills a full suite mid-run.{{end}} A red suite blocks the commit — fix or report, don't commit through failures.

## Step 7 — hand off

When the work is ready to land, invoke `@::pr` — it owns commit hygiene, the commit-message footer, submodule landing, rebase, PR creation, and the review watcher, and chains into `@::land` on approval.

For tasks that don't produce code (investigation, confirming existing behavior, external coordination): there is no pr step. Once the goal is confirmed met, close the task with `brog::update_task(<id>, status='done')`.

## Task closure

`@::pr` and `@::land` together cover code-change closure (`@::land` handles `brog::update_task status='done'` after the merge). For non-code tasks, close per step 7 once the goal is confirmed met.
