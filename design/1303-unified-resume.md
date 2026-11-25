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

The work is built in three parts, in this order (§9):
landings 1 and 2 of this design, on the trail store as it is today;
then the trail store's own redesign and its move to PostgreSQL, a task of its own;
then trail resume, on the store that task leaves.

What it enables, for example:

- `ride resume` of a native session works with the record-only trails token every session holds by default, with `--no-trails`, and during a trails-server outage.
  Today it fails with `403 trails token 'sessions' may not read`, since the resume reads the trail back inside the session.
- A trail is one lifetime:
  its header's recipe, hold, `git` base, launch line, outcome and usage are that lifetime's,
  so the sum of a run's trails ([#1331](https://github.com/dzhioev/bro/issues/1331)) counts that run alone.
- A resumed session's model is told that it was resumed and what changed around it (§3.5),
  where today it goes on believing the session facts, watches and children in its history.
- No recorder asks the store to decide anything,
  so the store's redesign builds no lineage resolver, attach path or lineage index,
  and [#1288](https://github.com/dzhioev/bro/issues/1288) moves a writer that asks the store to decide nothing.
- Once trail resume is built, `ride along --from-trail <trail> …` continues a recorded conversation of either harness in a new workspace, on any host that reads the store.
  Today only a native trail can be continued that way, by `bro chat --fork` on the host.

Out of scope, each with where it belongs:

- the trail store's record scheme and its move from DynamoDB to PostgreSQL:
  a task of its own, which replaces the like-for-like move [#1174](https://github.com/dzhioev/bro/issues/1174) designed.
  This design only adds to today's store (§5.1), and hands that task what it leaves out (§5.2) and the cut-off its window is (§9);
- continuing a session or a workspace from before landing 2:
  none is migrated, recovered or kept working (§8);
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
  but the trails already stored are data:
  they stay readable, and landings 1 and 2 change none of them;
- existing sessions and workspaces are not data to keep:
  no earlier format of a session's own records is recognised or migrated (§8), as [#1338](https://github.com/dzhioev/bro/issues/1338) decided for workspace records,
  and no route is kept open for a runtime older than this design (§9);
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
- **Session part** of a system prompt — the text `bro.prompts.session_fragment` renders for a launch, which the prompt ends with:
  the contracts of a party member, of a run that may summon and of a summoned run, their turn-end contract, and the hold text.
  It describes the run itself.
  What precedes it is the **persona part**.
- **Launch facts** — what `do-ride` records about the launch of each lifetime (§3.5).
- **Resume notice**, or notice — the text a resumed lifetime's model is given ahead of its first input (§3.5).

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
| `bro` | the session has a continuable log, defined below | `cannot resume bro harness workspace '<name>': it holds no session run log`, or §8's refusal when its records predate format 2 |

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
The run log format becomes **format 2**, which differs from format 1 in its `open` entry and in one field of its first step;
every other entry and rule stays what `harnesses/native/harness/bro/native/run_log.py` writes and checks today:

- `open.format` is `2`;
- `open.forked_from`, present on a run that continues another, is `{"run": <run id>, "step": <step number>}`:
  the run log it continues, which lies in the same directory, and the last step of that log its conversation includes;
- `open.interactive` is `false` for a one-shot run, continued or not, and `true` for a conversation;
- the `system_prompt` step carries `session_at`, the character offset in its `body` at which the session part begins (§2):
  an integer from 0 to the body's length, the length itself naming a prompt without a session part.

A run therefore knows no trail id.
The reader reads format 2 alone and refuses any other, naming the file.
The format number belongs to the file and never leaves it:
what a trail row says about the step line it holds is the trail format's own (§4.5).

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
  The run keeps the continued log's system prompt, byte for byte, when that prompt's session part
  — its `body` from `session_at` on —
  equals the session part composed for this launch.
  Otherwise the whole prompt is composed again for the launch:
  the hold differs, the run's summoned state or talk rights differ, whether it may summon differs, or the session texts themselves changed.
  The persona part alone never decides:
  a run whose session part is unchanged keeps the persona part it started with, and learns of what changed around it from the resume notice (§3.5).
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
It hands the run the resume notice (§3.5) through the suppressed `--resume-notice TEXT`, which requires `--fork`.

`bro run --fork` is built with trail resume (§9):
`ride solo --from-trail` is the first launch that continues a record in one-shot mode, a `ride resume` being always interactive.

### 3.5 What a resumed lifetime is told

A resumed lifetime's model carries a history that states facts of an earlier lifetime:
a banner, watches it armed, children it summoned, spell and reference texts it loaded.
The lifetime therefore opens with a **resume notice**, on both harnesses,
which states what is certain, compares what `do-ride` can compare, and says "may" for the rest.

**Launch facts.**
At every start of a managed session `do-ride` writes `<session dir>/launch-facts.json`, replacing the earlier lifetime's:

```json
{
  "format": 1,
  "runtime": "b3886e68a777…",
  "llm": "openai:gpt-6.1-sol,effort=high",
  "hold": "attended",
  "summoned": false,
  "talk": null,
  "may_summon": ["@eyebro for bro{}"],
  "permits": ["bro for *", "bro.placement=boxed for *"],
  "may_give": [],
  "credentials": ["brog+github", "github+", "mu+", "trails+"]
}
```

| Fact | Its value |
|---|---|
| `runtime` | the hash of the frozen runtime bundle the lifetime runs, or null for a materialized bundle, which has none (`ride/ride/runtime_bundle.py`) |
| `llm` | the admitted recipe in the canonical text the banner prints |
| `hold` | the session's hold |
| `summoned`, `talk` | whether the run owes a summoner an answer, and its quest's talk rights, null when it has no quest |
| `may_summon`, `permits`, `may_give` | the rows of the session's initial authority, as the banner prints them; null without a broker channel |
| `credentials` | the stored name each held credential kind reads, sorted; names only, never material |

Equal `runtime` hashes mean the same installed code and the same shipped texts:
persona and policy text, spells, reference pages, tool descriptions, and the Claude Code pin.

**Building the notice.**
On `do-ride … --resume`, `do-ride` reads the earlier lifetime's launch facts before it replaces them and builds the notice:

1. It opens with what is true of every resume, and closes with the pointer to the banner:
   `this run continues an earlier one, in a new process`,
   `Jobs, watches and summoned children of the earlier run have ended`,
   `The working directory may have changed since`,
   and last `The mission and the trail are new: call bro::banner for them`.
   The working directory is never compared.
2. Each fact that differs from the earlier lifetime's gets a line under `Changed since then:`,
   and the facts that are equal are named in one sentence after it, `Unchanged: …`;
   when no fact differs, that sentence stands alone as `Unchanged since then: …`:

   | Fact | When it differs | When it is equal |
   |---|---|---|
   | `runtime` | `the runtime was updated: spell and reference texts you loaded earlier may differ; load one again before you rely on its wording` | `the runtime, so spell and reference texts are as you loaded them` |
   | `llm` | `llm: <earlier> → <current>` | `the recipe` |
   | `hold` | `hold: <earlier> → <current>` | not named |
   | `summoned`, `talk` | `summoned: <earlier> → <current>`, `talk: <earlier> → <current>` | not named |
   | `may_summon`, `permits`, `may_give` | one line per differing row, `<row>: <earlier> → <current>` | `what you may summon` |
   | `credentials` | `credentials: <earlier> → <current>` | `your credentials` |

   A null `runtime` on either side is a difference:
   a materialized bundle cannot be told from an updated one.
3. Without earlier launch facts
   — the first resume after landing 2, or an earlier lifetime whose record is of another `format` —
   nothing is compared, and one sentence takes the place of both lists:
   `The runtime, the recipe, your credentials and what you may summon may have changed too: spell and reference texts you loaded earlier may differ; load one again before you rely on its wording`.
   A record of this `format` that is not of this shape fails the launch, naming the file.

For example, a session resumed on the same runtime with nothing changed:

```text
[notice: this run continues an earlier one, in a new process.
Jobs, watches and summoned children of the earlier run have ended.
The working directory may have changed since.
Unchanged since then: the runtime, so spell and reference texts are as you loaded them; the recipe; your credentials; what you may summon.
The mission and the trail are new: call bro::banner for them.]
```

and one resumed after the installation was re-synced, with `ride resume --grant @eyebro`:

```text
[notice: this run continues an earlier one, in a new process.
Jobs, watches and summoned children of the earlier run have ended.
The working directory may have changed since.
Changed since then:
- the runtime was updated: spell and reference texts you loaded earlier may differ; load one again before you rely on its wording
- may_summon: none → @eyebro for bro{}
Unchanged: the recipe; your credentials.
The mission and the trail are new: call bro::banner for them.]
```

**Delivering the notice.**
`do-ride` hands the text to the harness's session runner (`SessionRun.resume_notice`, null on a start that resumes nothing), and each harness puts it ahead of the lifetime's first input:

| Harness | Carrier | In the record |
|---|---|---|
| `bro` | `--resume-notice` (§3.4): the run delivers the text as a user-role input ahead of its first exchange | a `notification` step |
| `claude` | a `SessionStart` hook matching `resume` in the session's settings, whose additional context is the text | an `attachment` line of type `hook_additional_context`, ahead of the next user line |

The notice starts no turn of its own:
on either harness the model reads it with the first input that follows.
Both records are among the first rows of the lifetime's trail, so a conversation view shows where a lifetime began.
An in-process `bro chat --fork` or `bro run --fork` gives no notice: it is no session.

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
7. A top-level `restoring` list is present only while a trail resume is writing its records (§6.4).
   A lifetime never starts with one: the launch settles it first.
8. A missing file is an empty map.
   A file that is not of this shape, or that carries `restoring` when a recorder reads it, ends the recording as a refusal (§4.8).

A native run log has one trail, whose one span starts at step 0.
A Claude transcript has one trail per stretch.

### 4.3 What the store holds

The map stores no progress.
How far a trail got is the count of its rows in the store the recorder writes to, its **extent**,
which the recorder reads with the record permission alone:
an append of no records at an offset is answered with the extent when the offset equals it, and refused with the actual extent otherwise;
a trail the store does not hold is answered as not found.
Every statement below is about that store, whichever store an earlier lifetime or a restore used.

- **Held.**
  An item at position p of a trail's spans is held when p is below that trail's extent.
- **Verified.**
  Before the recorder relies on a trail an earlier lifetime of the session wrote, to fork from it or to complete it,
  it sends the trail's last held item again as an append at the offset before the extent,
  and requires the store's duplicate answer, which the store gives only when the row it holds has that item's digest.
  A restored trail is another session's:
  the recorder never appends to it, and reads its extent alone.
- **Settling a record's entries.**
  Before the recorder relies on a record the map knows, it settles the record's last entry, and the one before it whenever it removes one, until an entry stands or none is left:
  the entry of a restored trail stands when the store holds that trail and its extent reaches the end of the entry's spans;
  when it does not, the session records to a store its history was not read from, and every entry of the record is removed;
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

- `P`'s entry is restored and stands (§4.3): nothing to do.
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

A step is recorded as the line row it is today, `{"run_log": 1, "body": <the step line>}`.
The tag is the `bro` trail format's version of a step line held in a row:
the format owns the constant, the recorder takes it from there instead of from the log's `open.format`, and it stays 1, since format 2 changes no field the trail format reads.
The trail format, which the trails-server runs on every append, refuses any other tag (§15).

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

A start request gains two optional members:

- `id` — the id to start the trail under.
  It must be a `lulid`.
  A trail started under a given id keeps `start_sha256` in its header:
  the SHA-256 of the canonical JSON (`model.canonical_json_bytes`) of the start request as the wire carries it, its `id` and its `body` included.
  Nothing rewrites that field, and an export or an import carries it like any other recorded field.
  The store then answers by the id alone:

  | The id | The store |
  |---|---|
  | is free | creates the trail |
  | holds a trail whose `start_sha256` is the request's digest, and which no import is writing | answers that trail, with its extent, and creates nothing |
  | holds anything else: a trail started by a different request, one the store named itself, one imported without the field, or one whose import is under way | refuses the request as a collision, with the answer an import under a taken id gets (`model.trail_collision_body`) |

  The digest, and not the header's current fields, decides:
  the row fold rewrites fields a start sets, such as a Claude trail's `native.harness_version` and any trail's `subject`.
  Without an `id`, the store mints one as today and keeps no digest.
  `NetworkStore` retries a start that names an id on its schedule for transient failures, and still sends one without an id once.
- `forked_from.kind` — one of `resume`, `trail` and `branch`.
  The store validates it, keeps it in the header and serves it.
  A `forked_from` without a kind stays valid: every trail stored before this design has none.

`lineage` stays a member:
a recorder older than landing 2 still sends it, and the store resolves it as today until the store's move (§5.2).

The answer is `{id, started_at, extent}`:
`201` for a trail the request created, `200` for one it found under its id.
A request that carries lineage evidence adds its verdict, as today.

### 5.2 What the store's move leaves out

Landings 1 and 2 leave the store its lineage machinery, since a recorder older than landing 2 still depends on it.
The task that redesigns the store and moves it to PostgreSQL builds none of it, and its cut-over ends its use (§9):

- the start request's `lineage` evidence and the verdict in its answer (`adopted`, `chunks`, `attached`, `forked_from`, `reason`);
- `TrailFormat.resolve_lineage`, `backends.resolve_lineage` and `harnesses/claude/trails/bro/claude_trails/lineage.py`;
- attach: `backends.attached_header`, `ATTACH_CONTENDED` and both stores' `_attach`;
- the lineage index: `find_segment_trails` and `holds_record` in both stores,
  DynamoDB's `segment-started_at-index` and `uuid-index` with the derived `segment` attribute,
  and the `trails_by_segment` and `steps_by_uuid` indexes with `segment_headers` and `holds_record` that #1174's design planned;
- the `lineage_head` fold and the verdict type (`LineageHead` and `LineageDecision` in `bro/trails/lineage.py`, `rows.inherited_native`, `replayed_native` and `minted_native`).

What that task carries over from this design is §5.1:
a start under a given id with its `start_sha256`, and the edge's `kind`.
`walk_chain` and `walk_header_chain` in `bro/trails/lineage.py` walk edges and resolve nothing; they stay.

### 5.3 Readers

Chain walks, the `forked_from` list selector and its index, and the refusal to delete a trail that has forks read the edge as today.
`rewind tree` shows each edge's kind beside its step, and the conversation view names the kind at each boundary between the trails of a chain;
an edge without a kind shows none.

## 6. Trail resume

This section is the agreed behaviour of trail resume.
It is built after the store's move (§9), on that store's record scheme:
§6.3 and §6.4 describe reading rows and writing records from them as today's rows are, and are revised against the new scheme before the build, with the point §6.7 leaves open.
Sections 3 to 5 already carry what a later trail resume needs of them:
the edge kind `trail`, restored entries in the map, and the `restoring` list.

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
  since the first trail it records forks from that trail.
  Two `trails` configurations name the same store when their backend and its location agree
  — the service's `base_url`, the local root —
  whatever token each carries.
  A later lifetime that records to another store after all is caught by the recorder (§4.3);
- the harness refuses the history (`SessionHarness.history_refusal`):
  the bro harness refuses a chain whose recorded recipes are not all on the launch recipe's wire, naming the recorded wire.

### 6.3 Reading the history

The history is the chain's trails, root first, each through its **bound**, the step its child forked at, and the named trail through `STEP`:
their rows as stored, with spilled bodies resolved, and for a native chain the tool lists its `llm_call` rows name by digest.
A trail still being recorded is read as far as the store holds it.
Core carries it to the harness as one value (`bro/trails/history.py`), read through `TrailsStore` alone.

### 6.4 Writing the records

Under the workspace's session lock and before `do-ride` is launched, the launcher has the harness write the history into the workspace's records (`SessionHarness.restore_history`).
A trail of the chain that contributes no row is skipped.

A restore is one unit, committed by the map:

1. The launcher replaces the map with one that adds the new entries and a `restoring` list,
   which names every file the restore will create, relative to the workspace's records.
2. Each file is written under a temporary name and renamed into place.
3. The launcher replaces the map again without `restoring`.
   The restore has happened once this replacement has.

A failure before step 3 removes the temporary files, the files already renamed and the new entries, and fails the launch.
A launcher that dies before step 3 leaves `restoring` behind.
Every launch that takes the session lock, a `ride resume` included, first settles it:
it removes the files the list names and the entries of their records, and the list,
before it asks whether the workspace holds a record to continue (§3.1).
So a restore that did not complete is never the record a later resume continues,
and the workspace's earlier newest record is again its newest.

For example, a restore of a chain of three native trails dies after renaming the first log:
the map lists three files under `restoring`, one of which exists.
The next `ride resume` removes that log and the three entries, and continues the log the workspace held before.

**Native.**
One run log per trail of the chain, root first, under `<session dir>/bro/runs/`, with fresh run ids, which therefore sort in chain order:

- `open` from the trail's header: `format` 2, `version`, `bro`, `recipe` from `native.llm`, `interactive`, `surface`, `hold`, the header's `started_at` as `at`,
  and, for every trail but the root, `forked_from` naming the run log written for its parent and the header's `forked_from.step_id`;
- one step per row through the trail's bound, so that step n of the log is step n of the trail:
  a line row's line verbatim, and a spread row's fields written as a step line with the row's `ts` as its `at`.
  A first step that carries no `session_at`, as every spread row's and a format-1 line's does, is given one equal to its body's length:
  such a prompt names no session part, so the run composes its own (§3.3);
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
The run decides its prompt by §3.3's rule like any continuation:
it keeps the recorded prompt when the launch's session part is the recorded one, and composes it for the launch otherwise.

The lifetime's resume notice (§3.5) compares nothing, since no earlier lifetime of this workspace recorded the history:
it is the notice of a lifetime without earlier launch facts, opening with `this run continues a conversation recorded by another session, in a new process` and naming that session's jobs, watches and children as not here.

Nothing else differs from a regular resume, and every later lifetime of the workspace is one.

### 6.6 Limits

- A trail resume needs the trail's harness: a Claude trail cannot continue under the bro harness, nor the reverse.
- What Claude Code keeps beside a transcript does not come back: subagent transcripts, the files holding oversized tool results, and file checkpoints.
- The tree is the launch's: the trail restores no file, and uncommitted work of the recorded session is in no trail.
- A native trail whose header lacks a fact its run log's `open` requires, such as `hold`, cannot be restored.

### 6.7 Open until trail resume is built

- What a Claude session's usage file counts after a trail resume.
  The usage publisher sums the session's whole newest transcript (`harnesses/claude/harness/bro/claude/usage_publisher.py`),
  which after a trail resume begins with the restored lines,
  so commit footers would credit the session with the recorded conversation's tokens;
  the native usage file counts per process and is unaffected.
  The choice is between starting the count after the lines restored trails hold, read from the map, and listing it in §10.

## 7. What changes for users

With landings 1 and 2:

| Situation | Today | After |
|---|---|---|
| `ride resume` of a native session with default credentials | fails: `403 trails token 'sessions' may not read` | resumes; no trail is read |
| `ride resume` of a native session that ran with `--no-trails` | refused: no trail pointer | resumes |
| `ride resume` of a native session while the store is down | fails | resumes, and its trails catch up once the store is back |
| what a resumed session's model knows of the resume | nothing: its history still states the earlier lifetime's facts | a resume notice ahead of its first input (§3.5) |
| a resumed native session's system prompt | the earlier run's, unless the hold differs | the earlier run's, unless its session part differs (§3.3) |
| a resumed Claude session's trail | the transcript's trail, reopened | a new trail, edge kind `resume` |
| a resumed native session's trail | a new trail forked from the pointer's trail, its history read from the store | a new trail, edge kind `resume`, forked from the trail of the run log it continues |
| steps the previous lifetime's recorder did not record | Claude: appended to the reopened trail; native: missing from the resumed conversation | Claude: the new trail's first rows; native: appended to the previous run log's trail |
| Claude's `/branch` | a fork the store's resolver decides | a fork of kind `branch` the recorder decides |
| returning to an earlier transcript with `/resume` inside one Claude lifetime | not recorded during that lifetime | recorded as a further trail |
| `bro chat <bro> --fork <trail>` | forks a trail read from the store | gone: `--fork` takes a run log |
| `bro chat <bro> --fork` with no value | the bro's newest trail recorded as a conversation | the bro's newest run log, whatever surface wrote it |
| continuing a conversation from its trail | native alone, in-process on the host | not possible until trail resume is built |
| `ride resume` of a native workspace from before landing 2 | resumes through its trail, given the read permission | refused (§8) |
| a start whose reply is lost | Claude converges on the trail; native strands an empty one | neither strands one |
| `rewind tree` | forks | forks with their kinds |
| the store operation that creates a trail | `blaze` | `start` |

With trail resume, after the store's move:

| Situation | Before it | After |
|---|---|---|
| continuing a conversation from its trail | not possible | `ride along` or `ride solo` with `--from-trail`, on both harnesses |
| a one-shot continuation | none | `ride solo --from-trail`, and `bro run --fork` in-process |

## 8. Existing workspaces

Existing sessions and workspaces are not kept working:
no code recognises or migrates an earlier format of a session's records, and no route is added to continue one.
The trails they recorded stay in the store, readable as before.

- **Native.**
  A workspace whose records predate format 2 (§3.1) is refused by `ride resume`:
  `cannot resume bro harness workspace '<name>': its session records predate run log format 2`.
  One that never wrote a run log is refused as holding none (§3.1).
  Format-1 logs stay on disk, unread.
  Until trail resume is built, nothing continues such a workspace's conversation:
  `bro chat --fork <trail>` is gone with landing 2 (§3.4).
- **Claude.**
  A workspace resumes as before.
  Its transcript is a record the map does not know, so the first lifetime that writes to it records it from its first line into a new root (§4.4).
  The lines earlier lifetimes recorded are then stored twice, in the old trail and in the new root, with no edge between them,
  and the new root's usage counts them again.
- The refusal `ride resume` gives for a session record that predates resolved recipes no longer names `bro chat <bro> --fork <trail>`:
  it says to start a new session.

## 9. Landings and rollout

The hosted trails-server answers every installation that records to it, and those move to a revision one at a time,
while a root keeps the runtime it froze for its whole lifetime.
So the store accepts what new recorders send before any sends it.

| Part | Carries | Its rollout |
|---|---|---|
| Landing 1 — the store accepts | the rename (§5.1) first, then `id` with `start_sha256`, and `forked_from.kind`, in both stores, the wire and the server, and `rewind`'s kinds (§5.3) | redeploy the trails-server |
| Landing 2 — local resume | run log format 2, `--fork` over run logs, the native session runner and `session_exists` (§3), the prompt rule and the resume notice (§3.3, §3.5), the map and both recorders (§4), §8's refusals | re-sync installations, once landing 1's server is deployed |
| The store's move — a task of its own | the store's new record scheme on PostgreSQL, without §5.2 | its own; its quiesced window is the cut-off below |
| Trail resume — after the store's move | `--from-trail` and `--at`, the history value, both harnesses' `restore_history`, `bro run --fork` (§3.4, §6) | re-sync installations |

Landing 2 merges to the default branch only after landing 1's server is deployed,
so no installation can run a recorder the deployed server refuses.

**Mixed versions, from landing 1 until the store's move.**

- Recorders of both generations write to one server.
  A recorder from before landing 2 sends neither `id` nor `kind`;
  an older Claude recorder still starts its trails with lineage evidence, and the server still resolves it.
  Landings 1 and 2 refuse nothing an older recorder sends.
- Older recorders do not drain on their own:
  a live session keeps the runtime its root froze, and `ride resume` re-enters the runtime bundle a workspace's record names (`ride/ride/cli.py`),
  so a workspace launched with `--runtime-bundle`, or a manual summon child pinned to its root's bundle, starts an older recorder at any later time.
- The store's move ends that.
  Its window is the cut-off:
  from then on the store serves recorders of landing 2 and later alone, and a runtime older than landing 2 is not supported for recording.
  How an older recorder is refused there, and how the operator confirms none matters, belongs to that task's design.
- A reader on a runtime older than landing 1 still shows a trail recorded after landing 2:
  its rows are what they were, and its display ignores an edge's `kind`.
  It cannot continue one in-process: its typed header reader refuses the `kind` (`trail_from_header` in `bro/trails/store.py`).
  Nothing is kept working for that.
- An installation that records to its local store has no server to deploy.
  Each session writes with the store code of its own runtime, so on one host sessions of both generations share the local root;
  the older code ignores the fields landing 1 adds, and both generations read and write the one root.

**Rollback.**

- Landing 1's server may roll back to the image before it only while no installation runs landing 2:
  an older server refuses `id` and `kind`.
  After that it stays.
- Landing 2 rolls back on an installation by re-syncing to the commit before it.
  That runtime resumes a native workspace as it did before this design:
  through the trail its pointer names, with the read permission that takes, and never from a run log.
  A workspace it continues holds a format-1 log again, which a later re-sync refuses (§8).
  A Claude workspace resumes under either runtime, and an older recorder attaches to the newest trail of its transcript, whichever generation wrote it (§15).

**What this hands to the store's move.**
[#1174](https://github.com/dzhioev/bro/issues/1174)'s design moves today's documents to PostgreSQL as they are.
The store's move replaces it with a move onto a new record scheme, and takes from this file:
§5.1's `id`, `start_sha256` and `kind`, which it carries over;
§5.2, which it leaves out;
the cut-off above, which its window is;
and §6.3 and §6.4, whose reading and writing of rows it changes.

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
- A continued native run keeps the persona part of the prompt it started with (§3.3):
  policy text a newer runtime ships reaches it only when its session part differs too.
  The notice tells it that the runtime was updated.
- The notice compares launch facts alone (§3.5).
  The working directory, whatever lies behind a tool, and a credential whose material changed under the same stored name are never compared,
  and the first resume after landing 2 compares nothing.
- The launch facts hold a lifetime's initial authority.
  A grant an operator made during the earlier lifetime with `ride override` is therefore named as a change at the next resume, though the model saw it happen.
- A run log forked in place inside a session's directory, with `bro chat --fork <a session's log>` from the host, becomes that session's newest record:
  the next `ride resume` continues it.

## 11. Implementation mapping

Landing 1:

- `bro/trails/model.py` — `StartRequest` with `id`, and `kind` in the `forked_from` pointer and in `ForkedFrom`.
- `bro/trails/store.py`, `local.py`, `server/dynamo.py`, `network.py`, `server/server.py`, `backends.py`, `importing.py`, `record/spine.py` — `start_trail`, the start under a given id with `start_sha256` and its collision, the kind kept and served.
- `bro/base/lulid.py` — the check that a string is a `lulid`.
- `bro/trails/display/`, `rewind.py` — the kind in the tree and at chain boundaries.
- every caller and helper that says `blaze`: both recorders, `benchmark/`, `oops/trails/server/`, the tests.

Landing 2:

- `bro/trails/record/` — `recording_map.py` (new) for the map, its shape and its atomic replacement, and the settling of §4.3 with the start under a minted id of §4.6, which both recorders share.
- `harnesses/native/trails/bro/native_trails/format.py` — the version of a step line held in a row, as the format's own constant.
- `harnesses/native/harness/bro/native/run_log.py` — format 2, `session_at` and the chain.
- `bro/bro.py`, `bro/prompts/` — the session part of a composed prompt, told apart from its persona part.
- `harnesses/native/harness/bro/fork.py`, `bro/launch/resume.py`, `bro/launch/call.py` — forking a run log, the prompt rule, the history from the chain, `--fork [RUN_LOG]`, `--resume-notice`, no `--continue-trail`.
- `bro/llm/tracker.py` — `start`'s `forked_from` is the run log's own `{run, step}`, and it takes the session part's offset.
- `harnesses/native/harness/bro/native/harness.py`, `recorder.py` — `session_exists`, `missing_session_error`, the runner's pick of the log to continue, the notice handed to the run, the pointer no longer restored, no warning.
- `harnesses/native/harness/bro/native/trail_recorder.py` — §4.5 and §4.6.
- `harnesses/claude/harness/bro/claude/trail_recorder.py` — §4.4 and §4.6 in place of the evidence it sends.
- `ride/ride/do_ride.py` — the launch facts and the notice (§3.5), and `SessionRun.resume_notice`.
- `harnesses/claude/harness/bro/claude/` — the `SessionStart` hook that carries the notice, in the session's settings.
- `ride/ride/session.py` — §8's refusal for a record that predates resolved recipes.

Trail resume, after the store's move:

- `ride/ride/cli.py`, `flags.py`, `session.py`, `do_ride.py` — the flags, the checks, the restoring step with its settling at every launch, `--resume` for a trail resume and its notice.
- `ride/ride/harness.py` — `history_refusal` and `restore_history` on `SessionHarness`.
- `bro/trails/history.py` (new) — the history value.
- `harnesses/native/harness/bro/native/` and `harnesses/claude/harness/bro/claude/` — each harness's two operations.
- `harnesses/native/harness/bro/launch/run.py`, `bro/native/runner.py` — `bro run --fork`.
- both recorders — restored entries (§4.3 to §4.5), which no lifetime meets before.

Documentation, with the landing that changes what it describes:
`bro/reference/ride.md` ("Lifecycle verbs", the mode verbs' flags, "Workspaces", "Summoning another bro", "The session executable", "Session recording"),
`bro/reference/permissions.md` (the recovery route a refused resume names),
`harnesses/native/harness/bro/native/reference.md` ("The session runner", "Recording and usage", "Resume", "In-process surfaces"),
`harnesses/claude/harness/bro/claude/reference.md` ("The session runner", "Claude state", "Session recording"),
the maps of the subsystems it touches
— `bro/trails/AGENTS.md`, `harnesses/claude/trails/AGENTS.md`, `harnesses/native/trails/AGENTS.md`, `harnesses/native/harness/AGENTS.md`, `harnesses/claude/harness/AGENTS.md`,
`bro/launch/AGENTS.md`, `bro/prompts/AGENTS.md`, `ride/AGENTS.md` and `bro/AGENTS.md` —
and `bro/reference/extending.md` for the harness seam.

## 12. Testing

- **Run logs.**
  Format 2's `open` and its first step's `session_at`; a format-1 log refused; the chain, with a missing origin and a step the origin lacks refused;
  the latest consistent step over a log killed mid-tool-call, and over a log holding its system prompt alone.
- **Continuing.**
  A conversation rebuilt from a chain of three logs with no store reachable;
  `bro chat --fork` over a path and with the value omitted;
  the new log created beside the continued one, and another directory refused;
  the wire mismatch refused;
  the prompt kept when the launch's session part is the recorded one, and composed again when the hold differs, when the run was summoned and no longer is, and when it may summon where it could not.
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
  a format-2 log's rows accepted by the installed `bro` trail format over a real store.
- **The store.**
  The contract suite, over `LocalStore` and over `NetworkStore` on a loopback server:
  a start under a given id, the same request again answered with the trail, also after rows were appended;
  a different request under that id, and a request under the id of a trail the store named, each a collision;
  `start_sha256` carried through an export and an import;
  the kind kept and served, an unknown kind refused.
- **The resume notice.**
  Each fact of §3.5 equal and different, a null `runtime`, no earlier launch facts, a record of another format, and a malformed one;
  on the bro harness the text ahead of the first exchange as a `notification` step that starts no turn;
  on the Claude harness the hook's line in the transcript ahead of the next user line.
- **End-to-end routes of landings 1 and 2**, each run through `ride` as an operator runs it:
  1. a native session is started and resumed against a store that refuses reads, with `--no-trails`, and with the store unreachable:
     the conversation continues from its run log, and where it records, the new trail's edge names the earlier run's trail with kind `resume`;
  2. a native session whose earlier recorder was stopped early is resumed: the earlier trail is completed before the new one forks from it;
  3. a resumed native session's first exchange carries the notice, and a resume with `--grant @<bro>` names that change in it;
  4. a Claude session is resumed: a new trail of kind `resume`, the notice in its transcript, and after `/branch` a trail of kind `branch`;
  5. a native workspace from before landing 2 is refused with §8's message.
- **Trail resume**, when it is built:
  each refusal of §6.2 before any workspace exists;
  a native chain of line rows and of spread rows restored and continued, in both modes;
  a Claude chain restored into a transcript whose lines are the rows' bodies in order;
  a restore that dies between two files, settled by the next launch;
  the first trail's edge and kind; a later lifetime that records to another store; the next `ride resume` regular.
- **Live probes against the pinned Claude Code**, with the repository's other `*_llm_test.py`:
  `--resume` appends to the transcript in the TUI and in print mode;
  `/clear`, `/branch` and `--fork-session` write the files §15 describes;
  a `SessionStart` hook matching `resume` puts its additional context into the transcript and the request, in the TUI and in print mode;
  with trail resume, a transcript written from rows resumes with its history, in another directory, in print mode.
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

The owner settled these in the design review, on 2026-11-24 and 2026-11-25.
Where one differs from the list above, it replaces it:

10. The order of the work (§9):
    landings 1 and 2 on today's store;
    then the store's redesign and its move to PostgreSQL, as a task of its own that builds no lineage machinery and whose window is the cut-off for older recorders;
    then trail resume, on the new scheme.
    This replaces the four landings of point 9, whose last had today's store shed lineage (§5.2).
11. Existing sessions and workspaces are throwaway, and the trails already stored are the data that matters:
    a native workspace from before landing 2 is refused plainly, and no recovery route or compatibility path is kept for an older session or runtime (§1, §8, §9).
12. A line row's tag is the trail format's own version of a step line and stays 1 (§4.5).
13. A continued native run keeps its recorded prompt unless the session part differs,
    and a resumed lifetime's model is given a resume notice on both harnesses (§3.3, §3.5).
14. The notice compares the runtime, the recipe, the hold, the summoned state, the authority and the credential names,
    and says of the working directory only that it may have changed (§3.5).

## 14. Details this file fixes beyond that proposal

Each follows from a decision above.
The owner approved these with the written file on 2026-11-24:

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
- the wording of the refusals (§3.1).

The review added these, which await the owner's approval of this revision:

- `session_at` on a run log's first step, as how a recorded prompt names its session part (§3.3);
- the launch facts file, its facts and the notice's wording (§3.5);
- the notice's carriers: `--resume-notice` on the bro harness and a `SessionStart` hook on Claude (§3.4, §3.5);
- `start_sha256` as what decides a repeated start, and a collision for every other trail under a named id (§5.1);
- a restored trail is checked against the recorder's own store before a trail forks from it (§4.3);
- a restore is committed by the map's `restoring` list, and settled by the next launch (§4.2, §6.4);
- two `trails` configurations name one store by backend and location (§6.2);
- `bro run --fork` is built with trail resume (§3.4);
- the wording of §8's refusals.

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
- The same release, probed in print mode on 2026-11-24 and 2026-11-25:
  `--resume` appends to the transcript, and `--fork-session` writes the file described above, its copy dropping the lines without a `uuid`;
  a transcript composed under a new session id, in another config directory and working directory,
  from one transcript's lines followed by the head lines and own lines of a `--fork-session` copy of it,
  resumes with the whole history and is appended to in place, its new lines carrying the new session id;
  a history cut after an unanswered tool call, or after a prompt with no reply, is repaired by Claude Code when it resumes;
  a `SessionStart` hook matching `resume` that prints `hookSpecificOutput.additionalContext` has that text written as an `attachment` line of type `hook_additional_context` ahead of the next user line, and sent in the request.
  The TUI was not probed on those dates.
- An append of no records is answered with the extent at a matching offset and refused with the actual extent otherwise,
  a batch sent again over rows with its digests is answered as a duplicate,
  and an append to a trail the store does not hold is answered as not found
  (`LocalStore.append_records`, `DynamoStore.append_records`, and `_handle_append_records` in `bro/trails/server/server.py`, which admits an empty list).
- An append does not look at a trail's `end`, and `end_trail` replaces it (`bro/trails/local.py`, `bro/trails/server/dynamo.py`).
- A reader refuses a chain whose fork bound lies past the steps its parent holds (`RecordedAdapter._segment_messages` in `bro/trails/display/recorded.py`),
  which is why §4.5 completes a trail before forking from it.
- The bro trail format requires `native.llm` alone of a start (`_bro_validate_create`),
  and a start checks only the shape of `forked_from` (`bro/trails/model.py`).
- The `bro` trail format refuses a line row whose `run_log` is not 1 (`_step_line` in `harnesses/native/trails/bro/native_trails/format.py`),
  reads a step line's `kind`, `at`, `body` and, for an `llm_call`, `tools_sha256`, and passes its other fields through;
  the native recorder tags a row with its log's `open.format` (`harnesses/native/harness/bro/native/trail_recorder.py`).
- The start is the one store write `NetworkStore` does not retry (`bro/trails/network.py`).
- A start and an import refuse unknown fields (`BlazeRequest.from_wire`, `_validate_pointer`),
  so a server from before landing 1 refuses `id` and `kind`;
  `trail_from_header` builds `ForkedFrom` from the stored pointer's keys, so a reader from before landing 1 refuses a `kind` there,
  while the recorded display reads the pointer's `trail_id` and `step_id` alone (`bro/trails/store.py`, `bro/trails/display/recorded.py`).
- The row fold rewrites header fields a start sets:
  a Claude record's `version` replaces `native.harness_version`, and a subject is folded or set later (`_claude_classify`, `rows.AggregateState`, `set_subject`).
- An older Claude recorder's lineage evidence is resolved against every trail of its segment, newest first, by the head the store folds from that trail's rows (`claude_trails/lineage.py`);
  landings 1 and 2 keep that fold, so a trail a newer recorder wrote is attached to like any other.
  This was read, not run.
- `Workspace.ensure` refuses a launch that names an existing workspace with a different attachment, and a resume reuses the recorded one (`ride/ride/workspace/model.py`, `ride/ride/session.py`).
- `ride resume` runs the installation's current runtime unless the workspace's record names a runtime bundle, which it then re-enters (`recorded_runtime_reference` in `ride/ride/session.py`, `ride/ride/cli.py`);
  a frozen bundle's identity is the hash of its manifest, and a materialized one has none (`ride/ride/runtime_bundle.py`).
- `build_claude_launch` passes `--resume` in print mode too (`harnesses/claude/harness/bro/claude/claude_argv.py`).
- `bro chat --fork` accepts an initial message; `bro run` has no continuation, and `Runner.run()` always opens a fresh run where `Runner.send()` accepts a preseeded runner (`harnesses/native/harness/bro/native/runner.py`).
- A wire restores its state from `user_input` and `notification` bodies, `llm_call` responses and `tool_result` bodies with their call ids (`restore` in `wires/responses` and `wires/chat`),
  all of which a run log's step lines hold.
- A native system prompt is the composed persona text followed by the session fragment (`BaseBro.system_prompt_for`),
  and the fragment is composed from the hold, the summoned state and talk rights, whether the run may summon, and its credentials (`bro.prompts.session_fragment`).
- A session's initial authority is kept in its session directory (`initial-authority.json`, `bro/authority_view.py`),
  the banner's facts are collected in one place (`SessionFacts` in `bro/workspace/banner.py`),
  and `do-ride` knows the kinds a session holds (`BRO_INSTALL_KINDS`).
- A host command's credential picks come from `user.creds` and `user.tools.<command>.creds` (`bro/setup/AGENTS.md`).
- A row of a Claude trail holds its transcript line verbatim in `body`, and a native line row its step line (`harnesses/claude/trails/`, `harnesses/native/trails/`).
- The session lock admits one session per workspace (`bro/reference/ride.md`, "One session per workspace"), so one process writes the map at a time.
