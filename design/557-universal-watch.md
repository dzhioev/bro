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

Settled with the user on 2026-09-18 (trail `01m2t7wnf9-s0jn678z-q2rwz7t8`).
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
A job has a mode — `fg`, `bg`, or `watch`.
Every job pushes its exit as a notification carrying the exit code and a bounded output tail;
a watch also pushes its lines as they arrive.
Nothing else is pushed.

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
An idle interactive chat drains it into a turn of its own, the developer item as the whole input (`Runner.wake()`);
a human turn that finds notifications pending puts the item before the user message.
The observer gets a `NotificationEvent` (`bro/llm/observer.py`), so `bro run`'s activity stream and both chat surfaces render it live as its own display record.

Recording and replay:
`BRO_STEP_KINDS` and `BRO_TEXT_BODY_KINDS` (`bro/trails/backends.py`) admit `notification`;
the display projects it as a notification record;
`bro/fork.py` replays it as `{'role': 'developer', 'content': body}` and adds "right after a notification step" to the legal fork points.

### Tools

The `bro::` service server on the bro harness (`bro/bro.py:_build_service_server`):

| tool | behavior |
|---|---|
| `job(command, mode='fg' \| 'bg' \| 'watch', timeout_seconds=45, limit)` | roster-gated. `fg` starts the job and chills until its exit, returning the exit code and tail-kept output as `bash` does today; when the timeout or any other notification ends the chill first it returns `running`, the id, the output so far, and the poll call that reads on, the job continuing in the background. `bg` returns the id; only the exit is pushed. `watch` returns the id; lines are pushed as they arrive. |
| `poll(id, limit, tail)` | non-blocking read through the job's cursor: the oldest unread lines with a pending marker, or with `tail=true` the last `limit` lines |
| `kill(id)` | group kill; the exit notification follows |
| `jobs()` | every job: id, mode, command, state, exit code, unread lines |
| `chill(seconds)` | refuses unless a live job exists, since nothing could end it early; capped at 3600 s with the clamp named in the result; returns `{slept, woken}` |

Mounting:
`job`, `poll`, `kill`, and `jobs` mount on both wires of the bro harness, since a raw Claude session has no Bash of its own;
on the MCP wire `job` offers `fg` and `bg` only and `chill` is absent, since the push has no consumer there.
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
The dev persona declares `commands(ANY)`;
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

`call_text` reads stdin on a thread and races it against the inbox event;
`ChatApp` awaits the inbox in a worker.
Either starts a turn on a notification while idle and shows it as a notice while a turn runs.
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

The `notification` kind is a wire contract with the trails server:
an old server refuses it (`_bro_parse`) and recording is crash-on-failure, so the trails server upgrades before any native run records one.
Everything else runs from the session's frozen bundle and needs no order.

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
- Kill on foreground timeout:
  a background exists now;
  the run's end still kills.
- Raw Claude through the MCP server appending to results:
  Claude's push is Claude's.

### Risks

- The Responses API accepting a developer item after function outputs mid-chain:
  verified by a live probe in the first stage;
  a `user` item is the fallback under the same step kind.
- A chatty watch:
  bounded per delivery and killable;
  the prompts already keep says rare.
- An idle chat's automatic turn spends tokens unasked, as on Claude.
- A foreground `git push` moved to the background by a notification keeps running;
  the result says so.

### Verification the stages owe

- `bro/jobs_test.py` (moved):
  modes, exit notifications, the foreground transitions (exit, timeout, another notification), group kill at close.
- The inbox:
  bounds, the collapse, the event.
- `native/bro/native/llms/openai_test.py`:
  delivery after a batch, the recorded `notification` step, a turn of its own, the item before a user message;
  `native/bro/fork_test.py`:
  replay and the fork point.
- `bro/bro_test.py`:
  `chill`'s refusal, cap, and wake;
  the bare-wire summon tool shapes;
  the roster gate and admission;
  mounting per wire and harness.
- `bro/mcp_test.py`, `bro/harness/claude_test.py`, `ride/ride/claude/claude_argv_test.py`:
  the `commands` fold on both harnesses, the Bash gate.
- `bro/prompts/prompts_test.py`:
  the fragments per surface.
- `native/bro/launch/call_test.py`:
  the automatic turn in both chat surfaces.
- `bro/trails/backends_test.py` and the display tests:
  the new kind.
- `ride/ride/e2e_test.py`:
  a native root that watches `summon watch`, summons a detached child that raises, and is woken within seconds;
  a native child steered through its own quest.

## Cleanup that lands with it

- `bros/bro/spells/ask.md`:
  the "no wake-up at all" fork on the client pick, the "Neither" case, and the polling text for native runs — a native session watches `summon watch` and chills.
- `dev/bros/lead/spells/orchestrate.md`:
  the bro branch of the detached-phase loop (check with wait) becomes watch plus chill.
- `dev/bros/dev/spells/run-pr.md` and `dev/bros/eyebro/spells/review-pr.md`:
  the `dev::watch(job_id, wait_seconds=1500)` loops become a watch job on `poll-pr` and chill.
- `bro/prompts/summoner.md`, `bro/prompts/summoned.md`, `bro/prompts/AGENTS.md`:
  per the Prompts section.
- `dev/bros/dev/REFERENCE.md`:
  the background-jobs section and the `bash` timeout policy move with the module to the core description of `job`, `poll`, `kill`, `jobs`, and `chill`.
- `bro/harness/claude.py`, the root `AGENTS.md` (the service-tool roster, `claude.watch`, the dev toolset), `native/AGENTS.md`, `bro/launch/AGENTS.md`,
  `bro/reference/ride.md` (the summon surfaces, the bro harness section, `RIDE_MAY_SUMMON`'s Monitor sentence), and the service tool descriptions.

## Provenance

Re-filed from #64, whose managed-Claude half shipped in `18749d1` and PR #540;
this issue carries the native half.
Its first design — the 2026-09-11 sketch on #64, a journal-cursor seam — was superseded in the design session of 2026-09-18 (trail `01m2t7wnf9-s0jn678z-q2rwz7t8`), where the user chose a universal watch over a summon-specific seam.
#65, the multi-job `dev::watch` wait, is subsumed rather than a sibling:
`chill` waits on every job at once.
The own-quest requirement came from #637 (comment "own-quest messages");
`summon watch` already prints those lines, so the watched command carries it.
Originally reflected from trail `01m281fyt9-jgsx1m2e-q9g9aawx`, where a detached eyebro (trail `01m281vxtb-x9j3ndsh-912wc0gd`) raised 22 seconds after the summon and went unnoticed for nine minutes.
