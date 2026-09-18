# A universal watch for the native harness

The design for [native harness: a universal watch that wakes a run on background output](https://github.com/dzhioev/bro/issues/557), reviewed as a document through a temporary pull request:
the settled text replaces the task's `## Design`, and this file is not merged.
Nothing below is implemented.

## Goal

A bro-native run that launched a detached summon has no way to learn it finished.
`OpenAI.send` advances an active turn only by executing tool calls and feeding their results back (`native/bro/native/llms/openai.py`),
a later turn needs an outside caller to invoke `Runner.send(message)` (`native/bro/native/runner.py`),
and `summon_check` runs only once something else re-enters the session.
A child that raises seconds after its summon can go unnoticed for the length of whatever wait the root is holding.

Managed Claude sessions already have their push:
a persistent `summon watch` under Claude's Monitor, delivered in `18749d1` and armed for every session that may summon by PR #540.
Give the native harness its counterpart:
a universal watch — a background command whose output wakes the run as notifications — with `summon watch` as the command that carries summon lifecycle and quest chat,
one declared roster of the commands a persona may run on either harness,
and one way to wait.

## Design

Settled with the user on 2026-09-18 (trail `01m2t7wnf9-s0jn678z-q2rwz7t8`), then reviewed against the code and finalized the same day in the review-and-plan session (trail `01m2tk0kgy-bt2hq1mj-gmamnhye`, this pull request);
what the review changed and why is in `## Design changelog`.
Supersedes the 2026-09-11 sketch from #64, which read summon transitions off a journal cursor and appended them to tool results:
summon-specific, a second implementation of the watch's line policy, and no help for any other background event.

### The shape

The native harness gets the counterpart of Claude's Monitor.
A *watch* is a background command whose output lines reach the model as notifications.
`summon watch` is one such command, so summon lifecycle and quest chat — own-quest messages and refusals included — reach a native run through the same lines a Claude session reads, with the one line policy in `bro/summon.py`.
Every background process is a *job*;
a watch is a job in watch mode.
Waiting collapses to one primitive, `chill`, which ends at the next notification.

Both loops then have the same shape:
a notification that arrives mid-turn is delivered with the next tool result,
one that arrives while an interactive session idles starts a turn,
and a one-shot run ends with its turn, watches included, as a print-mode Claude session does.

### Background processes move to core

`dev/bros/dev/jobs.py` becomes `bro/jobs.py`:
one `Job` per command under `bash -c`, merged stdout and stderr drained into a spool by a reader thread, a per-job read cursor, an exit record, group kill.
A run owns one `Registry`, closed at the run's end (`Runner.__exit__`) with a group kill of whatever still runs;
the atexit backstop stays.
A job has a mode — `fg`, `bg`, or `watch` — which decides what it reports:
a watch reports its output as it arrives and its exit,
a background job only its exit,
and a foreground job nothing while the `job` call that started it waits on it, becoming a background job when that call returns without its exit.
Nothing else is reported.
Two invariants hold whatever the mode:
a job's exit reaches the model exactly once, in the foreground result or as an exit notification, never both;
and so does every output line — a delivery advances the job's cursor over what it rendered, and `poll` reads what no delivery carried.
The blocking `Job.watch` goes with its per-job claim;
the run's one wait is the inbox's.

### The inbox

One per run, owned by the `Runner`:
the wake condition every wait in the run blocks on, and the set of jobs with news — unread output past the cursor for a watch, an unreported exit for any job.
Job threads mark their job and signal;
`chill` and a foreground `job` wait on the condition;
the provider loop drains it.
A drain is a non-blocking read of every marked job under the budget the model already reads tool results under (`bro/base/text_window.py`: `DEFAULT_LIMIT` lines within `BYTE_LIMIT` bytes):
for unread output the head slice with the pending marker for the rest, as `dev::watch` emits today;
for an exit the code with the tail of what the cursor never reached, as the `bash` result keeps it today.
The spool is the only buffer, unbounded as today, so nothing collapses or drops;
what a drain leaves stays pending for the next one or for `poll`.
That is the whole interrupt — no queue of its own, no wake registration per wait site, no transport wake.
The condition is a `threading.Condition`, signalled from job threads and waited on off-loop, so it serves whichever event loop drives the run — `asyncio.run` under `bro run` and `call_text`, Textual's under `ChatApp`.
The `LiveRun` protocol (`bro/bro.py`) gains the inbox and the registry so the service tools reach them,
and the `LLM` constructor (`native/bro/native/llm.py`) takes the inbox so the provider loop can drain it;
`Echo` ignores it.

A notification is `{job, mode, command, kind: output | exited, lines, exit_code?, pending?}`, rendered by one formatter:

```
[job-2 watch `summon watch`]
summon ended failed:raised (request 01m… to eyebro)
summoner says please also cover the manual variant
[...pending: 312 lines / 24.1 KB — poll job-2...]
[job-5 `uv run run-tests --changed` exited (code 1)]
[...skipped before: 1,204 lines / 98.2 KB...]
…the last lines…
```

### Delivery

After a response's tool calls have run (`OpenAI._execute_tool_calls`), the loop drains the inbox into one developer-role input item placed after their `function_call_output` items,
and records a `notification` step:
body the rendered text, extras the turn index, call index, and job ids.
News that arrives while no batch is pending — the model is generating, or the turn's terminal response is in — waits for the next batch or for the turn's end.
At a turn's end an idle interactive chat drains it into a turn of its own, the developer item as the whole input (`Runner.wake()`),
which advances the turn index like a user turn and records the `notification` step where the `user_input` step would be;
a human turn that finds news pending puts the item before the user message;
a one-shot run ends.
An interruption loses nothing:
news not yet drained stays marked in the inbox, and a drained item rides `_pending_input` with the outputs it follows, as an interrupted batch's outputs do today.
The observer gets a `NotificationEvent` (`bro/llm/observer.py`), so `bro run`'s activity stream and both chat surfaces render it live as a notice.

Recording and replay touch every accept-list the kind crosses.
`StepKind` (`bro/llm/tracker.py`) and `BRO_STEP_KINDS` with `BRO_TEXT_BODY_KINDS` (`bro/trails/backends.py`) admit `notification`, its body being text.
The read side has a vocabulary of its own:
`MESSAGE_TYPES` (`bro/trails/model.py`) gains `notification`, `_bro_project` emits it and `BRO_ADAPTER.emitted_message_types` declares it,
and `bro/trails/display/recorded.py` builds a `Notice` record from it, the display's existing shape for text the conversation did not author;
`rewind steps` needs nothing, since it renders a step's kind as a label.
`native/bro/fork.py` replays the step as `{'role': 'developer', 'content': body}` in `_replay_step_items` and lets `latest_fork_point` rest on it when no call is pending
— without both, a trailing notification is silently dropped on resume;
a same-provider fork through `previous_response_id` carries it already, since it was sent to the provider as input.

### Tools

The `bro::` service server on the bro harness (`bro/bro.py:_build_service_server`):

| tool | behavior |
|---|---|
| `job(command, mode='fg' \| 'bg' \| 'watch', timeout_seconds=45, limit)` | roster-gated. `fg` starts the job and waits on the inbox until its exit, returning the exit code and tail-kept output as `bash` does today, `limit` bounding it as it bounds `bash`; when the timeout or any other news ends the wait first it returns `running`, the id, the output so far, and the `poll` call that reads on, and the job is a `bg` job from then on, its exit reported when it comes. `bg` returns the id; only the exit is reported. `watch` returns the id; lines are reported as they arrive. |
| `poll(id, limit, tail)` | non-blocking read through the job's cursor: the oldest unread lines with the pending marker, or with `tail=true` the last `limit` lines, the cursor jumping to the end |
| `kill(id)` | group kill; the exit notification follows |
| `jobs()` | every job: id, mode, command, state, exit code, unread lines |
| `chill(seconds)` | refuses unless a live job exists, since nothing could end it early; capped at 3600 s with the clamp named in the result; returns `{slept, woken}`; an interrupted turn wakes it, so the abandoned wait drops out as `Job.wake` drops a watch today |

Mounting:
`job`, `poll`, `kill`, and `jobs` mount on both wires of the bro harness, since a raw Claude session has no Bash of its own.
On the bare wire the tools reach the run's registry and inbox through `LiveRun`.
On the MCP wire there is no run and no inbox:
the service server owns the registry for the session's lifetime (closed with `BaseBro.close`),
`job` offers `fg` and `bg` only,
a background exit is read with `poll` or `jobs`,
`chill` is absent,
and a foreground `job` is bounded by the MCP call cap, which its `{{when #wire = mcp}}` caution names as the summon tools' do.
Full Claude sessions (harness `claude`) get none of them:
Claude's own tools are the persona's to block or narrow.

The summon tools on the bare wire stop waiting:
`summon` returns the accepted state after the host's acceptance (no `detach` field; a manual summon returns its token and command as today),
`summon_check` reads without blocking (no `wait`),
`summon_say` sends and returns, a question's id in hand (no `wait`).
Answers, questions, replies, and refusals arrive as `summon watch` lines;
a child's answer text is then read with `summon_check`.
The MCP-wire twins and the shell commands are unchanged.

The dev toolset keeps `read_reference`, `read_file`, `write_file`, `edit_file`, `grep`, and `glob`, and drops `bash`, `job`, `watch`, `kill`.
Every persona whose shell came from that mount declares `commands(ANY)` in its place
— `dev`, `eyebro`, `analyst`, `terminal`, and `devoops`, whose `mount(dev_mcp.toolset, 'bash')` becomes the declaration alone;
`lead` declares nothing.
#65 (the multi-job `dev::watch` wait) is subsumed:
`chill` waits on every job at once.

### Authority: one roster of commands

One declaration in `bro.mcp`, `commands(*commands)` or `commands(ANY)`, a new `ToolLayer` field folded per harness (`bro/bro.py:_fold_tool_layers`):

- native:
  the roster `job` accepts in any mode, matched whole and exact;
  `ANY` lifts it;
  with no declaration `job` mounts only when `summon watch` is admitted and then accepts only it;
- Claude:
  the blocked command tools, `Bash` and `Monitor`, narrowed to the roster through the PreToolUse gate (`ride/ride/claude/claude_argv.py:_tool_gate_hooks`, `ride/ride/claude/watch_guard.py`),
  with their task control handed back (`BashOutput`, `KillShell`, `TaskOutput`, `TaskStop`);
  narrowing an unblocked tool stays the error it is today;
  `ANY` narrows nothing.

`claude.watch` (`bro/harness/claude.py`) retires into it.
The `summon watch` admission keeps its rule on both harnesses — a run that may summon, or a summoned run whose summoner may say or question — adding the command to the native roster as it adds it to Monitor's today.
The personas that held a shell through the dev toolset declare `commands(ANY)` (above);
lead declares nothing.
The typed `sh(...)` tools are the other way to reach a command and stay as they are.

### Prompts

`summoner.md` and `summoned.md` become one text each, forked on the surface for the tool:
harness `claude` arms Monitor on `summon watch`,
the bare wire starts `bro::job('summon watch', mode='watch')` once before the first summon,
and a raw session (harness `bro`, wire `mcp`), which has neither, keeps the polling text.
The bare-wire branches add that notifications arrive with tool results, that `bro::chill` is the wait when nothing else remains, and that ending the turn ends a one-shot run.
The "not pushed on this harness" branches go, as does `bro/prompts/AGENTS.md`'s "a native run is never idle".
`holds/attended.md`'s "watcher events still wake the session" becomes true on native without change.

### Interactive surfaces

`call_text` reads stdin on a thread and races it against the inbox condition;
`ChatApp` awaits the inbox in a worker.
Either starts a turn on news while idle and shows it as a notice while a turn runs;
the input stays disabled through a notification's turn as through a user's.
`bro run` renders it in its activity stream.

### Bounds

| bound | default | on overflow |
|---|---|---|
| `chill`, foreground `timeout_seconds` | 3600 s cap | clamped, named in the result |
| output per job per delivery | the tool-output budget: `DEFAULT_LIMIT` lines within `BYTE_LIMIT` bytes | the rest stays in the spool behind the pending marker; the next drain or `poll` reads on |
| exit tail | the same budget, tail-kept as the `bash` result is today | `poll(tail=true)` with a larger `limit` |
| inbox | no bound of its own: at most one output range and one exit per job | — |
| spool per job | unbounded, as today | — |

The one new number is the wait cap:
a cap costs a run one round trip per quiet hour, and buys that a job which hangs without printing or exiting cannot hold the run dark for longer.
Everything else reuses the budget tool results already read under, so the model meets one marker vocabulary.

### Rollout and mixed versions

Three contracts cross a process boundary;
everything else runs from the session's frozen bundle and needs no order.

- **The trails server and the `notification` kind.**
  `_bro_parse` runs where the store is:
  in the trails server for the network backend, in the recording process itself for the local one.
  An old server refuses the kind with HTTP 400, which `NetworkStore` raises as `InvalidRequest` without a retry, and native recording is crash-on-failure, so the run aborts at its first notification.
  The server upgrades first — the `bro` wheel with the `[trails-server]` extra, through `oops/trails/server/deploy.sh` — and no version negotiation exists (`/health` reports nothing but `ok`), so the order is the whole contract;
  the local backend needs none.
- **Trails readers.**
  Projection runs on the server too (`/messages`), and the display's `MESSAGE_TYPES` is a second closed vocabulary:
  a reader older than the server fails loudly on `rewind show` and `rewind grep` of a trail carrying a notification (`unknown message type`), while `rewind steps` and every trail without one are unaffected.
  Readers run from the launcher's bundle, so upgrading the installation after the server closes the gap;
  a ride already running on an older bundle keeps failing on those trails until it restarts.
- **The brotocol.**
  No envelope, kind, transition, or field changes:
  the watch consumes the `events` read the quest chat shipped (#646–#649), and the bare-wire summon tools call client paths that exist (`summon_detached`, `check_summon`, `say` without a wait).
  `PROTOCOL_REVISION` stays 3.
  Host broker and peers are one bundle by construction (`bro/reference/ride.md`, "Runtime bundles"), the `summon watch` a job runs is the bundle's shim,
  and the one mixed pair — a project venv's `bro` against the operator's `ride` — is refused at attach on a revision mismatch as today.

### Rejected

- The journal-cursor seam of the 2026-09-11 sketch:
  summon-specific, a second line policy, no other background event covered.
- Host→peer push:
  #366 settled long-polls, and this is a layer above.
- A trailer inside the tool result's text:
  a distinct input item the trail records as its own kind was preferred.
- General interruption points through a context variable and per-site wake callbacks:
  unnecessary once every wait is a chill on the inbox.
- Waking `poll(wait_seconds)`, `summon_check(wait)`, `summon_say(wait)`, and the blocking `summon` on native:
  those waits no longer exist on the bare wire.
- Automatic resumption of an interrupted wait:
  the model cannot be invoked with a call unanswered, so the wait must return, and re-entering it behind the model's back is hidden control flow;
  every interrupted result names the call that resumes it.
- Framework-armed `summon watch`:
  both surfaces induce it through the prompt, so the model knows what it runs.
- A separate `watch` tool, or `watch: bool` beside `fg`/`bg`:
  `jobs()` lists watches, and the boolean admits a foreground watch its first line would end.
- `watch()` meaning every command:
  an empty list reading as ANY is a trap, and `allow_commands` refuses it today.
- A free-form roster by default on native:
  every bro would get a shell.
- `Monitor` as the name:
  Claude's.
- `dev::job` beside a core watch:
  two tools for one mechanism.
- Keeping `bash` in the dev toolset beside the core `job`:
  two shells for one mechanism, the second outside the roster gate.
- Kill on foreground timeout:
  a background exists now;
  the run's end still kills.
- Raw Claude through the MCP server appending to results:
  Claude's push is Claude's.
- An inbox queue of its own, with a count bound and a collapse rule:
  the spool already buffers everything, so the inbox is a wake condition and a per-job news mark, and nothing is ever dropped.
- Fresh numbers for the delivery budget:
  the budget tool results already read under serves notifications too, so the model meets one marker vocabulary.
- Projecting `notification` as the `harness_event` catch-all so readers older than the server keep rendering:
  it hides the record behind the muted generic shape;
  readers upgrade with the launcher, and an old one fails loudly on the new type rather than quietly.

### Risks

- The Responses API accepting a developer item after function outputs mid-chain:
  verified by a live probe in the first stage, kept as a repeatable test;
  a `user` item is the fallback under the same step kind.
- A chatty watch:
  bounded per delivery and killable;
  the prompts already keep says rare.
- News ends a foreground wait, so a chatty watch turns long foreground commands into background ones, each interruption costing a round trip;
  the result names the `poll` that reads on, and `chill` waits for the exit.
  A foreground `git push` moved to the background this way keeps running;
  the result says so.
- A run with several jobs receives up to a tool-output budget per job per delivery;
  the count of jobs is the model's own (`jobs()` lists them).
- An idle chat's automatic turn spends tokens unasked, as on Claude.

### Verification the stages owe

- `bro/jobs_test.py` (moved):
  modes, the exit-once and line-once invariants, the foreground transitions (exit, timeout, other news → `bg`), group kill at close.
- The inbox:
  the wake condition, the per-job news mark, a drain's budget and pending marker, waking on cancellation.
- `native/bro/native/llms/openai_test.py`:
  delivery after a batch, the recorded `notification` step, a turn of its own, the item before a user message, an interrupted turn losing nothing;
  `native/bro/native/llms/openai_llm_test.py` (new, in the opt-in `llm` stage):
  the live probe that a developer item after function outputs mid-chain is accepted, with the `user` item fallback under the same step kind if it is not;
  `native/bro/fork_test.py`:
  replay, the fork point, a trailing notification kept on resume.
- `bro/bro_test.py`:
  `chill`'s refusal, cap, and wake;
  the bare-wire summon tool shapes;
  the roster gate and admission;
  mounting per wire and harness, and the registry's owner on each.
- `bro/mcp_test.py`, `bro/harness/claude_test.py`, `ride/ride/claude/claude_argv_test.py`:
  the `commands` fold on both harnesses, the Bash gate.
- `dev/bros/dev/mcp_test.py` and the persona tests:
  the dropped tools, `commands(ANY)` on every persona that held the shell through the mount.
- `bro/prompts/prompts_test.py`:
  the fragments per surface.
- `native/bro/launch/call_test.py`:
  the automatic turn in both chat surfaces.
- `bro/trails/model_test.py`, `bro/trails/network_test.py`, `bro/trails/server/dynamo_test.py`, `bro/trails/display/recorded_test.py`, `bro/trails/rewind_test.py`:
  the kind accepted at the store, projected, and rendered.
- `ride/ride/e2e_test.py`:
  a native root that watches `summon watch`, summons a detached child that raises, and is woken within seconds;
  a native child steered through its own quest.

## Design changelog

Review-and-plan, 2026-09-18 (trail `01m2tk0kgy-bt2hq1mj-gmamnhye`), against the code the design names:

- **The bounds collapse onto the existing tool-output budget, and the inbox is a wake condition rather than a queue.**
  The delivery, tail, and inbox numbers had no derivation;
  `DEFAULT_LIMIT` / `BYTE_LIMIT` and the pending marker are what the model already reads under, and the spool already buffers everything, so nothing needs a count or a collapse rule.
  The wait cap (3600 s) is the one new number, with its reason stated.
  Settled with the user.
- **The `notification` kind is admitted in every list it crosses.**
  `StepKind` in `bro/llm/tracker.py` is a second write-side accept-list;
  the read side has `MESSAGE_TYPES`, `_bro_project`, `emitted_message_types`, and the display;
  `fork.py` needs both `_replay_step_items` and `latest_fork_point`, or a trailing notification is dropped on resume.
- **Rollout names three pairs.**
  Validation runs in the store's process (the server for the network backend, the recorder for the local one) and a refusal aborts the run, so the server upgrades first;
  projection runs on the server too, so readers older than it fail loudly on `rewind show` of a new trail;
  the brotocol is unchanged, and host and peers are one bundle by construction.
- **Every persona whose shell came from the dev toolset declares `commands(ANY)`:**
  `dev`, `eyebro`, `analyst`, `terminal`, and `devoops`, whose scoped `mount(dev_mcp.toolset, 'bash')` breaks at declaration once `bash` is gone.
  The design named only `dev`.
- **Invariants made explicit:**
  a job's exit and every output line reach the model exactly once;
  a foreground job outliving its wait becomes a background job;
  the registry's owner per wire, with no inbox on the MCP wire;
  `chill` wakes on an interrupted turn;
  a notification-started turn advances the turn index.
- **The Responses API probe is a repeatable live test** (`*_llm_test.py`, the opt-in stage), not a one-off run.

## Cleanup that lands with it

- `bros/bro/spells/ask.md`:
  the "no wake-up at all" fork on the client pick, the "Neither" case, and the polling text for native runs — a native session watches `summon watch` and chills.
- `dev/bros/lead/spells/orchestrate.md`:
  the bro branch of the detached-phase loop (check with wait) becomes watch plus chill.
- `dev/bros/dev/spells/run-pr.md` and `dev/bros/eyebro/spells/review-pr.md`:
  the `dev::watch(job_id, wait_seconds=1500)` loops become a watch job on `poll-pr` and chill;
  `dev/bros/dev/spells/bump-bro.md`:
  the `dev::bash` timeout note.
- `dev/bros/dev/__init__.py`, `dev/bros/eyebro/__init__.py`, `dev/bros/analyst/__init__.py`, `dev/bros/terminal/__init__.py`, `oops/bros/devoops/__init__.py`:
  `commands(ANY)` in place of the shell the dev toolset mount gave them.
- `bro/prompts/summoner.md`, `bro/prompts/summoned.md`, `bro/prompts/AGENTS.md`:
  per the Prompts section.
- `dev/bros/dev/REFERENCE.md`:
  the background-jobs section and the `bash` timeout policy move with the module to the core description of `job`, `poll`, `kill`, `jobs`, and `chill`.
- `bro/harness/claude.py`, the root `AGENTS.md` (the service-tool roster, `claude.watch`, the dev toolset), `native/AGENTS.md`, `bro/launch/AGENTS.md`,
  `bro/trails/AGENTS.md` (the record kinds), `bro/reference/ride.md` (the summon surfaces, the bro harness section, `RIDE_MAY_SUMMON`'s Monitor sentence), and the service tool descriptions.

## Provenance

Re-filed from #64, whose managed-Claude half shipped in `18749d1` and PR #540;
this issue carries the native half.
Its first design — the 2026-09-11 sketch on #64, a journal-cursor seam — was superseded in the design session of 2026-09-18 (trail `01m2t7wnf9-s0jn678z-q2rwz7t8`), where the user chose a universal watch over a summon-specific seam.
#65, the multi-job `dev::watch` wait, is subsumed rather than a sibling:
`chill` waits on every job at once.
The own-quest requirement came from #637 (comment "own-quest messages");
`summon watch` already prints those lines, so the watched command carries it.
Originally reflected from trail `01m281fyt9-jgsx1m2e-q9g9aawx`, where a detached eyebro (trail `01m281vxtb-x9j3ndsh-912wc0gd`) raised 22 seconds after the summon and went unnoticed for nine minutes.
Reviewed as a document by bro-eyebro through this pull request in the review-and-plan session of 2026-09-18 (trail `01m2tk0kgy-bt2hq1mj-gmamnhye`);
the review's changes are in `## Design changelog`.
