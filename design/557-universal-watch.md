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
one `Job` per command under `bash -c`, merged stdout and stderr drained by a reader thread into a spool, a per-job read cursor, an exit record, group kill.
The spool is a `tempfile.SpooledTemporaryFile`:
in memory up to a fixed threshold, on disk past it, so a chatty watch that runs for hours costs the runner a file rather than its memory, and nothing is dropped
— the bound is the disk's, as a redirected log's is.
A run owns one `Registry`, closed at the run's end (`Runner.__exit__`) with a group kill of whatever still runs;
the atexit backstop stays.
A job has a mode — `fg`, `bg`, or `watch` — which decides what it reports:
a watch reports its output as it arrives and its exit,
a background job only its exit,
and a foreground job nothing while the `job` call that started it waits on it, becoming a background job when that call returns without its exit.
Nothing else is reported.

What the model reads follows one consumption contract over the cursor.
A *head read* — a watch's delivery, `poll` by default — renders from the cursor forward under the budget and advances the cursor over what it rendered;
the remainder stays pending, announced by the pending marker.
A *tail read* — `poll(tail=true)`, the foreground result, a background job's exit notification — renders the last `limit` lines of the unread range and jumps the cursor to its end;
the skipped middle is announced by the skipped marker and is not delivered later, since the reader chose the tail.
A watch is never tail-read by the framework:
its exit notification carries the unread head slice, so a burst before the exit loses nothing to a jump.
A line therefore reaches the model at most once, and exactly once where every read of its job was a head read.
A job's exit is *consumed* exactly once — by the foreground result, by `kill`, or by the drain, whichever comes first under the job's lock — and reaches the model in that one place;
`kill` waits for the exit it forces (SIGTERM, then SIGKILL after the grace) and returns the code, so no exit notification follows a kill that saw the exit.
The blocking `Job.watch` goes with its per-job claim;
the run's one wait is the inbox's.

### The inbox

One per run, owned by the `Runner`:
the wake condition every wait in the run blocks on, and the set of jobs with news — unread output past the cursor for a watch, an unconsumed exit for any job.
Job threads mark their job and signal;
`chill` and a foreground `job` wait on the condition;
the provider loop drains it.
A drain is a non-blocking read of every marked job under the budget the model already reads tool results under (`bro/base/text_window.py`: `DEFAULT_LIMIT` lines within `BYTE_LIMIT` bytes):
a head read of a watch's unread output, a tail read for a background exit, as the consumption contract above says.
That is the whole interrupt — no queue of its own, no wake registration per wait site, no transport wake.

The condition is a `threading.Condition`, signalled from job threads and waited on off-loop, so it serves whichever event loop drives the run — `asyncio.run` under `bro run` and `call_text`, Textual's under `ChatApp`.
A wait is `Inbox.wait(deadline, cancelled)`:
it returns on news, at the deadline, or once the caller's own `cancelled` event is set
— the per-call `threading.Event` today's `watch` tool passes as `woken`, set from the tool's `CancelledError` handler with a notify —
so an interrupted turn never leaves a worker parked until its deadline.
Waiters consume nothing:
only a drain marks news consumed, and only the provider loop (`OpenAI` after a batch) and `Runner.wake()` drain, so a surface that observes the inbox acts by calling `Runner.wake()` rather than by reading.
The `LiveRun` protocol (`bro/bro.py`) gains the inbox and the registry so the service tools reach them,
and the `LLM` constructor (`native/bro/native/llm.py`) takes the inbox so the provider loop can drain it;
`Echo` ignores it.

A notification is `{job, mode, command, kind: output | exited, lines, exit_code?, pending?}`, rendered by one formatter under a line that says what the text is:

```
[notification: this run's background jobs reported; the lines below are their output]
[job-2 watch `summon watch`]
summon ended failed:raised (request 01m… to eyebro)
summoner says please also cover the manual variant
[...pending: 312 lines / 24.1 KB — poll job-2...]
[job-5 bg `uv run run-tests --changed` exited (code 1)]
[...skipped before: 1,204 lines / 98.2 KB...]
…the last lines…
```

