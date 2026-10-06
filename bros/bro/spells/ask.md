---
name: ask
description:

This spell should be used when the user asks to relay a question or job to another bro
— "[[ask researcher to compare the storage options]]", "ask the reviewer whether the change is safe", "have deployer roll out the API", "summon developer"
— including asking for an interactive child the user will drive themselves ("summon a dev session for me", a manual summon).
Turns the phrasing into a summon, picks the scope the request needs, and relays the retained answer with the failure modes handled.
A summon succeeds only when the target is in the summoner's `launch.bro.bros` set
— the session reads those members from the banner's `may_summon` row, fixed at launch
— so a denial stays a normal outcome the spell relays.

version: 1.24.0
---

# Ask

Relay a request to another bro through `bro::summon`.
The target runs the prompt as a scoped one-shot in a started party of its own or as a member of this session's party.
A summon opens a quest and returns its id after the host accepts it.
Everything done to the quest afterwards
— reading its outcome or conversation, talking on it, sharing an artifact, ending it
— goes through the `bro::quest_*` tools on that id.
The session watch carries later lifecycle and chat transitions as notifications.

## Parse the phrasing

From the user's wording extract:

- **target** — the bro to summon (`reviewer`, `deployer`, …).
  The session's `launch.bro.bros` members are on its banner as `may_summon` (`bro::banner`)
  — read it rather than probing:
  a target it does not name is denied, and `none` means this session cannot summon at all.
  Being listed is not a promise the run succeeds;
  the failure modes below still apply.
  Where this session has a shell, `bro show <name>` describes the target's tools, secrets, and spells.
- **prompt** — the request, rewritten to be fully self-contained.
  The target shares no conversation history with this session.
  A started party also shares no working tree;
  a joined member deliberately shares this session's tree.
  Spell out concrete names, refs, and expectations.
  Ask for what the user actually wants in the final answer;
  live messages handle progress and questions but do not replace that deliverable.
  Shape the request around the target's card and leave mechanics to its own tools.

Optional knobs normally follow only from the request:

- `timeout` bounds the run in seconds and defaults to 1800.
  Set it unprompted for an open-ended child, such as a full development cycle through review;
  size that in hours, not minutes.
- `into` bases a started child on a git ref rather than this workspace's current `HEAD`.
  Uncommitted changes never transfer.
- `hold` sets the child's user-involvement level and defaults to unattended.
- `harness` selects `bro` or `claude`, and `llm` selects the recipe within it.
- `party='start'` opens a workspace of the child's own;
  `party='join'` runs beside this session in the same workspace and isolation.
  A join is only for intended concurrent work in the same tree and refuses `into`, `isolation`, and `manual`.
- `isolation` selects `boxed` or `unboxed` for a started party.
  An unmarked request starts boxed when permitted, otherwise unboxed, and never becomes a join implicitly.
- `grant` and `revoke` shape the child's onward authority with `@bro` and `:launch.…` names.
  A grant must be authority this session holds;
  revokes are unbounded, and restating either state is harmless.
- `passes` gives the child a credential instance covered by this session's matching pass right.
  Without one, the child resolves credentials from its own bro, harness, model, and host configuration.
- `share` gives the child read access to an artifact ref this session can reach at launch.
  Use `bro::quest_share` for a ref minted later.

The complete permission grammar and fold are in `bro/reference/ride.md`, "Session permissions and credentials".

## Talk rights

A quest gets `worker.say` by default.
Widen it only for conversation the request needs:

- `owner.say` lets this session steer the child with unsolicited messages;
- `owner.question` lets this session ask the child and await its reply;
- `worker.say` lets the child send progress before its final answer;
- `worker.question` lets the child stop for an answer from this session.

A reply follows a question right in the other direction, so do not add a say right merely to permit replies.
Grant `worker.question` when the work may need a decision, approval, or missing fact from the summoner rather than forcing the child to raise and lose its live state.

## Open and follow the quest

Call `bro::summon` once and keep the accepted quest id.
The call is asynchronous by construction:
keep working while the session watch carries chat and lifecycle transitions.
Do not poll a running child and never summon it again to recover a result.
Whenever nothing else remains, end the turn;
the watch wakes the run on the next transition.

When a child asks a question, answer it with `bro::quest_say`, setting `reply_to` to the question id.
To ask the child, call `bro::quest_ask`.
Its reply arrives through the session watch and remains readable with `bro::quest_history`.
If the reply is itself a question, answer that question with `bro::quest_say` and its id.
Use `bro::quest_say` without `reply_to` to steer a child whose talk permits `owner.say`.
Use `bro::quest_share` to hand a live child an artifact created after launch.

On a terminal watch line, call `bro::quest_check` for the retained answer or failure.
The check is non-blocking and repeatable;
a `running` state immediately after the terminal-looking activity wants another watch transition or one later read, not a blocking call.
Use `bro::quest_history` to recover retained chat and open questions.

## Manual summon

When the host cannot spawn the requested child, or the user asks to drive the child themselves, call `bro::summon` with `manual=true`.
The host spawns nothing and returns a token plus a launch command after accepting the expectation.
`into`, `grant`, `revoke`, `passes`, and `talk` still apply;
`timeout`, `hold`, `llm`, `harness`, `party`, `isolation`, and `share` are refused because the user's launch owns them or no launched workspace exists yet.
The summoner still needs either `:launch.bro.party.boxed` or `:launch.bro.party.unboxed`.

Relay the ready-to-paste command `ride along --summoned <token> <target>`.
The user may instead choose `ride solo --summoned <token> <target>` for a one-shot request and may add their own launch flags.
Then follow the token as the quest id like any other accepted summon.
There is no timer on a manual summon;
wait at human pace through the session watch.
A child the user quits without delivering is a failure to relay, not a reason to summon it again.

## Relay the answer

Relay the target's retained answer to the user, attributed and trimmed of nothing substantive.
If the user asked for follow-up action on the answer, continue with it.

## Failure modes

- **Denied** — the target is outside this session's `launch.bro.bros`, the request is malformed, a grant exceeds this session's authority, a pass is unauthorized, or the summon exceeds the depth cap.
  The error names the reason and no child starts.
  A target missing from the fixed banner requires a relaunch with `--grant @<target>`;
  nothing in-session can widen it.
- **Raised or errored** — the target ran but could not fulfill the request.
  `bro::quest_check` raises with the retained reason.
  Relay it;
  choosing a different request or target is the user's decision.
- **Launch, exit, or timeout failure** — the child never started, died, or reached its run bound.
  The reason and any trail hint are the result to relay.
- **Interrupted observation** — the host journal still retains the quest by id.
  Recover its current state with `bro::quest_check` and its conversation with `bro::quest_history` rather than opening another quest.

## Do not exit with a summon in flight

A session's exit kills its host-supervised children and orphans their quests;
a manual child is detached, but its answer can no longer reach this summoner.
Before ending, keep ending turns to wait until every needed quest reaches a terminal watch line, then read each retained result with `bro::quest_check`.
Cancel work no longer needed instead of abandoning it.
If a result is lost through an exit, recover it from the child's trail where one exists.

## Stopping one deliberately

Call `bro::quest_cancel(quest_id)`.
It returns when the host accepts cancellation;
the terminal state arrives through the session watch and remains readable with `bro::quest_check`.
Only the session that summoned a quest can cancel it.
A spawned child is killed, a manual child is detached, and whatever the child launched ends `failed:orphaned`.
Reconcile durable state after the terminal arrives:
a PR or task update can appear during shutdown.
