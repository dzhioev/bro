# Design: native runs record through a session recorder over run logs

This file is the specification of [#1302](https://github.com/dzhioev/bro/issues/1302).
It lives on the branch `design/1302-native-run-logs`, which is never merged:
the pull request that carries it is a design review only.

## 1. Goal, scope and constraints

Goal: the native harness records the way the Claude harness does.
A native run writes its own local record, the *run log* (§3), in a format the harness owns, and never writes to a trail store.
In a managed session, a *session recorder* (§5), started and stopped around the session's run the way the Claude runner starts and stops its recorder, turns that log into the session's trail:
native trails store the log's steps as their rows, and the bro trail format reads them, as the Claude trail format reads Claude Code's transcript lines.
Trails become purely a ride extension:
a native run outside a managed session
— `bro run` or `bro chat` on the host, a test's in-process `Runner` —
records no trail and only saves its run log, as Claude Code always saves its transcript.

The change also delivers a run's answer whole, whatever its size:
an answer longer than the broker's answer bound is minted as an artifact (§6), and the cut text its result carries names that artifact, where today it names a trail.

What it enables, for example:

- A trails-server outage no longer fails a native turn:
  the run carries on, its run log keeps growing, and the session's recording health reads `FAILING` until the recorder's writes go through again.
- `bro run dev "…"` on the host contacts no trail store;
  its whole conversation is one file under the runtime root.
- Both harnesses record in one shape
  — the harness writes a local record in its own format, and a recorder beside the session writes the trail from it —
  which is the shape [#1288](https://github.com/dzhioev/bro/issues/1288) moves to the host.
- Changing trails, their header, store or projection, never changes the run log;
  changing the run log is a format version the bro trail format learns to read.
- A summoned run that answers with 200 KiB of findings delivers all of it:
  its result carries the first 64 KiB with a marker naming an artifact that holds the whole, which the summoner fetches.

Out of scope, each with where it belongs:

- retrying and replaying writes beyond what one recorder lifetime re-attempts (§5.7):
  deduplicating a blaze whose reply was lost, catching up a run log whose recorder stopped first, a retry policy ([#1189](https://github.com/dzhioev/bro/issues/1189));
- how a resumed or forked conversation continues:
  `bro chat --continue-trail` and `bro chat --fork` keep reading trails back from the store, and what replaces `--fork` on the host stays open ([#1303](https://github.com/dzhioev/bro/issues/1303));
- recording from the host, taking the `trails` credential out of sessions, a session's trail-reading tool, recording liveness judged from the store alone, and the summon link written by the host (#1288);
- the broker's limits, the answer bound among them ([#1307](https://github.com/dzhioev/bro/issues/1307));
- keeping the model off the session's broker channel except through its tools ([#1308](https://github.com/dzhioev/bro/issues/1308));
- the Claude harness's recorder, which stays as it is (§11 proposes two follow-ups);
  only the Claude harness's result path changes, through §6.

Constraints:

- core imports no other workspace member;
  the run log is the bro harness's own artifact, so its format, writer and reader live in `bro-native`,
  and the bro trail format, in `bro-native-trails`, reads a log's steps without importing `bro-native`;
- the run never writes to a trail store, reading one back only to fork or continue as today, and the recorder never writes the run log;
- backward compatibility is not a constraint (`AGENTS.md`, "Conventions");
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
- **Tool list** — the tools an `llm_call`'s request offered, which the run log carries once, in a `tools` entry, and names by its digest.
- **Session run** — the run a managed session's runner launches:
  the `bro run` or `bro chat` process `BroHarness.run_session` spawns.
  Any other native run inside a managed session, such as a `bro run` a job starts, is a *nested run*;
  nested runs serve tests and development.
- **Session recorder** — a daemon beside a managed session that writes the session's trail from the harness's own local record.
  This design's is `bro.native.trail-recorder`, which records the session run's log (§5);
  the Claude harness's is `bro.claude.trail-recorder`.
  It is what #1288 and #1303 call the trails writer, and below "the recorder" unqualified is the native one.
- **Session facts** — what a trail header says about the session rather than the run:
  `location`, `git` and `native.ride_command`, read off the session environment by `bro.trails.record.session.managed_session`, and `summoned_by`, read by `bro.summon.summoned_by_from_env`.
- **Answer bound** — `MAX_ANSWER_BYTES` (64 KiB, `bro/run_lifecycle.py`), the longest result text a mission's result message carries;
  a longer answer is cut to fit, and an **answer artifact** holds it whole (§6).
- **Trail pointer** (`bro/monitor/trail_pointer.py`), **recording health file** (`bro/monitor/health.py`) and **trail mark** (`bro.run_lifecycle.RunLifecycle.trail`) keep their meanings;
  §5 says who writes them.

## 3. The run log

### 3.1 Location

A run log is the file `<directory>/<run id>.jsonl`.
Its default directory is `<runtime root>/bro/runs/`, the runtime root being `bro.workspace.paths.runtime_base()`, wherever the run runs:
the run never looks at whether it is in a managed session.
A launcher that wants the log elsewhere names the file (§4.1), as the session runner does for the session run (§5.1).
The run id is a fresh `bro.base.lulid.lulid()`.
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
{"kind":"tool_result","at":"…","body":"…","turn_index":0,"call_index":1,"tool_name":"bro__banner","call_id":"c1","is_error":false}
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
Each entry opens the file for appending, writes its whole line in one `write()` and closes it, so the tracker holds no descriptor between entries;
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

The reader takes a run log's complete lines past a byte offset, checks them against §3.3 given what it read before, and returns the entries and the offset after them.
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

- `Runner.run()` still sends the run's result over its broker channel, through `RunLifecycle.completed`, which mints an answer longer than the answer bound (§6).
- Neither `run()` nor `send()` sends the trail mark:
  the session recorder does (§5.5).
- `summoned_by` is a session fact the recorder stamps (§5.3);
  the runner no longer reads it.

### 4.4 Surfaces

- `bro chat` ends by naming its run log, `conversation saved to <path>`, whether it created the log or a launcher named it with `--run-log`;
  in a managed session, `ride`'s `ride resume <workspace>` hint follows it.
  The `bro chat <bro> --fork <trail>` hint goes.
- `bro run` and `bro chat` log the run log's path where they logged the trail id.

## 5. The session recorder

### 5.1 Lifetime

`BroHarness.run_session` runs the session run inside the recorder's lifetime:

1. When resuming, it reads the trail to continue off the session's pointer, as today.
2. It names the session run's log, `<session dir>/bro/runs/<run id>.jsonl`, and creates its directory.
3. Unless `TRAILS_DISABLED` is set (`--no-trails`), it starts `bro.native.trail-recorder --run-log <path> --log-path <session dir>/bro/session-recorder.log`,
   by its path in the runtime it runs from (`bro.base.spawn.console_script`), detached into a session of its own (`spawn.popen`), with the session's environment and its output appended to that log.
   A recorder that cannot be started ends the launch with the error, as the Claude runner's does:
   a session that believes it is recording and is not is worse than one that refuses to start.
4. It removes `RIDE_SUMMONER` from its environment, so neither the session run nor anything it starts sees the attribution the recorder took.
5. It runs `bro run|chat … --run-log <path> …` and waits for it, forwarding SIGTERM, as today.
6. It stops the recorder:
   SIGTERM, which the recorder answers with its final pass (§5.2), then a wait of up to 60 seconds, after which it kills the recorder with a warning naming its log.

Steps 3 and 6 are one context manager around step 5, in `harnesses/native/harness/bro/native/recorder.py`.
A joined member runs a session runner of its own, so it has its own recorder over its own session records.

### 5.2 Following

The recorder runs a pass after every write to the run log's directory and every 3 seconds without one (`--interval`), until SIGTERM, SIGINT or its parent's exit, and then one final pass.
The interval is the Claude recorder's;
while the log is quiet, it bounds how soon a pass retries a failed write, keeps the trail alive and beats the health file (§5.6).
A pass reads the complete entries past what it has read (§3.5) and does, in order, what they owe the trail (§5.3).
A run log that does not exist yet owes nothing:
its run has not opened, and may never, as when the run's credential gate refuses it.

### 5.3 Writing the trail

- **Open.**
  Once the `open` entry is read, the recorder clears the trail pointer (§5.4) and blazes the trail with
  `harness` `bro`;
  `bro`, `version`, `interactive`, `surface`, `hold` and `forked_from` from the `open` entry;
  `native` `{"llm": <the open's recipe>, "ride_command": <the session's>}`;
  `location`, `git` and `summoned_by` from the session facts, `summoned_by` read once when the recorder starts;
  and `body` `{"records": []}`.
  The fresh trail's extent is 0, and a blaze answering any other is refused as a violated assumption.
- **Steps.**
  Every step is appended as the log holds it, so step n of the log is step n of the trail, and the trail's row is the log's entry.
  A step is *confirmed* once the store has acknowledged the append holding it.
  The steps read and not yet confirmed go in batches bounded as an import chunk is (`bro.trails.store.import_chunks`, 100 records or 8 MiB), a larger step travelling alone.
  A batch carries, as tool blobs, the lists of the `tools` entries its steps name that the recorder has not yet sent to this trail.
  A batch whose append fails is sent again unchanged before any later step, so an append whose reply was lost is answered by the store's duplicate check rather than refused as a conflict.
- **End.**
  Once the `end` entry is read and every step is confirmed, the recorder ends the trail with its reason and detail.
  A run log that has no `end` when the final pass reads it leaves its trail unended,
  which the store reads as unreported once the keepalives stop, as it reads any trail whose writer reported no outcome.
- **Keepalive.**
  A pass that writes nothing to an open, unended trail keeps it alive (`Recording.keepalive_if_idle`), so a quiet conversation is not swept as lost while its recorder runs.

The recorder writes through core's write spine (`bro/trails/record/spine.py`).
The bro trail format takes a step's `at` as its row's timestamp, so the rows keep each step's own time however far the recorder lags;
the header's `started_at`, `end.at` and `last_alive_at` are the recorder's write times.
A native trail written before this change has the same rows without `at`, and reads as it does today.

### 5.4 Trail pointer

The recorder clears the session's pointer when it reads the session run's `open` entry, and publishes the trail's id once the blaze returns.
It leaves the pointer in place when it stops:
the native resume and the host's `session_exists` read it after the session ends.

- Before the open, a resumed session's pointer still names the trail it continues, so a resume whose run never opens, because its fork was refused for one, can be tried again.
- Between the open and the blaze there is no pointer, and a summon carries no `summoned_by`.
- After the blaze, the pointer names the session run's trail, a resumed session's fork trail included:
  its summons are attributed to that trail, and the next `ride resume` continues it.

### 5.5 Trail mark

Once the trail opens, the recorder emits the trail mark once through `RunLifecycle.from_env()`,
when the session has a broker channel and its run is one-shot (the open's `interactive` is false) or the session is summoned (`bro.summon.summoned()`)
— the runs that announce their trail today.
It sends the mark whatever the run's state:
the host refuses a mark for a mission that already ended, and `Client.mark` waits for no reply.

### 5.6 Recording health

Every pass beats the recording health file with its outcome, `ok` or `error` with a one-line summary, the pass interval, and the recorder's log as diagnostics;
the final pass's beat promises no further beat.
The banner surfaces it as it does the Claude recorder's.
Judging recording health from the store alone, on the host, is #1288's.

### 5.7 Failures

- A store error fails the pass:
  its beat says `error`, nothing confirmed is undone, and the next pass re-attempts what is owed.
- A run log that breaks §3.3 ends its recording:
  the recorder writes nothing more to the trail, and beats `error` naming the file and the line until it stops.
- A selected `trails` credential that does not load ends the recorder at its start, after an `error` beat (`config not found: trails`), as the Claude recorder does.
- What the final pass cannot write stays unrecorded, and the trail stays unended;
  catching it up later is #1189's.

### 5.8 Example

A summoned one-shot child whose store fails mid-run:

1. The session runner names `runs/01m7e….jsonl`, starts the recorder on it, and spawns `bro run` with `--run-log` naming it.
2. The run writes its `open`, step 0 and step 1, its prompt.
   The recorder's pass clears the pointer, blazes trail `T` at extent 0, publishes `T`, emits the trail mark, and appends steps 0 and 1.
3. Step 2 is an `llm_call` whose response calls `summon`;
   the run writes the `tools` entry for its tool list just before it, and the recorder appends the step with that list as a blob.
   `current_tool_step_id` is `{step_id: 2, index: 0}`, and the host, reading `T` off the pointer, stamps the child with `summoned_by` `{T, 2, 0}`.
4. The store goes down.
   The run goes on writing steps 3 to 6;
   the first failing pass tried steps 3 and 4, and every pass after it resends that batch, beating `error`.
5. The store comes back:
   the batch is confirmed, and steps 5 and 6 go in the next one.
6. The run writes `end` `ok`, sends its result and exits.
   The session runner stops the recorder, whose final pass ends `T` as `ok`.

## 6. Oversized answers

A mission's result reaches its summoner in one broker message, whose result text the answer bound limits.
Today the `answer` tool refuses a longer answer, and any other result is cut to fit with a marker naming the run's trail (`truncate_answer`).
Here a longer answer is minted as an artifact first, and the marker names the artifact, which the summoner fetches to read the answer whole.

### 6.1 Sending

`RunLifecycle.completed(result, end_reason)` is the one way a run's result leaves it:
`Runner.run()`, interactive `bro chat`'s delivered answer (`bro/launch/call.py`), the Claude runner's `_complete_run`, and both harnesses' `answer` tool all go through it.
A result whose text fits the answer bound is sent as today.
For a longer one, `completed`:

1. writes the text to `<session dir>/answers/<lulid>.md`, owner-only and created exclusively;
2. mints that file with `artifact.mint_answer` (§6.2), which answers its ref;
3. sends as the result's `value`, or as a failed outcome's `error`, the text cut to fit, followed by a marker naming the ref:
   `[answer truncated; full response in artifact <ref>]`.

A mint the host refuses, as one past the ride's byte cap, falls back to today's marker,
naming the trail the session's pointer names when the result is sent, or `unavailable` when there is none:
`[answer truncated; full response in trail <trail id>]`.
A run with a broker channel but no session state directory has nowhere to mint from, and `completed` raises.
The broker's result payload does not change:
the cut text is an ordinary `value` or `error`.
The `answer` tool stops refusing a long answer (`validate_answer` and `ANSWER_TOO_LARGE` go).
`completed` reads the session's pointer itself for the fallback,
so its `trail_id` goes and the Claude runner's `_complete_run` stops reading the pointer.

### 6.2 The answer mint

`artifact.mint_answer {name}` is a broker request of its own beside `artifact.mint`, named in `bro/artifact.py` and served by `ride/ride/artifacts.py`:

- `name` is one path component.
  The host resolves it among the requesting session's records, `session/answers/<name>` under `PeerFacts.records_directory`, and refuses anything there that is not a regular file.
- The file is adopted as any mint is:
  readable by the minting peer and its summoners up to the ride root, under the ride's byte cap, and recorded in the artifact audit;
  the ref's name is the file's name.
- `artifact.mint`, the `artifact` command and the `artifact` toolset keep their rule, a path inside the workspace tree, and expose no answer mint.

The host cannot tell the run's own code from an agent in the same session, since every process there shares one channel (#1308):
what bounds the answer mint is what it can read, files under the session's `answers/`, which an agent could as well write into the tree and mint.

### 6.3 Reading

The summoner sees the cut text as it sees any result text,
through `quest_check`, the `quest` and `mission` commands and a blocking `summon`,
and reads the whole answer by fetching the ref its marker names:
the `artifact fetch <ref>` command prints the path the artifact appears at,
and the artifact toolset's `fetch` returns it, for the toolset's `read` to page through.
`bro.mission` does not change.
A summoner that cannot fetch the ref reads only the cut text:
one with neither a shell nor the artifact toolset, and one whose fetch is denied, as a manual child's is.

## 7. What changes for users

| Situation | Today | After |
|---|---|---|
| a trail-store outage during a native session | the next turn fails | the run continues, the banner shows `session_recording: FAILING`, and the trail catches up once the store is back while the recorder runs |
| `bro run`, `bro chat` and `bro chat --fork` on the host | record a trail | write a run log under `~/.local/share/ride/bro/runs/` and record no trail |
| `ask` and `call`, which are `ride solo` and `ride along` | record a trail | record a trail |
| `bro chat`'s exit line | `conversation recorded as trail T; fork it with: …` | `conversation saved to <path>` |
| `bro chat <bro> --fork` with no id | the bro's newest `call` trail, host conversations included | the bro's newest `call` trail, which only managed sessions record now |
| a resumed native session | the pointer keeps naming the original trail: summons are attributed to it, and the next resume forks it again | the pointer names the resumed trail once it opens |
| a native run nested in a session of either harness | records a trail and repoints the session's pointer at it | keeps its run log under the runtime root and records no trail |
| a summon before the native session's trail opens | cannot happen: the trail opens before the first model call | the child carries no `summoned_by`, like one summoned before a Claude session's recorder adopts its transcript |
| an answer longer than 64 KiB | the `answer` tool refuses it, and any other result is cut with a marker naming the trail | the result is cut with a marker naming an artifact that holds it whole, which the summoner fetches; a mint the host refuses falls back to the marker naming the trail |
| `--no-trails` | no trail | no trail and no recorder; the run log is still written |

## 8. Known approximations

- Summons between the session run's open and its blaze carry no `summoned_by`, and a run that ends before its blaze sends no trail mark (§5.4, §5.5).
  While the store is up, both windows last one store round trip after the open;
  during an outage they last the outage.
  #1288's host, which records every trail, is to write the summon link itself once both trails exist.
- The header's times are the recorder's write times (§5.3).
- A blaze whose reply is lost leaves an empty trail the store later reads as unreported, and the next pass blazes another, which is the session's (#1189).
- A recorder that dies mid-session stops the recording:
  its beat goes `STOPPED`, and the rest of its run log is not replayed (#1189).
- A step larger than trails-server's request cap (64 MiB) can never be appended, so its run's recording fails on it from then on;
  today such a step fails the run's turn.
- A resumed session whose trail never opens ends with no pointer, so the next `ride resume` is refused;
  its run log holds the conversation, which #1303's continuation from run logs can reach.
- Run logs under the runtime root accumulate, and a nested run's log in a box goes with the container.
- Inside a box, `bro chat`'s exit line names the path the box sees, `/var/ride/session/bro/runs/<id>.jsonl`,
  which on the host is `<workspace>/session/bro/runs/<id>.jsonl`.
- An answer artifact lives as long as the ride,
  and only a summoner that can fetch it reads the whole answer (§6.3);
  the others read its cut text, as every summoner does today.
- A refused answer mint's marker names the trail the pointer names when the result is sent,
  which may not hold the run's last steps yet, since the recorder follows the run, and is `unavailable` before the recorder's blaze.

## 9. Implementation mapping

Added in `bro-native` (`harnesses/native/harness/`):

- `bro/native/run_log.py` — the location (§3.1), the run log tracker (§3.4) and the reader (§3.5).
- `bro/native/trail_recorder.py` — the session recorder and its `bro.native.trail-recorder` console script (`sync-scripts --project harnesses/native/harness`).
- `bro/native/recorder.py` — the recorder's lifetime the session runner enters (§5.1).
- The `watchfiles` dependency, for the recorder's wake on writes, as `bro-claude` has it.

Changed in `bro-native`, under `harnesses/native/harness/`:

- `bro/native/runner.py` — §4: the run log default, `run_log`, no pointer, no mark, no `summoned_by`, the error detail.
- `bro/fork.py` — `run_log` passed to the runner.
- `bro/launch/run.py`, `bro/launch/call.py` and `bro/launch/resume.py` — `--run-log`, the exit line and the log lines.
- `bro/native/harness.py` — `run_session` per §5.1.

Changed in `bro-native-trails`:
`bro/native_trails/format.py` takes a row's `at` as its timestamp, and still a stored row's `ts`, which an import reads back.

Changed in core:

- `bro/llm/tracker.py` — the contract of §4.1.
- `bro/bro.py` — `LiveRun.trail_id`, the banner tool's pass-through of it, and the `answer` tool's `validate_answer` go.
- `bro/workspace/banner.py` — `render_banner`'s `trail_id` and `SessionFacts.collect`'s `trail_id_override` go.
- `bro/run_lifecycle.py` — §6.1:
  minting a long result, and the marker naming its ref, or, when the host refuses the mint, the trail the session's pointer names;
  `validate_answer`, `ANSWER_TOO_LARGE` and `completed`'s `trail_id` go.
- `bro/artifact.py` — the `artifact.mint_answer` kind and its client call (§6.2).

Changed in `bro-ride`:
`ride/ride/artifacts.py` serves `artifact.mint_answer` (§6.2).

Changed in `bro-claude`:
`bro/claude/runner.py`'s `_complete_run` sends its result without reading the pointer.

Changed at the root:
`conftest.py` loses the `NullTracker` pin and its docstring paragraph.

Deleted:
`harnesses/native/harness/bro/trails/record/bro.py` and its test, and with them the member's portion of the `bro.trails` namespace tree.

Documentation:

- `harnesses/native/harness/AGENTS.md` — "Components", "Recording" and the namespace-tree paragraph.
- `harnesses/native/harness/bro/native/reference.md` — "Recording and usage" and "Resume".
- `harnesses/native/trails/AGENTS.md` — the format's rows are run log steps.
- `bro/trails/AGENTS.md` — `TRAILS_DISABLED`, which turns a managed session's recorder off on either harness and is no longer the recovery path for a broken trails-server, "Recorder placement" and "Surfaces".
- `bro/llm/AGENTS.md` — `tracker.py`.
- `bro/AGENTS.md` — `run_lifecycle.py` and `artifact.py`.
- `bro/launch/AGENTS.md` — `run.py`'s recording sentence and the exit hint.
- `bro/reference/ride.md` — "Session recording", now true of both harnesses' recorders, and "Sharing artifacts between peers", for the answer mint.
- `bro/prompts/environment.md` — item 9's "the transcript isn't reaching trails".
- `README.md` — "every run on every harness is a recorded trail" becomes every session.

## 10. Testing

- The run log tracker and reader:
  step numbers, a tool list carried once in a `tools` entry and named by every `llm_call` that offers it, and `at` on every entry;
  exclusive creation, refusal after a failed write and after the end, an incomplete last line left unread, and each §3.3 violation refused.
- The runner:
  a run with no reachable store completes, consulting none;
  `current_tool_step_id` equals the run log's step number;
  a run without `run_log` logs under the runtime root, and one given `run_log` logs there.
- The recorder over a real `LocalStore`:
  the trail's rows equal the log's steps, each with its `at` as the row's time, and each tool list sent once;
  the pointer cleared at the open, published at the blaze and kept at the stop;
  the mark for a one-shot run and for a summoned session, and none for an interactive root;
  the health beats;
  a store failing then recovering, with the failed batch resent unchanged;
  SIGTERM's final pass recording the end, and the parent's exit.
- The session runner:
  the recorder started before and stopped after the run on the same path, none under `TRAILS_DISABLED`, `RIDE_SUMMONER` absent from the run's environment, and the resume pointer read before the run.
- The answer path:
  a result within the bound sent as text;
  a longer one written under `session/answers/`, minted, and sent cut to fit with a marker naming its ref, which a summoner fetches whole;
  a refused mint sent with the marker naming the pointer's trail, or `unavailable` without one;
  the host refusing an answer mint of a symlink, a directory, or a name with a separator;
  and `artifact.mint` still refusing a path outside the tree.
- The bro trail format:
  a step's `at` taken as its row's time, and a row without one read as today.
- A test waiting on the recorder waits on a signal that establishes the state it needs
  — a health beat with a given outcome, the pointer's publication, the process's exit —
  never on a sleep.

## 11. Follow-ups proposed, not part of this design

- The Claude runner emits its session's trail mark from a pointer watch (`harnesses/claude/harness/bro/claude/runner.py`);
  its recorder could emit it as the native one does, so #1288 moves one owner of the mark.
- The two recorders' lifetimes (`recorder.py` in each harness) are the same daemon start and bounded stop, which one shared helper could hold.

## 12. Decision record

Each decision below was settled in the design discussion;
the section that applies it is in parentheses.

- The run log is the harness's own format, with flat entries, `at` on every entry, a `tools` entry per tool list and `recipe` in `open`;
  native trails store its steps as their rows, and the bro trail format reads them (§3.2, §5.3).
- A. The recorder records the session run only; a nested run keeps its run log and records no trail (§2, §3.1, §5).
- B. The run log carries what the run knows; the recorder adds the session facts (§3.2, §5.3).
- C. The pointer names the session run's trail whatever its origin, a resumed session's included, and is cleared when the run opens (§5.4).
- D. A summon before the trail opens carries no `summoned_by`, and a late trail mark is lost; the host is to write the summon link itself under #1288 (§5.5, §8).
- E. `bro chat` ends by naming its run log, whoever named it (§4.4).
- F. A run log's default directory is under the runtime root wherever the run runs, and a launcher names another place (§3.1).
- G. Every entry carries its `at` (§3.2).
- H. The tracker factory, the runner's `TRAILS_DISABLED` check, the suite's `NullTracker` pin and the run's `trail_id` go; the error end's detail is `<type>: <message>`; a failed batch is resent unchanged (§3.4, §4.1, §5.3).
- I. Each `llm_call` keeps its `wire`, ready for switching models mid-run, and its model stays in its body, with no field of its own (§3.2).
- J. A long answer is minted through a request of its own that reads only the session's `answers/`, and agent-initiated mints keep the tree-only rule (§6.2).
- K. The summoner fetches a minted answer itself, and `bro.mission` does not resolve it (§6.3).
- L. The result names the minted answer's ref in its text, its head cut to fit before the marker, and the broker's result payload does not change (§6.1).
- M. A mint the host refuses falls back to today's marker, naming the trail the session's pointer names (§6.1).
- The recording health file stays, beaten by the recorder as by Claude's, and #1288 moves recording liveness to the store (§5.6).
- Minting long answers is part of this change, and the broker's limits are #1307's (§6).

## 13. Assumptions and evidence

- The bro trail format accepts a blaze with an empty `records` list (`_bro_open` in `harnesses/native/trails/bro/native_trails/format.py`), and a stored row carries the timestamp its parse returned (`build_rows` in `bro/trails/rows.py`).
- Both backends answer an append sent again unchanged as a duplicate (`LocalStore.append_records`, `DynamoStore.append_records`), and neither requires an append to carry a blob it already holds;
  only an import does.
- `NetworkStore` retries an append but never a blaze (`bro/trails/network.py`).
- `Client.mark` waits for no reply, and the dispatcher refuses a mark for a mission it no longer holds live (`bro/broker/dispatcher.py`).
- The host reads the summoning session's pointer at each summon (`PeerFacts.attribution_for_mission` in `ride/ride/peer_facts.py`), and a summon request carries only the tool call's position (`_summoned_by` in `ride/ride/bro_worker.py`).
- `spawn.popen` detaches its child into a session of its own, so a terminal's interrupt does not reach the recorder.
- The suite's environment rebuild clears `RIDE_*`, so a test's run is outside any session (`bro/base/suite_environment.py`);
  boxes set no `XDG_DATA_HOME`, so a boxed run's runtime root is the container's own (`bro.workspace.paths.runtime_base`).
- Today a resumed native session republishes no pointer, since `bro.fork.fork` opens its trail without publishing one, and a nested run publishes its own from `Runner._start`.
- Every result a run sends goes through `RunLifecycle.completed`:
  `Runner.run()`, `bro/launch/call.py`, the Claude runner's `_complete_run`, and `_complete_and_terminate` behind the Claude harness's `answer` tool.
- A mint resolves its path inside the workspace tree only (`bro.worker_types.tree_path`), and a mint is readable by the minting peer and its summoners (`bro/reference/ride.md`, "Sharing artifacts between peers").
- The `artifact fetch` command prints the path a ref appears at, and the artifact toolset's `fetch` returns it for the toolset's `read` and `grep` (`bro/artifact.py`, `bro/artifact_mcp.py`).
- Today's marker names the trail the run announced or its caller passed, and says `unavailable` without one (`truncate_answer` in `bro/run_lifecycle.py`);
  the Claude runner passes the trail its session's pointer names (`_complete_run`).
- `ride`'s launcher prints the `ride resume <workspace>` hint after a session whose harness reports it resumable (`_print_resume_hint` in `ride/ride/session.py`).