### Delivery

After a response's tool calls have run (`OpenAI._execute_tool_calls`), the loop drains the inbox into one user-role input item placed after their `function_call_output` items,
and records a `notification` step:
body the rendered text, extras the turn index, call index, and job ids.
The item is user-role, the lowest-trust input shape the Responses API has:
a watched command's output is untrusted text — repository content, process logs, a PR comment — and a `developer` item would lend it instruction authority above the user's own words.
The rendered text opens by saying what it is, as the Claude harness's own Monitor notifications do, and the prompts state the trust rule:
a notification is output whose authority is its source's
— a `summon watch` line carries a quest participant's message under the talk the host enforces, which the summoned contract already has the run act on,
while any other command's output is data to read, never an instruction to follow.
News that arrives while no batch is pending — the model is generating, or the turn's terminal response is in — waits for the next batch or for the turn's end.
At a turn's end an idle interactive chat drains it into a turn of its own, the item as the whole input (`Runner.wake()`),
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
`native/bro/fork.py` replays the step as `{'role': 'user', 'content': body}` in `_replay_step_items`
— the one role the step ever has, so a replay reconstructs the conversation the model saw —
and lets `latest_fork_point` rest on it when no call is pending;
without both, a trailing notification is silently dropped on resume;
a same-provider fork through `previous_response_id` carries it already, since it was sent to the provider as input.

### Tools

The `bro::` service server on the bro harness (`bro/bro.py:_build_service_server`):

| tool | behavior |
|---|---|
| `job(command, mode='fg' \| 'bg' \| 'watch', timeout_seconds=45, limit)` | roster-gated. `fg` starts the job and waits on the inbox until its exit, returning the exit code and the tail-kept output under `limit` as `bash` does today, consuming the exit; when the timeout or any other news ends the wait first it returns `running`, the id, the output so far, and the `poll` call that reads on, and the job is a `bg` job from then on, its exit reported when it comes. `bg` returns the id; only the exit is reported. `watch` returns the id; lines are reported as they arrive. `timeout_seconds` is capped at 3600 s, the clamp named in the result. |
| `poll(id, limit, tail)` | non-blocking read through the job's cursor: a head read of the oldest unread lines with the pending marker, or with `tail=true` a tail read of the last `limit` lines |
| `kill(id)` | group kill: SIGTERM, then SIGKILL after the grace; returns the exit it forced and consumes it, so no exit notification follows; unread output stays for `poll` or, for a watch, the next drain |
| `jobs()` | every job: id, mode, command, state, exit code, unread lines |
| `chill(seconds=3600)` | refuses unless a live job exists, since nothing could end it early; `seconds` is capped at 3600 s, the clamp named in the result; returns `{slept, woken}`; an interrupted turn ends it through the wait's own cancel event |

