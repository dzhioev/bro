# Design: a resume continues from local records, and every resume forks a trail

This file is the specification of [#1303](https://github.com/dzhioev/bro/issues/1303).
It lives on the branch `design/1303-unified-resume`, which is never merged:
the pull request that carries it is its design review only.

## 1. Goal, scope and constraints

Goal: a resumed session continues its conversation from the local record its harness wrote, on either harness, and reads no trail to do so.
What a resume does to trails is one rule for both harnesses:
every lifetime records what it adds into a trail of its own, forked from the trail that holds its history.
The trail store no longer decides how one trail continues another:
the session recorder decides from the session's local records and names the edge to the store.
A conversation can also be continued from a trail alone, in any workspace of the same harness:
a *trail resume* (§6) first writes the local record from the trail, and is then the same resume.

What it enables, for example:

- `ride resume` of a native session works with the record-only trails token every session holds by default, with `--no-trails`, and during a trails-server outage.
  Today it fails with `403 trails token 'sessions' may not read`, since the resume reads the trail back inside the session.
- A trail is one lifetime:
  its header's recipe, hold, `git` base, launch line, outcome and usage are that lifetime's,
  so the sum of a run's trails ([#1331](https://github.com/dzhioev/bro/issues/1331)) counts that run alone.
- `ride along --from-trail <trail> …` continues a recorded conversation of either harness in a new workspace, on any host that reads the store.
  Today only a native trail can be continued that way, by `bro chat --fork` on the host.
- The store sheds its Claude lineage resolver, the attach path and two indexes,
  so [#1288](https://github.com/dzhioev/bro/issues/1288) moves a writer that asks the store to decide nothing.

Out of scope, each with where it belongs:

- history on a summon, that is a child continuing an earlier child's conversation:
  not designed here, and §6's mechanism is what it would use;
- recording more than one record of a session at a time:
  a background session Claude's `/fork` starts, and subagent transcripts ([#528](https://github.com/dzhioev/bro/issues/528));
- recorder resilience within one lifetime:
  a recorder that died stays dead until the next lifetime ([#1189](https://github.com/dzhioev/bro/issues/1189)),
  the Claude recorder's readiness and partly kept batches ([#1320](https://github.com/dzhioev/bro/issues/1320)),
  and the two recorders' shared helper ([#1356](https://github.com/dzhioev/bro/issues/1356));
- defaulting a trail resume's attachment from the trail header's `git` block, which [#1341](https://github.com/dzhioev/bro/issues/1341) reshapes;
- moving the recorder and the recording map to the host, and a session's own trail reads (#1288).

Constraints:

- core imports no other workspace member;
  the run log is `bro-native`'s own format and the transcript is Claude Code's;
- a session's `trails` token may hold the record permission alone (`bro/trails/AGENTS.md`, "Service auth"),
  so nothing a session's processes do may need the read permission;
- backward compatibility of interfaces is not a constraint (`AGENTS.md`, "Conventions"),
  but the trails already stored are data and stay readable;
- no earlier format of a session's own records is recognised or migrated (§8), as [#1338](https://github.com/dzhioev/bro/issues/1338) decided for workspace records;
- the hosted trails-server serves installations on different revisions at once (§9);
- a violated assumption fails fast, teardown goes through context managers,
  and no outcome depends on one process's write landing before another's read unless a signal establishes the order.

## 2. Concepts

- **Lifetime** — one run of a session's harness program in a workspace, from `do-ride`'s start to its exit.
  A root start, a `ride resume` and a summoned child's run are each one lifetime;
  a resume is a new root with a fresh mission id (`bro/reference/ride.md`, "Lifecycle verbs").
- **Local record**, or record — a file a harness writes to hold a conversation, in a format the harness owns:
  a Claude Code transcript, `<session id>.jsonl` in the projects directory Claude Code keeps for the session's start directory (`harnesses/claude/harness/bro/claude/reference.md`, "Claude state"),
  or a native run log, `<run id>.jsonl` under `<session dir>/bro/runs/`,
  `<session dir>` being the session's state directory among the workspace's records (`bro/reference/ride.md`, "Workspaces").
  A record's **id** is its file name without the suffix.
- **Item** — the unit a record is recorded in:
  a transcript line, numbered from 0 by its position in the file,
  or a run log step, by its step number.
- **Conversation** — on Claude, one transcript;
  on native, a run log together with the chain of run logs it continues (§3.3).
- **History** of a lifetime — the conversation as it stood when the lifetime started.
- **Stretch** — the items one lifetime adds to one record without the session switching to another record in between.
- **Trail** — the recording of one stretch.
  A lifetime that writes to one record records one trail;
  a Claude lifetime whose user switches records inside the TUI records one trail per stretch (§4.4).
- **Edge** — a trail's `forked_from`, `{trail_id, step_id, kind}`:
  the trail and step that hold the last item of the trail's history, and how the trail came to continue it (§4.1).
  A trail without an edge is a **root**.
- **Session recorder**, or recorder — the daemon beside a managed session that writes its trails:
  `bro.native.trail-recorder` and `bro.claude.trail-recorder`.
  It is what #1288 and the task text call the trails writer.
- **Recording map**, or map — the recorder's file in the session records that says which trail holds which items of which record (§4.2).
- **Held** — an item is held when the map lists it in a trail's spans and the store holds that trail's row for it (§4.3).
- **Restored record** — a record that `ride` wrote from recorded trails for a trail resume (§6.4).
  The trails it was written from are **restored trails**: the session never writes to them.
- **Regular resume** — `ride resume <workspace>`: the same workspace continues from its own records.
- **Trail resume** — a start whose history is read from a trail (§6).
- **Start** a trail — create it in the store.
  The operation is today's `blaze`, renamed (§5.1).
- **Line row** and **spread row** — the two shapes of a native trail row (`harnesses/native/trails/AGENTS.md`):
  a line row holds one run log step line verbatim, and a spread row, written before run logs existed, holds the step's fields beside the store's own.

## 3. Local records and the regular resume

### 3.1 `ride resume`

`ride resume <workspace>` keeps what it does around the harness:
it replays the workspace's recorded session spec as a new root, interactive whatever the first mode was, on the installation's current runtime,
in the workspace's tree as the last lifetime left it, with its recorded attachments.
`--llm` replaces the recipe, and `--cred`, `--grant`, `--revoke` and `--limit` adjust the scope, as today.

The one question the launcher asks the harness, `SessionHarness.session_exists`, becomes the same question on both harnesses:
does the workspace hold a local record to continue.

| Harness | A session exists when | It is refused with |
|---|---|---|
| `claude` | the projects directory of the workspace's start directory holds a transcript, as today | `no claude session found for <workspace> in <directory>` |
| `bro` | the session has a continuable log, defined below | `cannot resume bro harness workspace '<name>': it holds no session run log`, or the hint of §8 when its records predate format 2 |

A native session's **continuable log** is found by reading the run logs under `<session dir>/bro/runs/` newest first, by name.
A file without a complete `open` entry, and a log of format 2 (§3.3) without a step 0, are the debris of a start that died before its first step, and are passed over.
The first other log is the continuable log when it is of format 2;
when it is of another format, the session's records predate this design (§8).

The trail pointer is not read:
a session that ran with `--no-trails`, or whose recorder never started a trail, resumes like any other.

### 3.2 Claude

The runner passes `--resume <session id>` for the workspace's most recently modified transcript, as today,
and Claude Code appends to that transcript, in the TUI and in print mode alike (§15).
The runner never passes `--fork-session`:
a resume continues the record in place, and only its trail forks (§4.4).

### 3.3 Native: one run log per lifetime, naming the one it continues

A native lifetime writes a new run log, as today.
The run log format becomes **format 2**, which differs from format 1 in its `open` entry alone;
every other entry and rule stays what `harnesses/native/harness/bro/native/run_log.py` writes and checks today:

- `format` is `2`;
- `forked_from`, present on a run that continues another, is `{"run": <run id>, "step": <step number>}`:
  the run log it continues, which lies in the same directory, and the last step of that log its conversation includes;
- `interactive` is `false` for a one-shot run, continued or not, and `true` for a conversation.

A run therefore knows no trail id.
The reader reads format 2 alone and refuses any other, naming the file.

The **chain** of a run log is the log itself preceded by the chain of the log its `forked_from` names.
The `step` a log's `forked_from` names is the **bound** of the log it continues: the chain includes that log's steps through it.
A `forked_from` naming a file its directory lacks, or a step that file does not hold, is refused, naming both.

For example, a session started fresh, resumed once after a clean stop, and resumed again after it was killed in the middle of a tool call:

```text
01m7a.jsonl  open {format 2}                                    steps 0..9    end ok
01m7b.jsonl  open {format 2, forked_from {run 01m7a, step 9}}   steps 0..14   (no end: killed after step 14, an llm_call making a tool call)
01m7c.jsonl  open {format 2, forked_from {run 01m7b, step 13}}  steps 0..
```

A run that continues a log rebuilds its conversation from the chain, by the rules `bro.fork` applies to a trail's steps today:

- **The fork step.**
  Unless the caller names one, it is the continued log's *latest consistent step*, the newest step after which no tool call is unanswered:
  a `user_input` or `notification` recorded while none is pending,
  an `llm_call` not marked `stopped` whose response makes no tool call,
  the `tool_result` that answers the last pending call,
  and, in a log that itself continues another, its `system_prompt` step, so that a log holding nothing else continues through the log before it.
  A log with no such step is refused.
  In the example, `01m7b`'s step 14 left a call unanswered, so `01m7c` continues it at step 13.
- **The prefix.**
  The conversation is the chain's steps in order, root first, each log up to its bound, without their `system_prompt` steps.
- **The prompt.**
  The run's system prompt is the continued log's, unless the run's hold differs from that log's `hold`;
  then the prompt is composed again under the new hold.
  The new log's step 0 holds the prompt the run uses.
- **The wire.**
  Every `llm_call` of the prefix must have been made over the target recipe's wire, or the run is refused before it opens, naming the recorded wire.
  A server-chained wire seeds its chain from the fork step when that step is a completed `llm_call` carrying a response id, the recipe's provider and model are the continued log's, and the prompt is unchanged;
  every other case restores the wire's state from the prefix's own records.

### 3.4 `bro chat --fork` and `bro run --fork`

- `bro chat <bro> [what] --fork [RUN_LOG] [--at N]` opens a conversation that continues the run log at path `RUN_LOG`, at step `N` as given or else at its latest consistent step,
  and renders the earlier exchanges from the chain as history.
- `bro run <bro> <input> --fork [RUN_LOG] [--at N]` is new:
  it continues the run log for the one input, under the one-shot turn-end and result rules of any `bro run`.
  `Runner.run()` accepts a runner that `bro.fork.fork` preseeded, as `Runner.send()` does.
- With the value omitted, `RUN_LOG` is the bro's newest run log in the default directory, `<runtime root>/bro/runs/`:
  the newest by name among its 200 newest files whose `open` names the bro.
- The new run log is created in `RUN_LOG`'s directory;
  a `--run-log PATH` naming another directory is refused with `--fork`.
- The run uses `RUN_LOG`'s recorded recipe, with source `run log <run id>`, unless `--llm` or the managed `--resolved-recipe` replaces it;
  `RUN_LOG`'s `open` must name the bro given.
- `--continue-trail` goes, and `--fork` no longer takes a trail id.
  `bro.fork.fork` takes a run log path and a step, and `bro.launch.resume` reads run logs;
  nothing in `bro-native` reads a trail store.

The session runner (`BroHarness.run_session`) uses the same flag.
On `do-ride … --resume` it takes the session's continuable log (§3.1),
and runs `bro chat` for an interactive session, or `bro run` for a one-shot one (§6.5), with `--fork <that log> --run-log <a new name beside it>` and no `--at`.

## 4. Trails: one per stretch

### 4.1 The edge and its kind

Every trail records one stretch (§2).
A stretch that continues a conversation gets a trail whose edge names where the conversation's last item is recorded, with one of three kinds:

| `kind` | The trail records | Set when |
|---|---|---|
| `resume` | the same conversation's next lifetime, continued from the session's own record | the history's last item is held by a trail a lifetime of this session wrote |
| `trail` | a conversation continued from history read out of the store | the history's last item is held by a restored trail (§6.4) |
| `branch` | a new conversation copied from another inside the harness | a new Claude transcript copies a known record, as `/branch` and `--fork-session` make one |

A copy is a `branch` whichever trail holds its history.
A trail with no history, or whose history no trail holds, is a root.
Nothing reopens a trail:
once the recorder that started it has ended it, it is final.

### 4.2 The recording map

The map is `<session dir>/recording-map.json`, among the session's records and reachable from inside the session like the trail pointer beside it.
It is replaced atomically, and one process writes it at a time:
the recorder during a lifetime, and `ride` before one starts (§6.4), both under the workspace's session lock.

```json
{
  "format": 1,
  "records": {
    "5a0c1f0e-…": {
      "trails": [
        {"id": "01m7k0a…", "spans": [[0, 212]], "restored": false, "ended": true, "forked_from": null},
        {"id": "01m7q3c…", "spans": [[212, null]], "restored": false, "ended": false,
         "forked_from": {"trail_id": "01m7k0a…", "step_id": 211, "kind": "resume"}}
      ]
    }
  }
}
```

1. `records` is keyed by record id.
   Each element of a record's `trails` is one trail's **entry**, and the entries are in the order the trails were started.
2. `spans` are half-open `[start, end)` ranges of the record's items, in increasing order;
   no item lies in two spans of one record.
   Only the last span of a record's last trail may be open, with `end` null: it takes every later item.
3. The item at position p of a trail's spans, read in order, is that trail's step p.
4. `restored` marks a restored trail.
5. `ended` says a recorder ended the trail.
6. `forked_from` is the edge the trail was started with, or null for a root.
7. A missing file is an empty map.
   A file that is not of this shape ends the recording as a refusal (§4.8).

A native run log has one trail, whose one span starts at step 0.
A Claude transcript has one trail per stretch.

### 4.3 What the store holds

The map stores no progress.
How far a trail got is the store's own count of its rows, its **extent**,
which the recorder reads with the record permission alone:
an append of no records at an offset is answered with the extent when the offset equals it, and refused with the actual extent otherwise.

- **Held.**
  An item at position p of a trail's spans is held when p is below that trail's extent.
  Every item in a restored trail's spans is held; the recorder never probes one.
- **Verified.**
  Before the recorder relies on a trail an earlier lifetime wrote, to fork from it or to complete it,
  it sends the trail's last held item again as an append at the offset before the extent,
  and requires the store's duplicate answer, which the store gives only when the row it holds has that item's digest.
- **Settling a record's entries.**
  Before the recorder relies on a record the map knows, it settles the record's last entry, and the one before it whenever it removes one, until an entry stands or none is left:
  the entry of a restored trail stands;
  an entry whose trail the store does not hold was never started, and is removed;
  an entry whose trail holds no row is removed too, leaving an empty trail the store reads as unreported;
  an entry that verifies stands;
  an entry that does not verify, or whose extent exceeds the items its spans can name, means the record is no longer what was recorded:
  every entry of the record is removed, and the recorder logs the trails it gave up.
  A record left without entries is one the map does not know.

### 4.4 Starting a trail on Claude

The recorder keeps today's choice of the record to follow:
among the transcripts modified since it started, the most recently modified one,
switching away from the one it follows only once that one is quiet.
When it follows a record, it settles the record's entries and then starts a trail that takes **every item of the record no trail holds, from the first such item on**.

**A record the map knows.**
The trail's spans are `[[first, null]]`, `first` being the item after the last held one,
and the open span of the record's last trail is closed there.
Its edge names the record's last trail and that trail's last held step, with kind `resume`, or `trail` when that trail is restored.
The recorder waits while the record holds no item from `first` on.
So when the previous lifetime's recorder fell behind, the lines it missed are the new trail's first rows.

**A record the map does not know.**
The recorder looks for the record it copies, its **origin**, among the records the map knows whose files exist:
the one a `forkedFrom.sessionId` on the record's first `uuid`-carrying line names,
and otherwise the one whose file carries that line's `uuid`, the one sharing the longest run of the record's leading `uuid` lines when several do.

- Without an origin, the trail is a root with the spans `[[0, null]]`.
  The recorder waits until the record carries a `uuid` line, as today.
- With an origin, the recorder waits until the record carries a `uuid` line the origin's file lacks, its first own record;
  until then the copy may still be being written.
  Let `c` be the record's first `uuid` line and `m` that first own one.
  A copied line, one in `[c, m)` whose `uuid` the origin carries, is held when the origin's line with that `uuid` is held,
  through the origin's own origin where the origin copied it in turn.
  With `a` the last held copied line, the trail's spans are `[[0, c], [a + 1, null]]`, without the first when `c` is 0,
  and its edge names the trail and step holding that line's original, with kind `branch`.
  When no copied line is held, the trail is a root with the spans `[[0, null]]`.

The origin's line for a `uuid` is found by reading the origin's file,
and "through the origin's own origin" is the same lookup in the record that has the trail the edge of the origin's first trail names.

**Switching and ending.**
When the session's writing moves to another record, the recorder ends the trail it was writing, with `ok`, or with `raised` under today's rule, marks it `ended`, and clears the pointer;
the record it turns to is followed by the rules above, a record it recorded earlier in the same lifetime included.
A transcript that shrank below what its trail holds ends that trail, as today, and its entries are removed.
At shutdown the recorder makes its final pass and ends the trail it is writing.

For example, on the pinned Claude Code (§15):

| The user | Claude Code writes | The recorder starts |
|---|---|---|
| runs `ride resume` | appends to transcript `A` | a trail for `A`'s new lines, edge `{A's last trail, its last step, resume}` |
| types `/clear` | a new transcript `B`, sharing no record with `A` | a root for `B` |
| types `/resume` and picks `A` | appends to `A` | another trail for `A`, edge `{A's last trail, its last step, resume}` |
| types `/branch` | a new transcript `C` whose head copies `A`'s records, each carrying `forkedFrom` | a trail for `C`'s own lines, edge `{the trail holding A's last copied line, that step, branch}` |

### 4.5 Starting a trail on native

The recorder follows the session run's log `L`, as today.
When it reads `L`'s `open`:

1. Without a `forked_from`, `L`'s trail is a root.
2. With one, the recorder first makes sure the continued log `P` is recorded, then starts `L`'s trail with the edge `{P's trail, forked_from.step, kind}`,
   kind `trail` when `P`'s trail is restored and `resume` otherwise.

Making sure `P` is recorded:

- `P`'s entry is restored: nothing to do.
- `P` has an entry an earlier lifetime wrote, which settles (§4.3):
  the recorder appends `P`'s steps from the trail's extent on, in the batches and with the tool lists of a live recording,
  and, when `P` holds an `end` entry and the trail is not marked `ended`, ends the trail with it and marks it.
- `P` has no entry, or its entry was removed:
  the recorder first makes sure of the log `P` itself continues, if it continues one, so that logs are recorded oldest first,
  then starts a trail for `P`, records it whole, and ends it from `P`'s `end` entry when `P` holds one.
  That trail carries what `P`'s `open` says, the bro, version, recipe, `interactive`, `surface` and hold, and the session's `location`;
  it carries no `git`, no launch line and no `summoned_by`, which were its own lifetime's facts and are not in the log.
  Its edge follows the same rules.

Every step of `L` is then recorded as today (step n of the log is step n of its trail), and `L`'s trail is ended from `L`'s `end` entry and marked.

In §3.3's example, with a store that was down while `01m7b` ran:
the recorder of `01m7c` finds `01m7b`'s trail holding steps 0 to 3, appends steps 4 to 14, leaves it unended since `01m7b` has no `end`,
and starts `01m7c`'s trail with the edge `{01m7b's trail, 13, resume}`.
Steps 4 to 14 keep their own times, since a line row takes its step line's `at`.

### 4.6 Trail ids the recorder names

The recorder mints each trail's id, a `lulid`, writes the trail's entry to the map, and only then sends the start request, which carries the id (§5.1).
A start whose reply was lost is sent again on the next pass with the same id and answered with the trail it already started,
so a lost reply strands no empty trail, on either harness.

### 4.7 Pointer, trail mark and health

- The recorder publishes the trail pointer when a trail's start is answered, as today.
  The pointer stays what summon control reads for `summoned_by` and what the banner shows; a resume no longer reads it.
- The native session runner still removes the pointer before it starts the recorder, and no longer puts it back when the run never opens.
- The trail mark and the recording health file are unchanged.
- The native resume's warning that the trail may lack the previous run's last steps goes:
  §4.5 completes that trail.

### 4.8 Failures

- A store error fails the pass, and the next pass attempts again what is owed: a probe, a completion, a start, an append.
  The session's run is never held up: it reads and writes local records alone.
- A refusal no later attempt can change ends the recording, as today:
  an invalid request, a denied permission, a collision on a trail id (§5.1), a run log that breaks its rules, a map that is not of §4.2's shape.
- A trail the recorder gave up while settling (§4.3) is logged with its id and left as it is.

## 5. The store

### 5.1 `start_trail`

`blaze` is renamed `start` everywhere it is named:
`TrailsStore.start_trail(request: StartRequest)`, the server's handler, the recorders, the test helpers and the prose.
The route stays `POST /v1/trails`, and no wire field carries the old word.

A start request gains two optional members and loses one:

- `id` — the id to start the trail under.
  It must be a `lulid`.
  When a trail with that id exists and the fields a start sets on it equal the request's, the store answers that trail, with its extent, and creates nothing;
  when they differ, it refuses the request as a collision, as an import of a different trail under a taken id is refused.
  Without an `id`, the store mints one as today.
  `NetworkStore` retries a start that names an id on its schedule for transient failures, and still sends one without an id once.
- `forked_from.kind` — one of `resume`, `trail` and `branch`.
  The store validates it, keeps it in the header and serves it.
  A `forked_from` without a kind stays valid: every trail stored before this design has none.
- `lineage` goes, with landing 4 (§9).

The answer is `{id, started_at, extent}` alone:
`201` for a trail the request created, `200` for one it found under its id.
Until landing 4, a request that carries lineage evidence is still resolved and answered with its verdict, as today.

### 5.2 What goes

- the start request's `lineage` evidence and the verdict in its answer (`adopted`, `chunks`, `attached`, `forked_from`, `reason`);
- `TrailFormat.resolve_lineage`, `backends.resolve_lineage` and `harnesses/claude/trails/bro/claude_trails/lineage.py`;
- attach: `backends.attached_header`, `ATTACH_CONTENDED` and both stores' `_attach`;
- the lineage index: `find_segment_trails` and `holds_record` in both stores,
  DynamoDB's `segment-started_at-index` and `uuid-index` with the derived `segment` attribute,
  and the `trails_by_segment` and `steps_by_uuid` indexes with `segment_headers` and `holds_record` in #1174's design;
- the `lineage_head` fold and the verdict type (`LineageHead` and `LineageDecision` in `bro/trails/lineage.py`, `rows.inherited_native`, `replayed_native` and `minted_native`).

`lineage_head` stays a reserved name among the server-derived native fields:
a writer naming it is refused, and a recompute or an import drops it.
A header stored with one keeps it until then, unread, and the trail format is not bumped.
`bro/trails/lineage.py` keeps `walk_chain` and `walk_header_chain`.

### 5.3 Readers

Chain walks, the `forked_from` list selector and its index, and the refusal to delete a trail that has forks read the edge as today.
`rewind tree` shows each edge's kind beside its step, and the conversation view names the kind at each boundary between the trails of a chain;
an edge without a kind shows none.

## 6. Trail resume

### 6.1 The command

`ride along` and `ride solo` take `--from-trail TRAIL_ID` and `--at STEP`:

```text
ride along --from-trail 01m7k0a… --repo git@github.com:me/project.git --into fix-branch dev
ride solo  --from-trail 01m7k0a… --at 41 -w follow-up dev 'why did you drop the retry?'
```

- The history is the trail's conversation through `STEP`, a step id of that trail, or through its last step without `--at`.
  `--at` requires `--from-trail`.
  The session continues that history as any resume continues a record:
  the bro harness at its latest consistent step (§3.3), and Claude Code by its own loading of the transcript.
- It is an ordinary start in every other respect:
  the workspace, the tree and its attachments, the hold, the recipe, the credentials and the authority resolve as for any `ride along` or `ride solo`,
  and the trail contributes the history alone.
  A workspace named with `-w` is reused under the launch's usual rule, so the launch names the attachment it has.
- `--from-trail` is refused with `--summoned`.
  `ride resume` takes neither flag.
- The launch line the session runs under (`RIDE_COMMAND`) carries both flags.
  The session spec recorded for `ride resume` does not: the next `ride resume` of the workspace is a regular one.

### 6.2 Checks before a workspace exists

The launcher reads the trail's header, and through `forked_from` every ancestor's, from the store its own credential selection names:
the `ride` command's `user.creds` and `user.tools` picks (`bro/setup/AGENTS.md`), which must select a `trails` instance holding the read permission.
With the launch's other preconditions, before a workspace is created, it refuses the launch when:

- the store refuses the read, or holds no such trail: the refusal names the trail and, for a denied read, the permission;
- `STEP` is not a step the trail holds, or the chain holds no step at all;
- a trail of the chain was not recorded by the launch's harness, naming the harness that recorded it and `--harness`;
- the trail's `bro` is not the launch's bro;
- the session would record to another store than the one the trail was read from, unless it runs with `--no-trails`,
  since the first trail it records forks from that trail;
- the harness refuses the history (`SessionHarness.history_refusal`):
  the bro harness refuses a chain whose recorded recipes are not all on the launch recipe's wire, naming the recorded wire.

### 6.3 Reading the history

The history is the chain's trails, root first, each through its **bound**, the step its child forked at, and the named trail through `STEP`:
their rows as stored, with spilled bodies resolved, and for a native chain the tool lists its `llm_call` rows name by digest.
A trail still being recorded is read as far as the store holds it.
Core carries it to the harness as one value (`bro/trails/history.py`), read through `TrailsStore` alone.

### 6.4 Writing the records

Under the workspace's session lock and before `do-ride` is launched, the launcher has the harness write the history into the workspace's records (`SessionHarness.restore_history`).
The map's entries are written first, then the files, each under a temporary name and renamed into place;
a failure removes the temporary files and fails the launch.
A trail of the chain that contributes no row is skipped.

**Native.**
One run log per trail of the chain, root first, under `<session dir>/bro/runs/`, with fresh run ids, which therefore sort in chain order:

- `open` from the trail's header: `format` 2, `version`, `bro`, `recipe` from `native.llm`, `interactive`, `surface`, `hold`, the header's `started_at` as `at`,
  and, for every trail but the root, `forked_from` naming the run log written for its parent and the header's `forked_from.step_id`;
- one step per row through the trail's bound, so that step n of the log is step n of the trail:
  a line row's line verbatim, and a spread row's fields written as a step line with the row's `ts` as its `at`;
- a `tools` entry before the first `llm_call` that names each tool list;
- no `end` entry.

Each log's map entry is `{"id": <the trail>, "spans": [[0, <steps written>]], "restored": true, "ended": true, "forked_from": <the header's>}`.
A header that lacks a fact `open` requires refuses the launch, naming the fact.

**Claude.**
One transcript, `<a new session id>.jsonl`, in the projects directory of the workspace's start directory:
the rows' bodies, which are transcript lines verbatim, of every trail of the chain in order, each through its bound.
Its map entry lists one restored trail per chain trail, each with the span of the lines written from it.

### 6.5 The start

The launcher starts `do-ride` with `--resume`, which now means: continue the workspace's newest record.
The harness's session runner does what it does for a regular resume:
Claude Code is passed `--resume <the new session id>`, with the prompt when the launch has one, in print mode for `ride solo`;
the bro harness runs `bro chat --fork` or `bro run --fork` over the session's continuable log, which is the newest restored one, with the prompt.
The first trail the recorder starts has the kind `trail`: §4.4's and §4.5's rules find the history's last item held by a restored trail.
Nothing else differs from a regular resume, and every later lifetime of the workspace is one.

### 6.6 Limits

- A trail resume needs the trail's harness: a Claude trail cannot continue under the bro harness, nor the reverse.
- What Claude Code keeps beside a transcript does not come back: subagent transcripts, the files holding oversized tool results, and file checkpoints.
- The tree is the launch's: the trail restores no file, and uncommitted work of the recorded session is in no trail.
- A native trail whose header lacks a fact its run log's `open` requires, such as `hold`, cannot be restored.

## 7. What changes for users

| Situation | Today | After |
|---|---|---|
| `ride resume` of a native session with default credentials | fails: `403 trails token 'sessions' may not read` | resumes; no trail is read |
| `ride resume` of a native session that ran with `--no-trails` | refused: no trail pointer | resumes |
| `ride resume` of a native session while the store is down | fails | resumes, and its trails catch up once the store is back |
| a resumed Claude session's trail | the transcript's trail, reopened | a new trail, edge kind `resume` |
| a resumed native session's trail | a new trail forked from the pointer's trail, its history read from the store | a new trail, edge kind `resume`, forked from the trail of the run log it continues |
| steps the previous lifetime's recorder did not record | Claude: appended to the reopened trail; native: missing from the resumed conversation | Claude: the new trail's first rows; native: appended to the previous run log's trail |
| Claude's `/branch` | a fork the store's resolver decides | a fork of kind `branch` the recorder decides |
| returning to an earlier transcript with `/resume` inside one Claude lifetime | not recorded during that lifetime | recorded as a further trail |
| `bro chat <bro> --fork <trail>` | forks a trail read from the store | gone: `--fork` takes a run log, and a trail is continued with `ride along --from-trail` |
| `bro chat <bro> --fork` with no value | the bro's newest trail recorded as a conversation | the bro's newest run log, whatever surface wrote it |
| continuing a conversation from its trail | native alone, in-process on the host | `ride along` or `ride solo` with `--from-trail`, on both harnesses |
| a one-shot continuation | none | `ride solo --from-trail`, and `bro run --fork` in-process |
| a start whose reply is lost | Claude converges on the trail; native strands an empty one | neither strands one |
| `rewind tree` | forks | forks with their kinds |
| the store operation that creates a trail | `blaze` | `start` |

## 8. Existing workspaces

No code recognises or migrates an earlier format of a session's records.

- **Native.**
  A workspace whose records predate format 2 (§3.1) is refused by `ride resume`:
  `cannot resume bro harness workspace '<name>': its session records predate run log format 2; continue it from its trail with: <command>`,
  the command being the workspace's launch line with `--workspace <name>` and `--from-trail <the pointer's trail>`,
  or `it published no trail to continue from` when the pointer is absent.
  Format-1 logs stay on disk, unread.
- **Claude.**
  A workspace resumes as before.
  Its transcript is a record the map does not know, so the first lifetime that writes to it records it from its first line into a new root (§4.4).
  The lines earlier lifetimes recorded are then stored twice, in the old trail and in the new root, with no edge between them.
- The hint `ride resume` gives for a session record that predates resolved recipes names `ride along --from-trail` in place of `bro chat --fork`.

## 9. Landings and rollout

The hosted trails-server answers every installation that records to it, and those move to a revision one at a time,
while a root keeps the runtime it froze for its whole lifetime.
So the store accepts what new recorders send before any sends it, and stops accepting what old recorders send only after none is left.

| Landing | Carries | Its rollout |
|---|---|---|
| 1 — the store accepts | the rename (§5.1) first, then `id` and `forked_from.kind` in both stores, the wire and the server, and `rewind`'s kinds (§5.3) | redeploy the trails-server, then re-sync installations in any order |
| 2 — local resume | run log format 2, `--fork` over run logs, the native session runner and `session_exists` (§3), the map and both recorders (§4), §8's refusals | with landing 3's |
| 3 — trail resume | `--from-trail` and `--at`, the history value, both harnesses' `restore_history`, `bro run --fork` (§3.4, §6) | re-sync installations once landings 1 to 3 are merged and landing 1's server is deployed |
| 4 — the store sheds lineage | §5.2 | redeploy the trails-server and apply the index removal, once every installation recording to the hosted store runs landing 2 or later and no session started before it is live |

- Landings 2 and 3 roll out together, so no installed runtime refuses a native session of §8 without offering `--from-trail`.
- Between landings 2 and 4, recorders of both generations write to one server:
  an older Claude recorder still starts its trails with lineage evidence, and the server still resolves it.
- After landing 4 a Claude recorder from before landing 2 that starts a trail is refused, since its request names `lineage`;
  its session runs on unrecorded, its banner reading `FAILING`.
  A native recorder from before landing 2 sends no evidence and keeps recording.
  The condition on landing 4's rollout is what keeps that from happening.
- An installation that records to its local store moves its store code with its recorders, in one step.
- Rollback: landing 1's server stays;
  landings 2 and 3 roll back by re-syncing to the commit before them, after which the older runtime cannot resume a workspace whose continuable log is of format 2;
  landing 4's server rolls back to the image before it, and the indexes need not come back, since no recorder asks what they answered.

[#1174](https://github.com/dzhioev/bro/issues/1174)'s design names what §5.1 renames and §5.2 removes:
its `## Design changelog` takes an entry when landing 1 and landing 4 merge, or its implementation follows this file if it starts later.

## 10. Known approximations

- After a recording that fell behind, a Claude trail's first rows were written by the lifetime before it (§4.4), so that trail's usage includes theirs.
- A native trail recorded for a log no trail held (§4.5) carries the session's `location` and none of that lifetime's `git`, launch line or `summoned_by`, and its times are the recorder's.
- While the recorder completes or records earlier logs (§4.5), the session run's trail is not started:
  the pointer is absent and a summon carries no `summoned_by`, as before any trail starts.
- A lifetime that writes nothing to its record starts no trail, so lines an earlier recorder missed stay unrecorded until a later lifetime writes.
- A trail given up while settling (§4.3) and an empty trail whose entry was removed stay in the store, unended, read as unreported.
- A recorder that dies mid-lifetime stops the recording until the next lifetime (#1189).
- Verification checks a trail's last row alone; a record changed before that row and unchanged at it goes unnoticed.
- A server-chained wire's stored response can have expired when a conversation is continued much later;
  the first exchange then fails, as it does today.

## 11. Implementation mapping

Landing 1:

- `bro/trails/model.py` — `StartRequest` with `id`, and `kind` in the `forked_from` pointer.
- `bro/trails/store.py`, `local.py`, `server/dynamo.py`, `network.py`, `server/server.py`, `backends.py`, `importing.py`, `record/spine.py` — `start_trail`, the start under a given id and its collision, the kind kept and served.
- `bro/trails/display/`, `rewind.py` — the kind in the tree and at chain boundaries.
- every caller and helper that says `blaze`: both recorders, `benchmark/`, `oops/trails/server/`, the tests.

Landing 2:

- `bro/trails/record/recording_map.py` (new) — the map, its shape and its atomic replacement.
- `harnesses/native/harness/bro/native/run_log.py` — format 2 and the chain.
- `harnesses/native/harness/bro/fork.py`, `bro/launch/resume.py`, `bro/launch/call.py` — forking a run log, the history from the chain, `--fork [RUN_LOG]`, no `--continue-trail`.
- `bro/llm/tracker.py` — `start`'s `forked_from` is the run log's own `{run, step}`.
- `harnesses/native/harness/bro/native/harness.py`, `recorder.py` — `session_exists`, `missing_session_error`, the runner's pick of the log to continue, the pointer no longer restored, no warning.
- `harnesses/native/harness/bro/native/trail_recorder.py` — §4.5 and §4.6.
- `harnesses/claude/harness/bro/claude/trail_recorder.py` — §4.4 and §4.6 in place of the evidence it sends.
- `ride/ride/session.py` — the hint of §8.

Landing 3:

- `ride/ride/cli.py`, `flags.py`, `session.py`, `do_ride.py` — the flags, the checks, the restoring step, `--resume` for a trail resume.
- `ride/ride/harness.py` — `history_refusal` and `restore_history` on `SessionHarness`.
- `bro/trails/history.py` (new) — the history value.
- `harnesses/native/harness/bro/native/` and `harnesses/claude/harness/bro/claude/` — each harness's two operations.
- `harnesses/native/harness/bro/launch/run.py`, `bro/native/runner.py` — `bro run --fork`.

Landing 4:

- §5.2's deletions in `bro/trails/`, `harnesses/claude/trails/`, and the index definitions `bro.oops.cdk` deploys.

Documentation, with the landing that changes what it describes:
`bro/reference/ride.md` ("Lifecycle verbs", the mode verbs' flags, "Workspaces", "Summoning another bro", "Session recording"),
`harnesses/native/harness/bro/native/reference.md` ("Recording and usage", "Resume", "In-process surfaces"),
`harnesses/claude/harness/bro/claude/reference.md` ("The session runner", "Session recording"),
`bro/trails/AGENTS.md`, `harnesses/claude/trails/AGENTS.md`, `harnesses/native/harness/AGENTS.md`, `bro/launch/AGENTS.md`, `ride/AGENTS.md`, `bro/AGENTS.md`,
and `bro/reference/extending.md` for the harness seam.

## 12. Testing

- **Run logs.**
  Format 2's `open`; a format-1 log refused; the chain, with a missing origin and a step the origin lacks refused;
  the latest consistent step over a log killed mid-tool-call, and over a log holding its system prompt alone.
- **Continuing.**
  A conversation rebuilt from a chain of three logs with no store reachable;
  `bro chat --fork` and `bro run --fork` over a path and with the value omitted;
  the new log created beside the continued one, and another directory refused;
  the wire mismatch refused.
- **The map.**
  Its shape and each rule of §4.2 refused when broken; atomic replacement; a missing file read as empty.
- **Settling.**
  Over a real `LocalStore`: an entry never started, an empty trail, a verified trail whose span closes,
  a trail whose last row differs, and an extent past the record, each with the outcome §4.3 gives.
- **The Claude recorder.**
  Each row of §4.4's table over synthetic transcripts;
  a resumed transcript whose earlier recorder fell behind;
  a copy of a copy, with the edge reaching through to the trail that holds the line;
  a copy read while it is being written;
  a start whose reply is lost, answered under the same id.
- **The native recorder.**
  A root; a resumed log with its predecessor complete, behind, and unknown to the map; a chain of two unrecorded logs recorded oldest first;
  the edge's kind after a restored log.
- **The store.**
  The contract suite, over `LocalStore` and over `NetworkStore` on a loopback server:
  a start under a given id, the same start again, a different start under that id, the kind kept and served, an unknown kind refused.
- **Trail resume.**
  Each refusal of §6.2 before any workspace exists;
  a native chain of line rows and of spread rows restored and continued, in both modes;
  a Claude chain restored into a transcript whose lines are the rows' bodies in order;
  the first trail's edge and kind; the next `ride resume` regular.
- **Live probes against the pinned Claude Code**, with the repository's other `*_llm_test.py`:
  `--resume` appends to the transcript in the TUI and in print mode;
  `/clear`, `/branch` and `--fork-session` write the files §15 describes;
  a transcript written from rows resumes with its history, in another directory, in print mode.
- A test waits on the signal that establishes the state it needs, never on a sleep.

## 13. Decision record

The owner settled these in the design discussion, from 2026-11-16 to 2026-11-24:

1. A resume uses local data alone and never depends on trails (the task's seed; §3).
2. Every resume forks: a lifetime's stretch is a trail of its own, on both harnesses (§4).
3. The edge carries a `kind`: `resume`, `trail` or `branch` (§4.1).
4. A trail resume writes the local record from the trail on the host and is then a regular resume (§6).
5. A trail resume's tree comes from the launch's own flags (§6.1).
6. Trail resume is designed here and built as a landing of its own (§9).
7. `--from-trail` works in both modes, and `bro run` learns to continue a run log (§3.4, §6.1).
8. `blaze` is renamed `start` (§5.1).
9. The remaining points of the proposal the owner approved on 2026-11-23:
   Claude continues its transcript in place and native writes a run log per lifetime (§3.2, §3.3);
   `--fork` over run logs (§3.4);
   the map (§4.2), the extent as progress and the verification (§4.3);
   completing and recording earlier logs (§4.5);
   trail ids the recorder names (§4.6);
   the pointer (§4.7);
   what the store gains and loses, with no format bump (§5);
   an ordinary start constrained by the trail (§6.1, §6.2);
   no migration (§8);
   the four landings (§9).

## 14. Details this file fixes beyond that proposal

Each follows from a decision above.
The owner approved them with the written file on 2026-11-24:

- the map's path and JSON shape (§4.2);
- how a copied Claude record's held lines and edge are found (§4.4);
- a run log continues only a log in its own directory, and the new log is created there (§3.3, §3.4);
- `--fork` without a value picks the bro's newest run log of any surface (§3.4);
- a native trail recorded late carries no `git`, launch line or `summoned_by` (§4.5);
- a start naming an id is retried by `NetworkStore` (§5.1);
- `--from-trail` is refused with `--summoned` (§6.1);
- the wire check before a workspace exists reads the chain's recorded recipes (§6.2);
- a restored native log carries no `end` (§6.4);
- an entry whose trail the store does not hold, or holds empty, is removed alone, the record's earlier entries standing (§4.3);
- a trail resume continues at the latest consistent step of the history `--at` bounds (§6.1);
- a trail that contributes no row is skipped when records are written (§6.4);
- the wording of the refusals (§3.1, §8).

## 15. Assumptions and evidence

- **Claude Code 2.1.322**, the pinned release, probed live in an isolated config directory on 2026-11-16:
  `--resume <id>` appends to `<id>.jsonl` in print mode and in the TUI;
  the in-TUI `/resume` appends to the transcript it switches to;
  `/compact` stays in its file;
  `/clear` opens a new transcript sharing no record with the old one;
  `/branch` opens a new transcript whose copied records keep their `uuid` and each carry `forkedFrom {sessionId, messageUuid}`;
  `--fork-session` opens one whose copied records keep their `uuid`, with `sessionId` rewritten and no `forkedFrom`, after head lines that carry no `uuid`;
  a transcript copied verbatim under a new session id, into another config directory and working directory, resumes with its history.
  `harnesses/claude/trails/bro/claude_trails/lineage.py` and the harness reference say an interactive resume copies the history, which this release does not.
- An append of no records is answered with the extent at a matching offset and refused with the actual extent otherwise,
  and a batch sent again over rows with its digests is answered as a duplicate
  (`LocalStore.append_records`, `DynamoStore.append_records`, and `_handle_append_records` in `bro/trails/server/server.py`, which admits an empty list).
- An append does not look at a trail's `end`, and `end_trail` replaces it (`bro/trails/local.py`, `bro/trails/server/dynamo.py`).
- A reader refuses a chain whose fork bound lies past the steps its parent holds (`RecordedAdapter._segment_messages` in `bro/trails/display/recorded.py`),
  which is why §4.5 completes a trail before forking from it.
- The bro trail format requires `native.llm` alone of a start (`_bro_validate_create`),
  and a start checks only the shape of `forked_from` (`bro/trails/model.py`).
- The start is the one store write `NetworkStore` does not retry (`bro/trails/network.py`).
- A start and an import refuse unknown fields (`BlazeRequest.from_wire`, `_validate_pointer`),
  so a server from before landing 1 refuses `id` and `kind`.
- `Workspace.ensure` refuses a launch that names an existing workspace with a different attachment, and a resume reuses the recorded one (`ride/ride/workspace/model.py`, `ride/ride/session.py`).
- `ride resume` runs the installation's current runtime unless the workspace's record names a runtime bundle (`recorded_runtime_reference` in `ride/ride/session.py`).
- `build_claude_launch` passes `--resume` in print mode too (`harnesses/claude/harness/bro/claude/claude_argv.py`).
- `bro chat --fork` accepts an initial message; `bro run` has no continuation, and `Runner.run()` always opens a fresh run where `Runner.send()` accepts a preseeded runner (`harnesses/native/harness/bro/native/runner.py`).
- A wire restores its state from `user_input` and `notification` bodies, `llm_call` responses and `tool_result` bodies with their call ids (`restore` in `wires/responses` and `wires/chat`),
  all of which a run log's step lines hold.
- A host command's credential picks come from `user.creds` and `user.tools.<command>.creds` (`bro/setup/AGENTS.md`).
- A row of a Claude trail holds its transcript line verbatim in `body`, and a native line row its step line (`harnesses/claude/trails/`, `harnesses/native/trails/`).
- The session lock admits one session per workspace (`bro/reference/ride.md`, "One session per workspace"), so one process writes the map at a time.
