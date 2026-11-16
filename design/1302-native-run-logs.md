# Design: native runs record through a session recorder over run logs

This file is the specification of [#1302](https://github.com/dzhioev/bro/issues/1302).
It lives on the branch `design/1302-native-run-logs`, which is never merged:
the pull request that carried it, [#1309](https://github.com/dzhioev/bro/pull/1309), was its design review only,
and closed without merging once the reviewer and the user had approved it.
The design is final;
a decision changed while building it is recorded in the root task's `## Design changelog`.

## 1. Goal, scope and constraints

Goal: the native harness records the way the Claude harness does.
A native run writes its own local record, the *run log* (§3), in a format the harness owns, and never writes to a trail store.
In a managed session, a *session recorder* (§5), started before the session's run and stopped after it, turns that log into the session's trail:
each row of a native trail holds one of the log's step lines verbatim (§6), and the bro trail format reads it, as the Claude trail format reads Claude Code's transcript lines.
Trails become purely a ride extension:
a native run outside a managed session
— `bro run` or `bro chat` on the host, a test's in-process `Runner` —
records no trail and only saves its run log, as Claude Code always saves its transcript.

The change also states the answer bound where a run meets it (§7):
the `answer` tool's description names the bound, an answer past it is refused while the run can still shorten it, and any other result past it is cut to fit with a marker naming the session's trail.

What it enables, for example:

- A trails-server outage no longer fails a native turn:
  the run carries on, its run log keeps growing, and the session's recording health reads `FAILING` until the recorder's writes go through again.
- `bro run dev "…"` on the host contacts no trail store;
  its whole conversation is one file under the runtime root.
- Both harnesses record in one shape
  — the harness writes a local record in its own format, and a recorder beside the session stores that record's lines in the trail verbatim —
  which is the shape [#1288](https://github.com/dzhioev/bro/issues/1288) moves to the host.
- Changing trails, their header, store or projection, never changes the run log;
  changing the run log is a format version the bro trail format learns to read.

Out of scope, each with where it belongs:

- retrying and replaying writes beyond what one recorder lifetime re-attempts (§5.7):
  deduplicating a blaze whose reply was lost, catching up a run log whose recorder stopped first, a retry policy ([#1189](https://github.com/dzhioev/bro/issues/1189));
- how a resumed or forked conversation continues:
  `bro chat --continue-trail` and `bro chat --fork` keep reading trails back from the store, and what replaces `--fork` on the host stays open ([#1303](https://github.com/dzhioev/bro/issues/1303));
- recording from the host, taking the `trails` credential out of sessions, a session's trail-reading tool, recording liveness judged from the store alone, and the summon link written by the host (#1288);
- the broker's limits, the answer bound's value among them ([#1307](https://github.com/dzhioev/bro/issues/1307));
- the Claude harness's recorder, which stays as it is (§13 proposes follow-ups);
  only the Claude harness's result path changes, through §7.

Constraints:

- core imports no other workspace member;
  the run log is the bro harness's own artifact, so its format, writer and reader live in `bro-native`,
  and the bro trail format, in `bro-native-trails`, reads a log's step lines without importing `bro-native`;
- the run never writes to a trail store, reading one back only to fork or continue as today, and the recorder never writes the run log;
- backward compatibility of interfaces is not a constraint (`AGENTS.md`, "Conventions"),
  but the trails already stored are data, and stay readable;
- a violated assumption fails fast, and teardown goes through context managers (the development style policy);
- no outcome depends on one process's write landing before another's read unless a signal establishes the order (the concurrency guide).

## 2. Concepts

- **Run** — one conversation a `bro.native.runner.Runner` drives.
  It opens at `run()`'s start, at the first `send()`, or in `bro.fork.fork()`, and ends with the runner's lifetime;
  a runner whose `run()` is called twice drives two runs.
- **Run log** — the append-only file holding one run (§3);
  an **entry** is one line of it.
- **Step** — an entry of one of the kinds the loop records:
  `system_prompt`, `user_input`, `notification`, `llm_call`, `tool_result` and `error`.
  A step's **step number** is its position among its run's steps, counting from 0;
  the system prompt is step 0.
  A **step line** is a step's line in the run log, without its newline.
- **Tool list** — the tools an `llm_call`'s request offered, which the run log carries once, in a `tools` entry, and names by its digest.
- **Session run** — the run a managed session's runner launches:
  the `bro run` or `bro chat` process `BroHarness.run_session` spawns.
  Any other native run inside a managed session, such as a `bro run` a job starts, is a *nested run*;
  nested runs serve tests and development.
- **Session recorder** — a daemon beside a managed session that writes the session's trail from the harness's own local record.
  This design's is `bro.native.trail-recorder`, which records the session run's log (§5);
  the Claude harness's is `bro.claude.trail-recorder`.
  It is what #1288 and #1303 call the trails writer, and below "the recorder" unqualified is the native one.
- **Line row** and **spread row** — the two shapes of a bro trail row (§6):
  a line row holds one step line verbatim, and every row the recorder writes is one;
  a spread row holds a record's fields beside the store's own, as every bro row written before this change does.
- **Session facts** — what a trail header says about the session rather than the run:
  `location`, `git` and `native.ride_command`, read off the session environment by `bro.trails.record.session.managed_session`, and `summoned_by`, read by `bro.summon.summoned_by_from_env`.
- **Answer bound** — `MAX_ANSWER_BYTES` (64 KiB, `bro/run_lifecycle.py`), the longest result text a mission's result message carries (§7).
- **Trail pointer** (`bro/monitor/trail_pointer.py`), **recording health file** (`bro/monitor/health.py`) and **trail mark** (`bro.run_lifecycle.RunLifecycle.trail`) keep their meanings;
  §5 says who writes them.

## 3. The run log

### 3.1 Location

A run log is the file `<directory>/<run id>.jsonl`.
Its default directory is `<runtime root>/bro/runs/`, the runtime root being `bro.workspace.paths.runtime_base()`, wherever the run runs:
the run never looks at whether it is in a managed session.
A launcher that wants the log elsewhere names the file (§4.1), as the session runner does for the session run (§5.1).
The run id is a fresh `bro.base.lulid.lulid()`, so a directory's run logs sort by the time their runs opened.
Directories are created owner-only (0700), and the file is created owner-only (0600) and exclusively:
a path that already exists is refused, so a path names one run.

| Run | Its run log |
|---|---|
| the session run of `ride along --harness bro dev` | `<workspace>/session/bro/runs/<id>.jsonl`, named by the session runner, which a box sees at `/var/ride/session/bro/runs/<id>.jsonl` |
| `bro run dev "…"` or `bro chat dev` on the host | `~/.local/share/ride/bro/runs/<id>.jsonl` |
| a test's in-process `Runner` | under the per-test `XDG_DATA_HOME` the root `conftest.py` sets |
| a nested run | the default directory, which in a box is the container's own home, since boxes set no `XDG_DATA_HOME` |

The session run's log is among the harness's own session records (`bro/reference/ride.md`, "Workspaces"), so it goes with the workspace.
A run log under the runtime root is kept like the local trail store beside it:
nothing removes it.

### 3.2 Entries

A run log is UTF-8 JSON Lines:
every entry is one line of compact JSON, holding no raw newline and ending in `\n`.
Every entry is an object with a `kind` and an `at`, the UTC time the run wrote it, in `%Y-%m-%dT%H:%M:%S.%fZ`.

- `open` says what the run is:
  - `format` — the run log format, `1`;
  - `version` — the framework version that wrote the log (`bro.base.configs.VERSION`);
  - `bro` — the bro's name;
  - `recipe` — the recipe the run opened with (`Recipe.dump()`);
  - `interactive` — `false` for `run()`, `true` for `send()` and `fork()`;
  - `surface` — the driving surface, such as `ask` or `call`;
  - `hold` — the run's hold;
  - `forked_from` — on a fork only, the trail edge `fork()` continues, `{"trail_id": …, "step_id": …}`.
- `tools` holds one tool list, `tools`, under its digest, `sha256`:
  the hex SHA-256 of the list's canonical JSON (sorted keys, no whitespace, UTF-8), which is the digest the trail store keys tool lists by (`bro.trails.model.tools_sha256`).
  It is written once per distinct list, just before the first `llm_call` that offers it.
- A step carries its `body` and its kind's fields, as the loop records them:
  - `system_prompt` and `user_input` — `body`, the text, and `turn_index`;
  - `notification` — `body`, the text, `turn_index`, `call_index` and `job_ids`;
  - `llm_call` — `body` `{"request": …, "response": …}`, the wire's own record of the exchange, whose request leaves its tools out;
    `tools_sha256`, the digest of the request's tool list;
    `wire`, the wire the exchange went over;
    `turn_index` and `call_index`;
    the wire's own fields, such as `response_id`;
    and `stopped` on a leg that did not complete.
    The call's model is in its body:
    the request's `model` as asked, and the response's `model` as served;
  - `tool_result` — `body`, the tool's output, `turn_index`, `call_index`, `tool_name`, `call_id` and `is_error`;
  - `error` — `body` `{"message": …, "traceback": …}`.
- `end` holds the run's outcome:
  `reason` is `ok`, `raised` or `error`, and `detail` obeys `bro.trails.model.validate_end`, so it is non-empty for `raised` and `error`.

A one-shot run that made one tool call, with bodies, times and the recipe shortened:

```jsonl
{"kind":"open","at":"2026-11-14T21:00:00.000000Z","format":1,"version":"0.1.0","bro":"dev","recipe":{"type":"openai","model":"gpt-5.6",…},"interactive":false,"surface":"ask","hold":"unattended"}
{"kind":"system_prompt","at":"…","body":"…","turn_index":0}
{"kind":"user_input","at":"…","body":"fix the test","turn_index":0}
{"kind":"tools","at":"…","sha256":"9f2c…","tools":[…]}
{"kind":"llm_call","at":"…","body":{"request":{…},"response":{…}},"tools_sha256":"9f2c…","wire":"responses","turn_index":0,"call_index":1,"response_id":"resp_1"}
{"kind":"tool_result","at":"2026-11-14T21:00:03.000000Z","body":"…","turn_index":0,"call_index":1,"tool_name":"bro__banner","call_id":"c1","is_error":false}
{"kind":"llm_call","at":"…","body":{"request":{…},"response":{…}},"tools_sha256":"9f2c…","wire":"responses","turn_index":0,"call_index":2,"response_id":"resp_2"}
{"kind":"end","at":"…","reason":"ok"}
```

Its steps are numbered 0 to 4;
the second `llm_call` offered the same tools as the first, so no second `tools` entry precedes it.

### 3.3 Rules

1. The first entry is the only `open` entry, and its `format` is one the reader knows.
2. Every other entry follows it;
   the step at position n among the steps, counting from 0, is step n.
3. Step 0 is the run's `system_prompt`, with `turn_index` 0.
4. A `tools` entry's `sha256` is its list's digest, and no earlier `tools` entry carries it;
   every `llm_call` names in `tools_sha256` a digest an earlier `tools` entry carries.
5. There is at most one `end` entry, and nothing follows it.
6. Only a line ending in `\n` is written:
   a reader leaves a last line without one for a later read, since the writer may still be writing it.

A reader refuses a run log that breaks rules 1 to 5, naming the file and the line.
A run log may stop short of an `end` entry:
its run is still going, or its process died first.

### 3.4 Writing

Only the run's own process writes its run log, through the *run log tracker*, the `Tracker` (§4.1) that writes a run log;
it serializes its writes under one lock.
Each entry opens the file for appending, writes its whole line and closes it, so the tracker holds no descriptor between entries;
a step is counted once its line is written.

- Opening a run writes the `open` entry, which creates the file, and step 0.
- Every step the loop records (`LLM._track_step`) writes one step, and `step()` returns its step number.
  An `llm_call` takes its request's tool list out of its body:
  a list the log has not carried yet is written as a `tools` entry first, and the step names the list by `tools_sha256` either way.
- The end of the runner's lifetime writes the `end` entry, after the `error` step of a run that failed.
  The `end` entry's detail for an `error` outcome is `<exception type>: <message>`, which is never empty, where today's bare message can be empty and is then refused at the store's end.

A write that fails raises, and the tracker refuses every write after it, since the file may now end in a partial line;
an entry after the `end` is refused too.
A failed open or step therefore fails the run's turn as a failed store write does today, but only for a local I/O failure.
The `error` step and the `end` entry stay best-effort, as today's recorder keeps them:
a failed write of either is logged and leaves the run's own outcome alone, and the log then reads as a run without an end.
An entry is in the file once its write returns;
the tracker does not fsync, so a host crash can lose the tail.

### 3.5 Reading

The reader takes a run log's complete lines past a byte offset, no more than its caller's bound on their count and size admits, though always at least one,
checks them against §3.3 given what it read before, and returns each entry, both parsed and as its line without the newline, with the offset after them.
The session recorder is its one reader in this design;
#1303's continuation and #1288's host recorder are the readers it is shaped for.

## 4. The runner

### 4.1 Recording a run

The runner records its run into the run log and nothing else:
it writes to no trail store, and it writes neither the trail pointer, nor the recording health file, nor the trail mark.

- `Runner(bro, recipe, *, activity_file=None, run_log=None)` — `run_log` is the path the run's log is created at, for a launcher that names it;
  without it each run creates a fresh log in the default directory (§3.1).
  A path names one run, so a runner given one drives one run.
  `Runner.run_log` is the current run's log path once it opens, and None while a replacement tracker records the run.
- `bro run` and `bro chat` take a `--run-log PATH` flag, passed to the runner, and through `bro.launch.resume.resume` to `fork()` for `--continue-trail` and `--fork`.
- The default tracker is the run log tracker.
  A per-run `tracker=` still replaces it, so tests inject recording fakes and `NullTracker` writes nothing.
  `set_default_tracker_factory`, the runner's `TRAILS_DISABLED` check and the root `conftest.py`'s `NullTracker` pin go:
  the default touches nothing outside the per-test root.
- The `Tracker` contract (`bro/llm/tracker.py`) drops the trail:
  `start(bro, recipe, system_prompt, forked_from, interactive, surface, hold)` returns nothing and takes no `summoned_by`;
  `step(kind, body, **extras)` returns the step number where the tracker counts steps;
  `end(reason, detail)` closes the run once.
- `Runner.trail_id` and `LiveRun.trail_id` go.
  `bro::banner` reads the trail off the session's pointer, as the Claude session's tool server already does,
  and `render_banner`'s `trail_id` override goes with them.

### 4.2 Step numbers and the summon edge

The loop maps each tool call of an `llm_call` to `{step_id, index}` from the step number `step()` returned, as it maps the store's ordinal today,
so `current_tool_step_id` names the step the trail will hold:
the recorder writes step n of the log as step n of the trail (§5.3).
A summon carries that position to summon control as before;
the host takes the trail id off the summoning session's pointer (`ride/ride/peer_facts.py`) and hands the child `summoned_by`, which the child's recorder writes into the child's trail.

### 4.3 Lifecycle emission

- `Runner.run()` still sends the run's result over its broker channel, through `RunLifecycle.completed`, which cuts a result longer than the answer bound (§7).
- Neither `run()` nor `send()` sends the trail mark:
  the session recorder does (§5.5).
- `summoned_by` is a session fact the recorder stamps (§5.3);
  the runner no longer reads it.

### 4.4 Surfaces

- `bro chat` ends by naming its run log, `conversation saved to <path>`, whether it created the log or a launcher named it with `--run-log`, and names none when its run never opened;
  in a managed session, `ride`'s `ride resume <workspace>` hint follows it.
  The `bro chat <bro> --fork <trail>` hint goes.
- `bro chat`'s `--fork` help says that the fork's conversation is saved as a run log, which only a managed session's recorder turns into a trail.
- `bro run` and `bro chat` log the run log's path where they logged the trail id.

## 5. The session recorder

### 5.1 Lifetime

`BroHarness.run_session` runs the session run inside the recorder's lifetime:

1. When resuming, it reads the trail to continue off the session's pointer, as today.
   When the recording health file reports a problem (`bro.monitor.health.problem()`: the previous recorder's last beat was an error, or it stopped beating),
   it logs a warning that the trail may lack the previous session run's last steps, naming the newest run log under `<session dir>/bro/runs/`, which holds them.
2. It removes the session's pointer (§5.4).
   A pointer it cannot remove ends the launch, since the session run's summons would be attributed to it.
3. It names the session run's log, `<session dir>/bro/runs/<run id>.jsonl`, and creates its directory.
4. Unless `TRAILS_DISABLED` is set (`--no-trails`), it starts `bro.native.trail-recorder --run-log <path> --log-path <session dir>/bro/session-recorder.log --ready-fd <fd>`,
   by its path in the runtime it runs from (`bro.base.spawn.console_script`), detached into a session of its own (`spawn.popen`), with the session's environment and its output appended to that log,
   and waits for the recorder's readiness:
   one byte on the pipe `--ready-fd` names, which the recorder writes once its stop handling is in place and before it touches the store (§5.2),
   so a short run's stop always reaches a recorder that answers it with the final pass, and a store outage does not hold the launch.
   A recorder that cannot be started, or that closes the pipe without the byte because it exited first, ends the launch with the error, naming its log:
   a session that believes it is recording and is not is worse than one that refuses to start.
5. It removes `RIDE_SUMMONER` from its environment, so neither the session run nor anything it starts sees the attribution the recorder took.
6. It runs `bro run|chat … --run-log <path> …` and waits for it, forwarding SIGTERM, as today.
7. It stops the recorder:
   SIGTERM, which the recorder answers with its final pass (§5.2), then a wait of up to 60 seconds, after which it kills the recorder with a warning naming its log.
8. When the session run's log does not exist, the run never opened, and it puts back the pointer step 2 removed (§5.4).

Step 1 reads the health file before step 4's recorder beats it.
Steps 4 and 7 are one context manager around steps 5 and 6, in `harnesses/native/harness/bro/native/recorder.py`,
and steps 2 and 8 another around it, so the pointer is put back only once the recorder has stopped.
The readiness pipe is the handshake a job's supervisor and a watch's producer start with (`bro.jobs.Job`, `bro/watches.py`).
A joined member runs a session runner of its own, so it has its own recorder over its own session records.

### 5.2 Following

The recorder first puts its stop handling in place:
SIGTERM and SIGINT handlers that only mark the final pass due.
It then writes its readiness byte and closes the pipe (§5.1), and only after that loads the `trails` credential and opens the store,
so a stop that arrives after the byte, even before the first pass, is answered with the final pass, and a store that does not answer cannot delay the launch.

The recorder runs a pass after every write to the run log's directory and every 3 seconds without one (`--interval`), until a stop or its parent's exit, and then one final pass.
The interval is the Claude recorder's;
while the log is quiet, it bounds how soon a pass retries a failed write, keeps the trail alive and beats the health file (§5.6).
A pass does, in order, what the entries past the confirmed steps owe the trail (§5.3), one batch at a time:
it reads (§3.5) only the entries that fill the next batch, and reads past them only once the store has confirmed it,
so a backlog the store has not taken stays in the run log, of which the recorder holds at most one batch.
A run log that does not exist yet owes nothing:
its run has not opened, and may never, as when the run's credential gate refuses it.

The passes are methods of one in-process recording class over a store and a run log path, which the console script loops and tests drive directly.

### 5.3 Writing the trail

- **Open.**
  Once the `open` entry is read, the recorder blazes the trail with
  `harness` `bro`;
  `bro`, `version`, `interactive`, `surface`, `hold` and `forked_from` from the `open` entry;
  `native` `{"llm": <the open's recipe>, "ride_command": <the session's>}`;
  `location`, `git` and `summoned_by` from the session facts, `summoned_by` read once when the recorder starts;
  and `body` `{"records": []}`.
  The fresh trail's extent is 0, and a blaze answering any other is refused as a violated assumption.
- **Steps.**
  Every step is appended as its step line's record (§6.1), so step n of the log is step n of the trail.
  A step is *confirmed* once the store has acknowledged the append holding it.
  Steps go in batches bounded as an import chunk is (`bro.trails.store.import_chunks`, 100 records or 8 MiB), a larger step travelling alone,
  and the next batch is read from the run log only once the one before it is confirmed (§5.2).
  A batch carries, as tool blobs, the lists of the `tools` entries its steps name that no confirmed append has carried to this trail.
  A batch whose append fails is kept, and sent again unchanged before anything past it is read,
  so an append whose reply was lost is answered by the store's duplicate check rather than refused as a conflict.
- **A batch kept in part.**
  The hosted store commits an append in transactions of 50 rows, so a failed append can leave it holding the batch's leading steps.
  An append refused as a conflict at an extent inside its batch is answered by sending the batch's leading steps up to that extent again,
  which the store answers as a duplicate when they are the steps it holds, and then the rest of the batch from that extent.
  A conflict at any other extent, or a leading part the store does not answer as a duplicate, ends the recording (§5.7).
  For example, a batch of steps 10 to 109 whose append failed after the store committed steps 10 to 59 is refused, when sent again, as a conflict at extent 60;
  the recorder sends steps 10 to 59, which the store answers as a duplicate, and then steps 60 to 109.
- **End.**
  Once the `end` entry is read and every step is confirmed, the recorder ends the trail with its reason and detail.
  A run log that has no `end` when the final pass reads it leaves its trail unended,
  which the store reads as unreported once the keepalives stop, as it reads any trail whose writer reported no outcome.
- **Keepalive.**
  A pass that writes nothing to an open, unended trail keeps it alive (`Recording.keepalive_if_idle`), so a quiet conversation is not swept as lost while its recorder runs.

The recorder writes through core's write spine (`bro/trails/record/spine.py`), and resolves a conflict through appends alone:
a session's trails token may hold the record permission without the read one.
A line row takes its step line's `at` as its time (§6.1), so the rows keep each step's own time however far the recorder lags;
the header's `started_at`, `end.at` and `last_alive_at` are the recorder's write times.

### 5.4 Trail pointer

The session runner removes the session's pointer before it starts the session run (§5.1), and the recorder publishes the trail's id once the blaze returns.
Nothing removes the pointer when the session ends:
the native resume and the host's `session_exists` read it after the session ends.

- From the session run's start to the blaze there is no pointer, and a summon carries no `summoned_by`.
  So a resumed session's summon never pairs the trail it continues with a step number of the run that continues it:
  that run starts only once the pointer naming the trail is gone.
- After the blaze, the pointer names the session run's trail, a resumed session's fork trail included:
  its summons are attributed to that trail, and the next `ride resume` continues it.
- A session run that exits without its run log never opened, as when its credential gate or its fork refused it.
  The session runner then puts back the pointer it removed, once the recorder has stopped, so a refused resume can be tried again;
  without a run log the recorder never blazed, so nothing publishes over it.
- A session that ends with no pointer, because it ran with `--no-trails` or because its run opened and its recorder never blazed, is refused by `ride resume`, which names both causes (`BroHarness.missing_session_error`).

### 5.5 Trail mark

Once the trail opens, the recorder emits the trail mark once through `RunLifecycle.from_env()`,
when the session has a broker channel and its run is one-shot (the open's `interactive` is false) or the session is summoned (`bro.summon.summoned()`)
— the runs that announce their trail today.
It sends the mark whatever the run's state:
the host refuses a mark for a mission that already ended, and `Client.mark` waits for no reply.
A mark that cannot be sent, as when the broker channel does not connect, is logged and not sent again, and leaves the pass's outcome alone.

### 5.6 Recording health

Every pass beats the recording health file with its outcome, `ok` or `error` with a one-line summary, the pass interval, and the recorder's log as diagnostics;
the final pass's beat promises no further beat.
The banner surfaces it as it does the Claude recorder's.
Judging recording health from the store alone, on the host, is #1288's.

### 5.7 Failures

- A store error fails the pass:
  its beat says `error`, nothing confirmed is undone, and the next pass re-attempts what is owed (§5.3).
- A refusal no later attempt can change ends the recording:
  `InvalidRequest`, `PermissionDenied` or `TrailNotFound` from the store, a conflict §5.3 cannot answer, or a run log that breaks §3.3.
  The recorder then writes nothing more to the trail, and beats `error` naming the refusal, or the file and the line, until it stops.
- A selected `trails` credential that does not load ends the recorder at its start, after an `error` beat (`config not found: trails`), as the Claude recorder does;
  its readiness byte is already written, so the session runs on unrecorded, its banner reading `FAILING`.
- What the final pass cannot write stays unrecorded, and the trail stays unended;
  catching it up later is #1189's.

### 5.8 Example

A summoned one-shot child whose store fails mid-run:

1. The session runner removes the pointer, names `runs/01m7e….jsonl`, starts the recorder on it, waits for its readiness byte, and spawns `bro run` with `--run-log` naming it.
2. The run writes its `open`, step 0 and step 1, its prompt.
   The recorder's pass blazes trail `T` at extent 0, publishes `T`, emits the trail mark, and appends steps 0 and 1.
3. Step 2 is an `llm_call` whose response calls `summon`;
   the run writes the `tools` entry for its tool list just before it, and the recorder appends the step with that list as a blob.
   `current_tool_step_id` is `{step_id: 2, index: 0}`, and the host, reading `T` off the pointer, stamps the child with `summoned_by` `{T, 2, 0}`.
4. The store goes down.
   The run goes on writing steps 3 to 6;
   the first failing pass tried steps 3 and 4, and every pass after it resends that batch, reading nothing past it and beating `error`.
5. The store comes back:
   the batch is confirmed, and steps 5 and 6 go in the next one.
6. The run writes `end` `ok`, sends its result and exits.
   The session runner stops the recorder, whose final pass ends `T` as `ok`.

## 6. Native trail rows

### 6.1 Line rows

The recorder appends each step as the record `{"run_log": <the open entry's format>, "body": <its step line>}`, which the bro trail format stores as a line row:

- `kind` — the line's `kind`;
- `ts` — the line's `at`;
- `body` — the line, verbatim;
- `run_log` — the run log format the line is read under;
- `tools_sha256` — on an `llm_call`, the line's `tools_sha256`, which the store's tool-blob bookkeeping reads off rows (`bro.trails.model.named_tool_digests`);
- the store's own fields, as on every row:
  `trail_id`, `format`, `step_id`, `payload_sha256`, and `usage` where the step bills one.

The format refuses a record it cannot read as a line row:
a `run_log` it does not know, a `body` that is not one JSON object, a line whose `kind` is not a step kind or whose `at` is not a string,
a text-bodied step (`system_prompt`, `user_input` or `notification`) whose line `body` is not a string, and an `llm_call` without a `tools_sha256` digest.
A stored line row carries `run_log` and its line in `body` as the record did, so the format reads it back the same way when a recompute or an import's seal folds the rows again.

The tool call of §3.2's example, step 3:

```text
step line  {"kind":"tool_result","at":"2026-11-14T21:00:03.000000Z","body":"…","turn_index":0,"call_index":1,"tool_name":"bro__banner","call_id":"c1","is_error":false}
record     {"run_log":1,"body":"{\"kind\":\"tool_result\",\"at\":\"2026-11-14T21:00:03.000000Z\",…}"}
row        {"trail_id":"T","format":1,"step_id":3,"ts":"2026-11-14T21:00:03.000000Z","kind":"tool_result","payload_sha256":"…","body":"{\"kind\":\"tool_result\",…}","run_log":1}
```

### 6.2 Spread rows

Every bro row written before this change is a spread row:
the record's `kind`, `body` and other fields beside the store's own, its `ts` the record's or else the store's clock, and no `run_log`.
The format keeps reading and accepting them as it does today, so the trails stored before the change, and the sessions still running an older runtime (§9), keep their shape.

### 6.3 Reading rows

A row's *native record* is the step it holds, in the shape the harness's readers take:
for a line row, the row's `trail_id`, `format`, `step_id`, `ts`, `payload_sha256` and `usage` with every field of its line but `at`, which `ts` carries;
for a spread row, the row itself.

- Classification and projection read a row's native record, so a line row folds and projects as the spread row of the same step does.
- `TrailFormat` gains `native_record(row)`, the identity for a format that defines none, and `bro.trails.store.fetch_recorded_trail` builds each step from its row's native record,
  so fork, resume and the wires' `restore` read line rows as they read spread rows.
- `rewind steps`, transfers and imports read rows as stored, so they show and carry the line verbatim.

## 7. Long results

A mission's result reaches its summoner in one broker message, whose result text the answer bound limits.

- The `answer` tool (`bro/bro.py`), which both harnesses serve, refuses an answer longer than the bound before it ends anything, with `answer too large: <n> bytes, over the <bound>-byte answer bound; shorten it to fit`,
  so the run can call it again with a shorter answer.
  Its description states the bound and that a longer answer is refused.
  Both texts render `MAX_ANSWER_BYTES`.
- `RunLifecycle.completed(result, end_reason)`, the one way a run's result leaves it, cuts any other result longer than the bound
  — a one-shot run's last reply or failure detail, a `raise` reason, the Claude runner's last turn —
  to its head at a UTF-8 boundary, followed by the marker `[answer truncated; full response in trail <trail id>]`,
  the trail being the one the session's pointer names when the result is sent, or `unavailable` without one.
  The broker's result payload does not change.
- `completed`'s `trail_id` parameter goes:
  the native run announces no trail of its own (§5.5), and the Claude runner's `_complete_run` stops reading the pointer to pass one.

For example, a summoned child whose findings run to 80 KiB calls `answer` with them and is refused, `answer too large: 81920 bytes, over the 65536-byte answer bound; shorten it to fit`;
it condenses them to 50 KiB and answers again, and its summoner receives all 50 KiB.
A one-shot child whose last reply is 80 KiB sends a result of 65,536 bytes:
the reply's head, then `[answer truncated; full response in trail T]`.

## 8. What changes for users

| Situation | Today | After |
|---|---|---|
| a trail-store outage during a native session | the next turn fails | the run continues, the banner shows `session_recording: FAILING`, and the trail catches up once the store is back while the recorder runs |
| `bro run`, `bro chat` and `bro chat --fork` on the host | record a trail | write a run log under `~/.local/share/ride/bro/runs/` and record no trail |
| `ask` and `call`, which are `ride solo` and `ride along` | record a trail | record a trail |
| a native trail's rows | each record's fields beside the store's own | each step's run log line, verbatim in `body`, which `rewind steps` prints; trails recorded before keep their rows |
| `bro chat`'s exit line | `conversation recorded as trail T; fork it with: …` | `conversation saved to <path>` |
| `bro chat <bro> --fork` with no id | the bro's newest `call` trail, host conversations included | the bro's newest `call` trail, which only managed sessions record now |
| a resumed native session | the pointer keeps naming the original trail: summons are attributed to it, and the next resume forks it again | no pointer from the run's start, then the resumed trail once it opens, or the original again if the run never opens |
| `ride resume` after a session whose recording failed or stopped | cannot arise: a failed write fails the turn | the resume warns that the trail may lack the session's last steps, naming the run log that holds them |
| a native run nested in a session of either harness | records a trail and repoints the session's pointer at it | keeps its run log under the runtime root and records no trail |
| a summon before the native session's trail opens | cannot happen: the trail opens before the first model call | the child carries no `summoned_by`, like one summoned before a Claude session's recorder adopts its transcript |
| an answer longer than 64 KiB | the `answer` tool refuses it, advising an artifact | the `answer` tool refuses it, advising a shorter answer, and its description states the bound |
| any other result longer than 64 KiB | cut with a marker naming the trail the run announced | cut with a marker naming the trail the session's pointer names |
| `--no-trails` | no trail | no trail and no recorder; the run log is still written |

## 9. Rollout and mixed versions

The rows a native trail stores are read across deployment boundaries, by code that may predate line rows:

- the trails-server parses every record it is sent, and folds and projects the rows it stores, through the bro trail format its image installs (`oops/trails/server/entries.json`);
- a runtime reads a trail's stored rows back to fork or continue it (`fetch_recorded_trail`, §6.3).
  Every root runs the runtime it froze at its start, its descendants the same one, and `ride resume` runs the one its workspace recorded
  (`bro/reference/ride.md`, "Runtime bundles" and "Lifecycle verbs"), so a workspace resumes on the runtime that recorded its trail.

Code that predates §6 reads spread rows only.
Such a server refuses a line row's record as malformed, since the record carries no step `kind`;
it projects a stored line row's `llm_call` as a harness event, since the row's body is the line rather than the exchange, and a recompute through it drops that call's usage from the trail's totals.
Such a runtime cannot fork or continue a trail holding line rows:
its fork point refuses the first such `llm_call`, `llm_call step … has no response object` (`bro.fork.latest_fork_point`).
Nothing else crosses a deployment boundary:
the run log is written and read within one session's runtime, by the run and its recorder,
and the trail pointer, the recording health file, the trail mark and the result message keep their formats, so a host and sessions on different runtimes work together as today.

The change therefore lands on the default branch in two landings, readers before writers:

1. **Line-row readers** — the bro trail format reads line rows (§6), with `TrailFormat.native_record` and `fetch_recorded_trail`, while nothing writes one yet.
   Its rollout redeploys the trails-server and re-syncs the host installation, in either order.
2. **Run log recording** — the run log, the recorder and the switch to them (§3 to §5), and the long results (§7).
   It leaves the bro trail format and the store as landing 1 left them, so its rollout is a re-sync of the host installation alone, made only once both of landing 1's steps are done:
   every root started after it records through its recorder, in line rows,
   while a resume runs the runtime its workspace recorded, and a root already running keeps its own, its children the same one,
   so each of those records spread rows in-process if its runtime predates landing 2.

| Rows | Written by runtimes | Accepted and projected by servers | Forked and continued by runtimes |
|---|---|---|---|
| spread | before landing 2 | any | any |
| line | from landing 2 | from landing 1 | from landing 1 |

A root on a runtime from before landing 1
— one started before landing 1's re-sync, or the resume of a workspace that was —
reads line rows as code that predates §6 does:
a fork or continuation of a line-row trail inside it fails as above.

Rollback:

- Landing 2 rolls back by re-syncing the installation to a commit before it.
  Roots started after that record spread rows in-process again, but for the resumes of workspaces landing 2 started, which keep its recorder,
  and the line-row trails recorded meanwhile stay readable, since the server and the installation stay at landing 1 or later.
- Landing 1 rolls back only while no line row is stored, that is, before landing 2's rollout.
  Once one is, neither the server nor the installation goes back past landing 1, and problems are fixed forward:
  a server from before it would refuse every line-row append, mis-project the stored line rows and drop their usage at a recompute,
  and an installation from before it would start roots that cannot fork or continue them.

## 10. Known approximations

- Summons between the session run's start and its blaze carry no `summoned_by`, and a run that ends before its blaze sends no trail mark (§5.4, §5.5).
  While the store is up, both windows end one pass and one store round trip after the open;
  during an outage they last the outage.
  #1288's host, which records every trail, is to write the summon link itself once both trails exist.
- The header's times are the recorder's write times (§5.3).
- A native child's trail may still be catching up when its summoner gets the result:
  the recorder's final pass runs after the run has sent its result and exited.
- A final pass over a long backlog can outlast the 60 seconds the stop waits (§5.1), which leaves the rest unrecorded and the trail unended.
- A blaze whose reply is lost leaves an empty trail the store later reads as unreported, and the next pass blazes another, which is the session's (#1189).
- A recorder that dies mid-session stops the recording:
  its beat goes `STOPPED`, and the rest of its run log is not replayed (#1189).
- A step larger than trails-server's request cap (64 MiB) can never be appended, so its run's recording fails on it from then on;
  today such a step fails the run's turn.
- A resume after a recording that failed or stopped continues the trail as stored:
  the steps it could not record are missing from the conversation the resume continues, which the resume warns about (§5.1);
  continuing from the run log is #1303's.
- A session whose run opens but whose trail never does ends with no pointer, so the next `ride resume` is refused;
  its run log holds the conversation, which #1303's continuation from run logs can reach.
- Run logs under the runtime root accumulate, and a nested run's log in a box goes with the container.
- Inside a box, `bro chat`'s exit line names the path the box sees, `/var/ride/session/bro/runs/<id>.jsonl`,
  which on the host is `<workspace>/session/bro/runs/<id>.jsonl`.
- A cut result's marker names the trail the pointer names when the result is sent,
  which may not hold the run's last steps yet, since the recorder follows the run, and is `unavailable` before the recorder's blaze.

## 11. Implementation mapping

Landing 1 (§9) is the change in `bro-native-trails`, core's `TrailFormat.native_record` and `fetch_recorded_trail`, the image check's line-row case, and the docs that describe them;
landing 2 is everything else below.

Added in `bro-native` (`harnesses/native/harness/`):

- `bro/native/run_log.py` — the location (§3.1), the run log tracker (§3.4) and the reader (§3.5).
- `bro/native/trail_recorder.py` — the session recorder:
  the in-process recording class whose passes write the trail (§5.2 to §5.7), and the `bro.native.trail-recorder` console script that loops them (`sync-scripts --project harnesses/native/harness`).
- `bro/native/recorder.py` — the recorder's lifetime the session runner enters, its readiness wait included, and the pointer's removal and restoration around it (§5.1).
- The `watchfiles` dependency, for the recorder's wake on writes, as `bro-claude` has it.

Changed in `bro-native`, under `harnesses/native/harness/`:

- `bro/native/runner.py` — §4: the run log default, `run_log`, no pointer, no mark, no `summoned_by`, the error detail.
- `bro/fork.py` — `run_log` passed to the runner.
- `bro/launch/run.py`, `bro/launch/call.py` and `bro/launch/resume.py` — `--run-log`, the exit line, the `--fork` help and the log lines (§4.4).
- `bro/native/harness.py` — `run_session` per §5.1 and `missing_session_error` per §5.4.
- `bro/native/distribution_test.py` — the wheel's contents, which no longer hold a `bro.trails` portion.

Changed in `bro-native-trails`:
`bro/native_trails/format.py` reads line rows (§6.1) beside spread rows (§6.2), and defines `native_record` (§6.3).

Changed in core:

- `bro/llm/tracker.py` — the contract of §4.1.
- `bro/bro.py` — `LiveRun.trail_id` and the banner tool's pass-through of it go;
  the `answer` tool's refusal and description follow §7.
- `bro/workspace/banner.py` — `render_banner`'s `trail_id` and `SessionFacts.collect`'s `trail_id_override` go.
- `bro/run_lifecycle.py` — §7:
  the refusal text, and `completed` naming the trail the session's pointer names, its `trail_id` parameter gone.
- `bro/trails/backends.py` — the `TrailFormat.native_record` member (§6.3).
- `bro/trails/store.py` — `fetch_recorded_trail` builds steps from native records (§6.3).

Changed in `bro-claude`:
`bro/claude/runner.py`'s `_complete_run` sends its result without reading the pointer.

Changed in `bro-oops`:
`oops/trails/server/image_cases.json` gains a native case recorded in line rows, which `verify_image.sh` records and projects.

Changed at the root and in `local/`:
`conftest.py` loses the `NullTracker` pin and its docstring paragraph,
and `local/bro/local/run_tests.py`'s test roster gains the new test modules and loses the deleted one.

Deleted:
`harnesses/native/harness/bro/trails/record/bro.py` and its test, and with them the member's portion of the `bro.trails` namespace tree.
The tests that record trails in-process through that recorder to fork, resume or read them record through §5.2's recording class instead,
and core's `bro/trails/local_test.py` writes its trail through the spine.

Documentation:

- `harnesses/native/harness/AGENTS.md` — "Components", "Recording" and the namespace-tree paragraph.
- `harnesses/native/harness/bro/native/reference.md` — "Recording and usage", "Resume" and "In-process surfaces".
- `harnesses/native/trails/AGENTS.md` — the format's line and spread rows.
- `bro/trails/AGENTS.md` — `TRAILS_DISABLED`, which turns a managed session's recorder off on either harness and is no longer the recovery path for a broken trails-server;
  "Recorder placement";
  "Surfaces";
  and `native_record` among the format's members.
- `bro/llm/AGENTS.md` — `tracker.py`.
- `bro/AGENTS.md` — `run_lifecycle.py`.
- `bro/launch/AGENTS.md` — `run.py`'s recording sentence and the exit hint.
- `bro/reference/ride.md` — "Session recording", now true of both harnesses' recorders.
- `bro/prompts/environment.md` — item 9's "the transcript isn't reaching trails".
- `README.md` — "every run on every harness is a recorded trail" becomes every session.

## 12. Testing

- The run log tracker and reader:
  step numbers, a tool list carried once in a `tools` entry and named by every `llm_call` that offers it, and `at` on every entry;
  exclusive creation, refusal after a failed write and after the end, an incomplete last line left unread, each §3.3 violation refused, and each entry returned as its line too.
- The runner:
  a run with no reachable store completes, consulting none;
  `current_tool_step_id` equals the run log's step number;
  a run without `run_log` logs under the runtime root, and one given `run_log` logs there.
- The recording class over a real `LocalStore`:
  the trail's rows are line rows holding the log's step lines verbatim, each with its `at` as `ts`, and each tool list sent once;
  the pointer published at the blaze and kept at the stop;
  the mark for a one-shot run and for a summoned session, and none for an interactive root;
  the health beats;
  a backlog longer than a batch read one batch at a time;
  a store failing then recovering, with the failed batch resent unchanged and no entry past it read until it is confirmed;
  a store that keeps a failed batch's leading steps, answered by sending them again and then the rest;
  and each refusal of §5.7 ending the recording.
- The daemon:
  its readiness byte written before it opens the store, so a store that never answers does not delay it;
  a SIGTERM right after the byte answered with a final pass that records the whole log and its end;
  and the parent's exit.
- The session runner:
  the run started only after the recorder's readiness byte, and a recorder that exits without writing it ending the launch;
  the recorder stopped after the run, over the run's log, and none under `TRAILS_DISABLED`;
  `RIDE_SUMMONER` absent from the run's environment;
  the pointer read before the run when resuming, removed before the run starts, put back when the run never created its log, and left as the recorder published it when it did;
  and the resume warning when the health file reports a problem, and none when it does not.
- Long results:
  the `answer` tool refusing an answer one byte past the bound and taking one at it, and its description stating the bound;
  any other result past the bound cut to fit, with the marker naming the pointer's trail, or `unavailable` without one.
- The bro trail format:
  a line row parsed, classified and projected as the spread row of the same step is;
  each refusal of §6.1;
  `native_record` of both shapes;
  a stored line row folded again by a recompute;
  and spread rows read as today.
- Routes, end to end in-process over a `LocalStore`:
  a trail of line rows, appended as §6.1's records, that `bro.launch.resume.resume` continues, which landing 1 completes;
  a run's log recorded into a trail that `bro.launch.resume.resume` continues, the fork's own log recorded as the trail forked from it;
  and a summon's `current_tool_step_id` naming the row whose `llm_call` holds the summon call at that index.
- The trails image check (`oops/trails/server/verify_image.sh`) recording and projecting line rows.
- A test waiting on the recorder waits on a signal that establishes the state it needs
  — its readiness byte, a health beat with a given outcome, the pointer's publication, the process's exit —
  never on a sleep.

## 13. Follow-ups proposed, not part of this design

- The Claude runner emits its session's trail mark from a pointer watch (`harnesses/claude/harness/bro/claude/runner.py`);
  its recorder could emit it as the native one does, so #1288 moves one owner of the mark.
- The Claude recorder's lifetime waits for no readiness, so a stop that comes before its handlers are installed ends it without a final pass (§15);
  §5.1's handshake applies to it too, and one shared helper could then hold both recorders' lifetimes (`recorder.py` in each harness).
- The Claude recorder sends its whole grown tail again after a failed append,
  so a lost reply, or a batch the hosted store kept in part, conflicts on every later pass over its segment;
  §5.3's recovery applies to it too.

## 14. Decision record

Each decision below was settled in the design discussion or its review;
the section that applies it is in parentheses.

- The run log is the harness's own format, with flat entries, `at` on every entry, a `tools` entry per tool list and `recipe` in `open` (§3.2).
- A. The recorder records the session run only; a nested run keeps its run log and records no trail (§2, §3.1, §5).
- B. The run log carries what the run knows; the recorder adds the session facts (§3.2, §5.3).
- C. The pointer names the session run's trail whatever its origin, a resumed session's included;
  the session runner removes it before the session run starts, and puts it back when the run never opens (§5.4).
- D. A summon before the trail opens carries no `summoned_by`, and a late trail mark is lost; the host is to write the summon link itself under #1288 (§5.5, §10).
- E. `bro chat` ends by naming its run log, whoever named it (§4.4).
- F. A run log's default directory is under the runtime root wherever the run runs, and a launcher names another place (§3.1).
- G. Every entry carries its `at` (§3.2).
- H. The tracker factory, the runner's `TRAILS_DISABLED` check, the suite's `NullTracker` pin and the run's `trail_id` go; the error end's detail is `<type>: <message>`; a failed batch is resent unchanged (§3.4, §4.1, §5.3).
- I. Each `llm_call` keeps its `wire`, ready for switching models mid-run, and its model stays in its body, with no field of its own (§3.2).
- J. A native trail row holds its step line verbatim, as a Claude row holds its transcript line, and the rows stored before stay readable as spread rows (§6).
- K. A batch the store kept in part is completed by sending its kept part again, which the store answers as a duplicate, and then the rest (§5.3).
- L. Long results are cut, never minted:
  the `answer` tool refuses an answer past the bound while the run can shorten it, its description states the bound, and any other result is cut with a marker naming the pointer's trail;
  the bound's value is #1307's (§7).
- M. A resume warns when the session's recording failed or stopped (§5.1).
- N. The session run starts only once the recorder has said, through a readiness pipe, that its stop handling is in place, which it does before it touches the store (§5.1, §5.2).
- O. A pass reads the run log one batch at a time, so a backlog stays on disk (§5.2, §5.3).
- P. Readers land before writers:
  line-row reading is a landing of its own, rolled out before the landing that writes line rows, and once a line row is stored nothing rolls back past it (§9).
- The recording health file stays, beaten by the recorder as by Claude's, and #1288 moves recording liveness to the store (§5.6).

## 15. Assumptions and evidence

- The bro trail format accepts a blaze with an empty `records` list (`_bro_open` in `harnesses/native/trails/bro/native_trails/format.py`).
- The bro trail format spreads a record's fields into its row beside the store's own, leaving out a field named like one of them, and takes a record's `ts` as the row's time (`_bro_parse`);
  the Claude trail format keeps the transcript line in `body` and copies `kind`, `ts` and a few ids out of it (`_claude_parse` in `harnesses/claude/trails/bro/claude_trails/format.py`);
  a stored row's `ts` is its parse's time, or else the store's clock (`build_rows` in `bro/trails/rows.py`).
- A recompute, a check and an import's seal parse the stored rows again through the format (`replay` in `bro/trails/rows.py`);
  `fetch_recorded_trail` builds steps through the format-neutral `step_from_row` (`bro/trails/store.py`);
  transfers carry rows as stored, with the tool blobs the rows name in `tools_sha256` (`named_tool_digests` in `bro/trails/model.py`).
- Both backends answer an append sent again unchanged as a duplicate (`LocalStore.append_records`, `DynamoStore.append_records`), and neither requires an append to carry a blob it already holds;
  only an import does.
- `DynamoStore.append_records` commits an append in transactions of at most 50 rows (`MAX_TRANSACTION_RECORDS` in `bro/trails/server/dynamo_types.py`), so a failure after the first leaves the store holding the batch's leading rows;
  an append conflict reports the store's extent (`AppendConflict.actual`), over the network too (`bro/trails/network.py`).
- A session's trails token may hold the record permission alone (`bro/trails/AGENTS.md`, "Service auth").
- `NetworkStore` retries an append but never a blaze (`bro/trails/network.py`).
- `Client.mark` waits for no reply, and the dispatcher refuses a mark for a mission it no longer holds live (`bro/broker/dispatcher.py`).
- The host reads the summoning session's pointer at each summon (`PeerFacts.attribution_for_mission` in `ride/ride/peer_facts.py`), and a summon request carries only the tool call's position (`_summoned_by` in `ride/ride/bro_worker.py`);
  a blaze checks only the shape of `summoned_by` (`bro/trails/model.py`), so a child may name a step its summoner's trail does not hold yet.
- `spawn.popen` detaches its child into a session of its own, so a terminal's interrupt does not reach the recorder.
- The suite's environment rebuild clears `RIDE_*`, so a test's run is outside any session (`bro/base/suite_environment.py`), and the root `conftest.py` points `XDG_DATA_HOME` at a per-test directory;
  boxes set no `XDG_DATA_HOME`, so a boxed run's runtime root is the container's own (`bro.workspace.paths.runtime_base`).
- Run ids sort by the time they were minted (`bro/base/lulid.py`).
- Today a resumed native session republishes no pointer, since `bro.fork.fork` opens its trail without publishing one, and a nested run publishes its own from `Runner._start`.
- Every result a run sends goes through `RunLifecycle.completed`:
  `Runner.run()`, `bro/launch/call.py`, the Claude runner's `_complete_run`, and `_complete_and_terminate` and `_raise_and_terminate` behind the Claude harness's `answer` and `raise` tools.
- Today's marker names the trail the run announced or its caller passed, and says `unavailable` without one (`truncate_answer` in `bro/run_lifecycle.py`);
  the Claude runner passes the trail its session's pointer names (`_complete_run`).
- `bro.monitor.health.problem()` reports `FAILING` after an `error` beat and `STOPPED` after a missed one, and nothing after a final `ok` beat (`bro/monitor/health.py`).
- `ride`'s launcher prints the `ride resume <workspace>` hint after a session whose harness reports it resumable (`_print_resume_hint` in `ride/ride/session.py`).
- The trails-server image parses rows through the formats `oops/trails/server/entries.json` declares, and CI runs on every pull request, the image check among its stages (`.github/workflows/tests.yml`);
  the image check's cases are fixed blazes and records (`oops/trails/server/image_cases.json`), so a line-row case needs no writer.
- Today's bro trail format refuses a record without a step `kind` (`_bro_parse`),
  finds usage only in an `llm_call` whose body is an object (`_bro_classify`), and projects any other `llm_call` as a harness event (`_bro_llm_call_messages`);
  `step_from_row` keeps a row's body as stored, and `latest_fork_point` refuses an `llm_call` whose body holds no response object (`bro/trails/store.py`, `bro/fork.py`).
- `ride resume` re-executes the runtime its workspace's record names before it reads the rest of the record (`ride/ride/cli.py`, `recorded_runtime_reference` in `ride/ride/session.py`).
- The Claude recorder installs its SIGTERM handler only in `_watch`, after it has built its store and recorder (`harnesses/claude/harness/bro/claude/trail_recorder.py`),
  and its lifetime waits for no readiness, so a stop that comes first ends it without a final pass.
- A job's supervisor and a watch's producer write one byte to an inherited pipe once ready, and their starters read a pipe that closes without it as a failed start (`bro.jobs.Job`, `bro/job_supervisor.py`, `bro/watches.py`).
- `trail_pointer.clear` ignores a failure to remove the pointer (`bro/monitor/trail_pointer.py`), so the session runner's removal, which must not fail silently, goes through a call that raises.