Mounting:
`job`, `poll`, `kill`, and `jobs` mount on both wires of the bro harness, since a raw Claude session has no Bash of its own.
On the bare wire the tools reach the run's registry and inbox through `LiveRun`.
On the MCP wire there is no run and no inbox:
the service server owns the registry and closes it when the serving process stops
— `bro/runtime/mcp_server.py` closes every server it resolved from its HTTP lifespan's exit stack and after its stdio streams close,
which neither path does today (the dev toolset's registry has relied on the atexit backstop),
uvicorn turning the runner's SIGTERM into that graceful shutdown —
`job` offers `fg` and `bg` only,
a background exit is read with `poll` or `jobs`,
`chill` is absent,
and a foreground `job` is bounded by the MCP call cap, which its `{{when #wire = mcp}}` caution names as the summon tools' do.
Full Claude sessions (harness `claude`) get none of them:
Claude's own tools are the persona's to block or narrow.

The summon tools on the bare wire stop waiting:
`summon` returns the accepted state after the host's acceptance (no `detach` field; a manual summon returns its token and command as today);
`summon_check` reads without blocking (no `wait`);
`summon_say(text, request_id?, reply_to?, question=false)` sends and returns — with `question=true` it mints the question's id, the brotocol `message` with `id` that `say` today mints only under `wait`, and returns `{state: question, id}`;
`summon_cancel(request_id)` returns once the host has accepted the cancel (no `timeout`).
Answers, questions, replies, refusals, and the ends a cancel forces arrive as `summon watch` lines;
a child's answer text is then read with `summon_check`.
`bro/summon.py:say` gains `question` beside `wait` (a wait implies a question), and the CLI gains `summon say --question`, a question whose reply arrives on the watch rather than in the call;
the MCP-wire tools are otherwise unchanged.

`summon watch` closes the pre-arm gap.
It arms at the current journal head, so a message the summoner sent while the child was starting — the child's quest exists before its first turn — sits behind the baseline,
and after a dead watch is re-armed so does whatever the children sent meanwhile.
At arm it therefore takes the head `H` from `events {}` first, then reads the own quest (`query {id}`, whose by-id view carries the chat tail) and the live child quests (`query {}`, whose listing carries each record's `pending`),
prints, marked `before the watch`, the requester messages retained in the own quest's tail and every pending question at either end whose `seq` is at most `H`
— every retained chat entry carries the journal sequence of its own event (`Journal._record_chat`), so an entry newer than the head is left to the stream —
and then streams `after: H` as today, so nothing is printed twice.
A re-arm repeats the lines the tail retains, marked, which is noise where the gap was loss.
Both harnesses gain this, since the command is shared.

The dev toolset keeps `read_reference`, `read_file`, `write_file`, `edit_file`, `grep`, and `glob`, and drops `bash`, `job`, `watch`, `kill`.
Every persona whose shell came from that mount declares `shell(ANY)` in its place
— `dev`, `eyebro`, `analyst`, `terminal`, and `devoops`, whose `mount(dev_mcp.toolset, 'bash')` becomes the declaration alone;
`lead` declares nothing.
#65 (the multi-job `dev::watch` wait) is subsumed:
`chill` waits on every job at once.

### Authority: one roster of commands

One declaration in `bro.mcp`, `shell(*commands)` or `shell(ANY)`, a new `ToolLayer` field folded per harness (`bro/bro.py:_fold_tool_layers`):

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
The personas that held a shell through the dev toolset declare `shell(ANY)` (above);
lead declares nothing.
The typed `cli(...)` tools are the other way to reach a command — a fixed argv with no shell between, its parameters derived from the CLI's own argument declarations;
today's `sh(...)`, renamed with its `sh::` namespace to `cli::`, so that `shell('bro list')`, a shell admitting that command line, and `cli('bro list')`, that CLI served as a typed tool, read apart at the declaration.

### Prompts

`summoner.md` and `summoned.md` become one text each, forked on the surface for the tool:
harness `claude` arms Monitor on `summon watch`,
the bare wire starts `bro::job('summon watch', mode='watch')` once before the first summon,
and a raw session (harness `bro`, wire `mcp`), which has neither, keeps the polling text.
The bare-wire branches add that notifications arrive with tool results under the trust rule above — a `summon watch` line is the quest participant's message, any other output is data —
that `bro::chill` is the wait when nothing else remains,
that a question goes out as `bro::summon_say(question=true)` and its reply comes back on the watch,
that `bro::summon_cancel` returns on acceptance with the end arriving on the watch,
and that ending the turn ends a one-shot run.
The "not pushed on this harness" branches go, as does `bro/prompts/AGENTS.md`'s "a native run is never idle".
`holds/attended.md`'s "watcher events still wake the session" becomes true on native without change.

### Interactive surfaces

`call_text` gives stdin one daemon reader thread for the surface's life, handing lines to the loop through a queue, so a blocking read is never abandoned or duplicated:
the idle loop awaits the queue or the inbox, whichever comes first,
a line that arrives during a notification's turn waits for the next,
EOF ends the REPL as today,
and the thread is never joined.
`ChatApp` awaits the inbox in a worker.
Either starts a turn on news while idle;
during a turn a notification appears as a notice at the moment the provider loop drains it after a batch, rendered from the observer's `NotificationEvent`,
and news the model has not yet received shows nothing, since a surface never reads the inbox itself.
The input stays disabled through a notification's turn as through a user's.
`bro run` renders it in its activity stream.

### Bounds

| bound | default | on overflow |
|---|---|---|
| foreground `timeout_seconds` | 45 s, capped at 3600 s | clamped, named in the result |
| `chill(seconds)` | 3600 s, the cap | clamped, named in the result |
| output per job per delivery | the tool-output budget: `DEFAULT_LIMIT` lines within `BYTE_LIMIT` bytes | the rest stays in the spool behind the pending marker; the next drain or `poll` reads on |
| tail read (the foreground result, a background exit, `poll(tail=true)`) | the same budget, tail-kept as the `bash` result is today | the skipped middle is announced by the skipped marker and discarded, since the read chose the tail; a watch's exit is a head read and keeps its remainder pending |
| inbox | no bound of its own: at most one output range and one exit per job | — |
| spool per job | in memory up to a fixed threshold, then a temporary file; nothing dropped | the disk's, as a redirected log's is |

The one new model-visible number is the wait cap:
a cap costs a run one round trip per quiet hour, and buys that a job which hangs without printing or exiting cannot hold the run dark for longer.
Everything else reuses the budget tool results already read under, so the model meets one marker vocabulary;
the spool's memory threshold decides only when a spool moves to disk and changes nothing the model sees.

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
  the watch consumes the `events` and `query` reads the quest chat shipped (#646–#649), a question without a wait is the `message` with `id` the wire already carries, and a cancel without its poll is the `cancel` request as it is.
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
- A `developer`-role item for the notification:
  it lends untrusted command output instruction authority;
  the user role carries the same text at the lowest trust the API has.
- A `user` fallback beside a `developer` item:
  two roles the step would have to record and replay;
  one role, settled before the first stage lands.
- General interruption points through a context variable and per-site wake callbacks:
  unnecessary once every wait is a chill on the inbox;
  a wait's own cancel event is not a registration for events, it is how the wait ends when its caller is gone.
- Waking `poll(wait_seconds)`, `summon_check(wait)`, `summon_say(wait)`, `summon_cancel(timeout)`, and the blocking `summon` on native:
  those waits no longer exist on the bare wire.
- Automatic resumption of an interrupted wait:
  the model cannot be invoked with a call unanswered, so the wait must return, and re-entering it behind the model's back is hidden control flow;
  every interrupted result names the call that resumes it.
- Framework-armed `summon watch`:
  both surfaces induce it through the prompt, so the model knows what it runs;
  the pre-arm gap closes at arm instead.
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
- Reclaiming the spool's consumed prefix in place of spilling to disk:
  it caps nothing, since unread output is what grows;
  the temporary file bounds memory and keeps every byte.
- Projecting `notification` as the `harness_event` catch-all so readers older than the server keep rendering:
  it hides the record behind the muted generic shape;
  readers upgrade with the launcher, and an old one fails loudly on the new type rather than quietly.

### Risks

- The Responses API accepting a user-role item after function outputs mid-chain:
  verified by a live probe in the first stage, kept as a repeatable test;
  there is no alternate role, so if the probe fails the delivery seam is redesigned before the stage lands, and the changelog records it.
- Untrusted text in a notification:
  the user role bounds its authority, the trust rule keeps a command's output data while a `summon watch` line keeps the summoner's voice,
  and the persona's own caution about what it runs bounds its content.
- A chatty watch:
  bounded per delivery, spooled to disk, and killable;
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
  modes, the consumption contract (head and tail reads, the cursor jump, an exit consumed once when `kill`, a foreground wait, and the drain race for it),
  the foreground transitions (exit, timeout, other news → `bg`), the spool past its memory threshold, group kill at close.
- The inbox:
  the wake condition, the per-job news mark, a drain's budget and pending marker, a cancelled wait ending through its own event, waiters consuming nothing.
- `native/bro/native/llms/openai_test.py`:
  delivery after a batch, the recorded `notification` step, a turn of its own, the item before a user message, an interrupted turn losing nothing;
  `native/bro/native/llms/openai_llm_test.py` (new, in the opt-in `llm` stage):
  the live probe that a user-role item after function outputs mid-chain is accepted;
  `native/bro/fork_test.py`:
  replay, the fork point, a trailing notification kept on resume.
- `bro/summon_test.py`:
  the watch's arm-time replay of a summoner message sent before the arm and of pending questions at either end,
  a message committed between the baseline and the query printed once, `say(question=True)` without a wait, `summon say --question`.
- `bro/bro_test.py`:
  `chill`'s refusal, cap, and wake;
  the bare-wire summon tool shapes, the question mode and the cancel returning on acceptance among them;
  the roster gate and admission;
  mounting per wire and harness, and the registry's owner on each.
- `bro/runtime/mcp_server_test.py`:
  the resolved servers closed on the lifespan's exit and after the stdio streams close.
- `bro/mcp_test.py`, `bro/harness/claude_test.py`, `ride/ride/claude/claude_argv_test.py`:
  the `shell` fold on both harnesses, the Bash gate;
  `bro/llm/cli_tool_test.py`:
  the `cli` namespace.
- `dev/bros/dev/mcp_test.py` and the persona tests:
  the dropped tools, `shell(ANY)` on every persona that held the shell through the mount.
- `bro/prompts/prompts_test.py`:
  the fragments per surface.
- `native/bro/launch/call_test.py`:
  the automatic turn in both chat surfaces;
  in `call_text`, a notification winning while a read is pending, the line then read by the next turn, and an exit or answer with a read pending.
- `bro/trails/model_test.py`, `bro/trails/network_test.py`, `bro/trails/server/dynamo_test.py`, `bro/trails/display/recorded_test.py`, `bro/trails/rewind_test.py`:
  the kind accepted at the store, projected, and rendered.
- `ride/ride/e2e_test.py`:
  a native root that watches `summon watch`, summons a detached child that raises, and is woken within seconds;
  a native child steered through its own quest, one message sent before its watch armed.

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
- **Every persona whose shell came from the dev toolset declares `shell(ANY)`:**
  `dev`, `eyebro`, `analyst`, `terminal`, and `devoops`, whose scoped `mount(dev_mcp.toolset, 'bash')` breaks at declaration once `bash` is gone.
  The design named only `dev`.
- **Invariants made explicit:**
  a foreground job outliving its wait becomes a background job;
  the registry's owner per wire, with no inbox on the MCP wire;
  a notification-started turn advances the turn index.
- **The Responses API probe is a repeatable live test** (`*_llm_test.py`, the opt-in stage), not a one-off run.

Review round 1 by bro-eyebro on this pull request, 2026-09-18:

- **The notification is a user-role item, and the only role the step has.**
  A watched command's output is untrusted text, and a `developer` item would lend it instruction authority;
  the `user` fallback went with it, since a step that could carry either role would replay a conversation the model never saw.
- **One consumption contract over the cursor.**
  The line-once claim contradicted the tail reads it named:
  head reads advance the cursor over what they rendered, tail reads jump it and announce the skipped middle, a watch is never tail-read by the framework, and an exit is consumed exactly once by the foreground result, `kill`, or the drain.
- **A wait carries its own cancel event.**
  `off_loop` cannot cancel a parked thread;
  the per-call event today's `watch` tool passes is how an interrupted `chill` or foreground wait ends, and waiters consume nothing.
- **`summon watch` replays at arm.**
  It baselines at the journal head, so a summoner's message during the child's startup was permanently behind it;
  the arm now reads the own quest's tail and every pending question at either end, marked `before the watch`.
- **The bare-wire question and cancel paths exist.**
  `say` mints a question id only under `wait`, so the promised non-blocking question had no path;
  `summon_say` gains `question`, the CLI `--question`, and `summon_cancel`, whose wait the design had missed, returns on acceptance.
- **The MCP-wire teardown is a real path.**
  `BaseBro.close` never reaches the service server and `bro/runtime/mcp_server.py` closes nothing;
  the serving process closes every resolved server on its lifespan's exit and after stdio closes, under the SIGTERM uvicorn turns into a graceful shutdown.
- **The spool spills to disk.**
  An unbounded `StringIO` for the run's life was a memory bound the risks did not name;
  a `SpooledTemporaryFile` bounds memory and keeps every byte.
- **`call_text` owns stdin through one persistent reader thread and a queue**, so a read the inbox outraces is neither abandoned nor duplicated.
- **The bounds table separates defaults from the cap.**

Review round 2 by bro-eyebro, 2026-09-18:

- **The arm-time replay filters by journal sequence.**
  A message committed between the baseline and the query would have printed twice;
  every retained chat entry carries its event's `seq`, so the replay keeps entries at or below the head and the stream carries the rest.
- **The trust rule names the source.**
  "Never instructions" contradicted the summoned contract, which has the run act on its summoner's says and questions;
  a `summon watch` line carries the quest participant's message under the host-enforced talk, any other command's output is data.
- **The tail-read row promises no recovery.**
  A tail read jumps the cursor, so the skipped middle is discarded and announced;
  a watch's exit is a head read.
- **A notification shows on a surface when the provider drains it**, never before, since surfaces do not read the inbox.

Settled with the user after the eyebro's approval, 2026-09-18:

- **The roster declaration is `shell(...)`, and `sh(...)` becomes `cli(...)` with the `cli::` namespace.**
  `commands(...)` named its argument rather than the capability;
  `shell` beside `sh` would have put two names for the two routes to a command a keystroke apart, so the typed route says what it is made from, a CLI.

## Cleanup that lands with it

- `bros/bro/spells/ask.md`:
  the "no wake-up at all" fork on the client pick, the "Neither" case, and the polling text for native runs — a native session watches `summon watch` and chills;
  the question loop's bare-wire branch (`summon_say(question=true)`, the reply on the watch).
- `dev/bros/lead/spells/orchestrate.md`:
  the bro branch of the detached-phase loop (check with wait) becomes watch plus chill.
- `dev/bros/dev/spells/run-pr.md` and `dev/bros/eyebro/spells/review-pr.md`:
  the `dev::watch(job_id, wait_seconds=1500)` loops become a watch job on `poll-pr` and chill;
  `dev/bros/dev/spells/bump-bro.md`:
  the `dev::bash` timeout note.
- `dev/bros/dev/__init__.py`, `dev/bros/eyebro/__init__.py`, `dev/bros/analyst/__init__.py`, `dev/bros/terminal/__init__.py`, `oops/bros/devoops/__init__.py`:
  `shell(ANY)` in place of the shell the dev toolset mount gave them.
- `bro/prompts/summoner.md`, `bro/prompts/summoned.md`, `bro/prompts/AGENTS.md`:
  per the Prompts section, the cancel sentence's bare-wire branch included.
- `dev/bros/dev/REFERENCE.md`:
  the background-jobs section and the `bash` timeout policy move with the module to the core description of `job`, `poll`, `kill`, `jobs`, and `chill`.
- `bro/summon.py` and `bro/reference/ride.md`:
  the watch's arm-time replay, `summon say --question`, and the bare-wire twins of `summon_say`, `summon_check`, and `summon_cancel`.
- `bro/runtime/mcp_server.py` and `bro/runtime/AGENTS.md`:
  the resolved servers closed on shutdown.
- `bro/mcp.py`, `bro/llm/cli_tool.py`, `dev/bros/lead/__init__.py`, `README.md`, `bro/llm/AGENTS.md`, `dev/bros/dev/spells/bump-bro.md`, and the root `AGENTS.md`:
  `sh(...)` becomes `cli(...)` and its namespace `cli::`.
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
