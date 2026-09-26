---
name: ask
description:

This spell should be used when the user asks to relay a question or job to another bro
— "[[ask researcher to compare the storage options]]", "ask the reviewer whether the change is safe", "have deployer roll out the API", "summon developer"
— including asking for an interactive child the user will drive themselves ("summon a dev session for me", a manual summon).
Turns the phrasing into a summon (a scoped one-shot run that starts a party or joins the summoner’s),
picks whichever summon client the session has, decides foreground vs background,
and relays the answer with the failure modes handled.
A summon succeeds only when the target is in the summoner's `launch.bro.bros` set
— the session reads those members from the banner's `may_summon` row, fixed at launch
— so a denial stays a normal outcome the spell relays.

version: 1.21.0
---

# Ask

Relay a request to another bro via **summon**:
the target runs your prompt as a scoped one-shot in a started party of its own or as a member of this session’s party, and the selected surface carries its retained answer back.
A summon opens a **quest** and prints its id;
everything done to the quest afterwards
— reading its outcome or conversation, talking on it, ending it
— goes through the `quest` surfaces on that id.
You only formulate the request,
fire the client,
and relay the result
— all protocol,
authorization,
and spawning are host-side.

## Parse the phrasing

From the user's wording extract:

- **target** — the bro to summon (`reviewer`, `deployer`, …).
  The session's `launch.bro.bros` members are on its banner as `may_summon` (`bro::banner`)
  — read it rather than probing:
  a target it does not name is denied, and `none` means this session cannot summon at all.
  Being listed is not a promise the run succeeds;
  the failure modes below still apply.
  Where this session has a shell, read the target's card next:
  `bro show <name>` prints its description, its tools with their descriptions, its secrets and its spells
  — what it already knows how to do, in its own terms.
- **prompt** — the request, rewritten to be fully self-contained.
  The target shares no conversation history or environment with this session.
  A started party also shares no working tree;
  a joined member deliberately shares this session’s tree.
  Spell out concrete names,
  refs,
  and expectations ("list the deploy targets and their kinds", not "list them").
  Ask for what the user actually wants in the final answer;
  live messages handle progress and questions but do not replace that deliverable.
  Shape the request around the card:
  name the outcome and leave the mechanics to the target's own tools,
  which run against paths and setup you cannot see from here.
  Name a command yourself when running that exact command is the request
  — not as a guess at how the target would have reached the outcome anyway.

Optional knobs, normally only when the user asks for them:
a per-call timeout in seconds (default 1800 — sized for a deploy),
a base git ref for the child (default:
this workspace's current HEAD, so the target builds on the code as committed here
— uncommitted changes never transfer;
when the request turns on a specific commit,
branch or tag, pass it here rather than naming it in the prompt),
the child's hold — its user-involvement level (default unattended; the child runs isolated with no human channel, so raise it only when the user explicitly wants otherwise)
— the child's harness
— `claude` runs the target as a one-shot managed Claude Code session, `bro` as the target's own LLM process (default: the project's `summon-harness`)
— and the child's LLM recipe
— `provider:model:effort` with an optional `+fast` suffix and any field left empty, resolved within the child's harness, so `::high` keeps the base provider and model (default:
the target bro's own recipe on the bro harness,
Claude Code's own on claude).
Placement is a knob when the user asks for it:
`start` opens a workspace of the child’s own, with `boxed` or `unboxed` isolation;
`join` runs the child beside this session in the same workspace and isolation.
A join shares the working tree, so use it only when concurrent work in that tree is intended;
it refuses `into`, an isolation choice, and `manual`.
An unmarked request starts boxed when `:launch.bro.party.boxed` is held, then unboxed when `:launch.bro.party.unboxed` is the available member;
it never changes into a join.

The child's onward authority is a knob too:
grants and revokes.
A grant or revoke names one permission-document launch name:
`@bro`, `:launch.<type>`, `:launch.<type>.<set>.<member>`, or `:launch.<type>.<flag>`.
The bro placement members are `:launch.bro.party.boxed`, `:launch.bro.party.unboxed`, and `:launch.bro.party.join`.
They fold over the target's own seed and configuration rather than inheriting yours, and repeating either state is harmless.
You can grant only a name you hold under a type key you hold;
revokes are unbounded.
Grant only what the request actually needs and the user asked for, including the type key a field or flag needs,
such as `@reviewer` when a developer child must hand off a review or `:launch.webview` plus `:launch.webview.vnc` when it must open a visible browser.
The complete grammar, fold, and worker schemas are `bro/reference/ride.md`, "Session permissions and credentials".

A summon request carries no credentials.
The child resolves its credentials from its own bro, harness, model, and applicable host configuration, just like a root launch without credential flags.
A credential name in `grant` or `revoke` is refused;
configure it under `projects.<identity>.bros.<target>` in the host's `~/.bro.json` instead.

The quest's **talk** is a separate least-authority knob.
The child gets `worker.say` by default;
widen it only for conversation the request needs:

- `owner.say` lets this session steer the child with unsolicited messages;
- `owner.question` lets this session ask the child and await its reply;
- `worker.say` lets the child send progress before its final answer and is already in the default;
- `worker.question` lets the child stop for an answer from this session.

A reply follows the question right in the other direction, so do not add a say right merely to permit replies.
Grant `worker.question` when the work may need a decision, approval, or missing fact from the summoner rather than forcing the child to raise and lose its live state.
Grant summoner rights only when someone will keep the watch armed and act on those messages.
The Bash client takes repeatable or comma-separated `--talk <right>` values;
the tool client takes a `talk` list.

Exception — set the timeout unprompted when the child's run is open-ended:
a full-cycle dev child (a [[fix]] run through [[run pr]] and the review watch, or a [[run pr]] re-entry) idles for human review latency, so the default kills it mid-watch.
Size the timeout in hours (e.g. 28800), not minutes.

## Pick the client

The session's surface decides the client:

{{iff #harness = claude}}
**Managed Claude session:** prefer Bash.
Run `summon <target> '<prompt>'` (`--start` / `--join` / `--boxed` / `--unboxed`, `--timeout <s>`, `--into <ref>`, `--hold <level>`, `--grant <name>`, `--revoke <name>`, `--talk <right>`, `--llm <recipe>`, `--harness <name>`).
It prints the quest id and the started trail id to stderr,
then blocks until the answer or a child question lands on stdout.
A child question exits 4 and logs the exact `quest say` reply command;
other non-zero exits carry failures on stderr.
Everything after the summon is `quest <verb> <quest-id>`:
`check` for the outcome,
`history` for the conversation,
`say` and `ask` to talk,
`cancel` to end it
— `quest --help` lists them.
{{eliff #harness = bro}}
**Bro-native session:** use `bro::summon` to open the quest and the `bro::quest_*` tools on its id.
`bro::summon` returns after host acceptance with the quest id;
later chat and lifecycle transitions arrive through the `quest watch` job the session contract has armed.
Read the retained answer or failure with `bro::quest_check` when its terminal line arrives, and the conversation with `bro::quest_history`.
{{end}}

## Foreground vs background

A summon typically runs **minutes** (workspace launch + a full LLM run of the target).

{{iff #harness = claude}}
Run anything that isn't trivially quick in the background (claude's foreground Bash cap is ~10 min — shorter than the 1800s summon default, so a foreground wait can be killed mid-run while the child keeps going):
use the harness's background run (`run_in_background`),
keep working,
and collect the output when the completion notification arrives.
To peek mid-run, use `rewind show <trail-id>` with the trail id from the summon's stderr, or `quest check <quest-id>`
— non-blocking:
prints the answer if the result is already in,
says `still running` (exit 3) if not,
exits 4 with the questions the child is stalled on,
and never disturbs the backgrounded wait.
Alternatively `summon --detach` waits for host acceptance, prints the quest id, and exits;
wait for the retained result later with `quest check --wait <quest-id>`.

Every summon prints its quest id up front (stderr in blocking mode, stdout with `--detach`)
— note it.
Any summon is reclaimable by that id, foreground included:
if a waiting process is killed mid-flight, the host journal retains the quest,
`quest check <id>` polls it,
and `quest check --wait <id>` waits on the same non-destructive read.

When a blocking summon granted `worker.question` exits 4, stdout is the child's question and stderr names its quest id, question id, and ready reply command.
Answer it with `quest say <quest-id> '<answer>' --reply-to <question-id>`, then resume the same quest with `quest check --wait <quest-id>`.
That check may itself exit 4 with the open questions the child is stalled on;
repeat the answer/check loop until it exits 0 with the child's final answer or 1 with a failure.
Never restart the summon to answer it.
`quest history <quest-id>` shows the conversation so far, open questions marked `pending`.
`quest watch` arms at the current journal head and prints ordered transitions after it
— your summons', messages and denials included;
if retained events have a gap, it reports the loss and re-arms from the current head.
{{eliff #harness = bro}}
`bro::summon` is detached by construction:
note the accepted quest id and keep working while the quest watch carries its chat and lifecycle transitions.
When nothing else remains, call `bro::chill` and act on the next notification;
read the retained answer with `bro::quest_check` once the watch reports the terminal state.
If a child asks a question, answer it with `bro::quest_say(reply_to=…)`, then return to `bro::chill` rather than polling or restarting the summon.
To ask the child, call `bro::quest_ask`;
its reply arrives on the watch and remains readable with `bro::quest_history`.
`quest watch` arms at the current journal head and prints ordered transitions after it
— your summons', messages and denials included;
if retained events have a gap, it reports the loss and re-arms from the current head.
{{end}}

## Manual summon — a child the user launches

When the host cannot spawn the requested child,
or the request needs the user *in* the child session
— "summon a dev session for me to drive",
"open an interactive reviewer I can talk to",
or a job that plainly needs human judgment mid-run
— make it a **manual summon**:
nothing is spawned;
instead the host registers the expectation and hands back a token,
and the user launches the session themselves.

{{iff #harness = claude}}
**Managed Claude client:** run `summon --manual --detach <target> '<prompt>'`.
It waits for the host to accept,
then prints the token (the quest id) on stdout and logs the launch command to relay;
a denial fails right there, before any token exists.
`--into`,
`--grant`,
`--revoke`,
and `--talk` still apply;
`--timeout`,
`--hold`,
`--llm`,
`--harness`, and the placement flags (`--start` / `--join` / `--boxed` / `--unboxed`) are refused — the user’s launch owns those.
{{else}}
**Bro tool client:** call `bro::summon` with `manual=true` and any needed `talk` rights.
It returns the token and launch command once the host accepts;
a denial fails the call immediately.
{{end}}
The summoner still needs either `:launch.bro.party.boxed` or `:launch.bro.party.unboxed` because the human launch starts a party.

Relay the token to the user as the ready-to-paste interactive command
— `ride along --summoned <token> <target>`
— and note they may instead run `ride solo --summoned <token> <target>` for a one-shot request without an interactive terminal.
They may add their own launch flags (`--unboxed`, `--llm`, `--hold`, `--workspace`, `--cred`, credential `--grant`/`--revoke`, or a claude/bro harness).
The prompt you passed becomes the session's first message;
the child bases on this workspace's HEAD *at the moment they launch* (or the `--into` ref you gave).

Then wait like any detached summon.
{{iff #harness = bro}}
The quest watch carries the start, chat, and end;
call `bro::chill` when nothing else remains and read the retained answer with `bro::quest_check` after the terminal line.
{{eliff #harness = claude}}
`quest check <token>` polls (running until the user launches and the child answers),
while `quest watch` streams the start/end events.
There is no timer on a manual summon
— pace the polling to human time, and keep working meanwhile.
{{end}}
The answer arrives through the child's `answer` tool or as the printed reply from a clean one-shot run.
A child session the user quits without delivering surfaces as a failure;
that is an answerable outcome, not an error to retry.

## Relay the answer

The stdout / tool result is the target's answer.
Relay it to the user, attributed ("reviewer says: …"), trimmed of nothing substantive.
If the user asked for a follow-up action on the answer, continue with it.

## Failure modes

- **Denied** — the target isn't in the summoner's `launch.bro.bros` set,
  a scope override was malformed or grants authority beyond what the summoner holds,
  or the summon would nest past the depth cap.
  Immediate, no child spawned;
  the error names the reason.
  For the session itself the list is fixed at launch:
  the fix is relaunching `ride solo|along` (or `ask` / `call` / `dive-in`) with `--grant @<target>`
  — tell the user that;
  nothing in-session can widen it.
  A summoned bro's onward authority starts from its own seed and `may_summon` members under the project and host configuration layers, then the request's `@bro` and `:launch.…` overrides.
  Its credentials come from the target bro's applicable host configuration, not from the request.
  Change the relevant source rather than retrying unchanged.
- **Raised / error** — the target ran but couldn't fulfill the request;
  the reason is the failure text.
  Relay it — rephrasing the prompt or picking another target is a user decision.
- **Failed (launch / exit / timeout)**
  — the child never started,
  died,
  or was killed at the timeout.
  The message carries the reason and, once the child announced a trail, the `rewind show <trail-id>` that has the full trace;
  a child that died before recording says so, and the reason is all there is.
- **Interrupted wait / unavailable retained payload**
  — a killed or detached wait remains recoverable by quest id.
  {{iff #harness = bro}}Read it with `bro::quest_check` when the watch reports its terminal state.{{eliff #harness = claude}}`quest check <quest-id>` polls,
  and `quest check --wait <quest-id>` waits for the host-retained result.{{end}}
  If retention evicted the payload, the error points at the trail that still carries the run.

## Do not exit with a summon in flight

When the session's root process exits, in-flight summoned children are killed (an in-flight manual child is only detached — the user's session lives on, but its answer can no longer arrive).
When a summoned session exits with summons of its own in flight, those end `failed:orphaned` and their children are killed the same way.
{{iff #harness = bro}}
Before ending the session, return to `bro::chill` until every pending summon ends, then read each retained result with `bro::quest_check`.
A one-shot turn that ends with a summon in flight gets one notice naming it;
chill on it, or cancel what is no longer needed, since the next such turn end ends the run.
{{eliff #harness = claude}}
Before ending the session (or letting it end), wait for pending summons with `quest check --wait`.
A one-shot session holds while a background task runs and re-invokes you when one ends, so ending the turn with `watch-next` waiting is a wait;
once every summon has ended, stop the watch before the final turn ends.
{{end}}
If a result was lost this way it is still recoverable from the child's trail.

## Stopping one deliberately

Cancelling a child quest ends it `failed:cancelled`, whatever it summoned in turn ends `failed:orphaned`, a spawned child is killed, and a manual child is only detached
— its user-owned session lives on, no longer answering the quest.
{{iff #harness = bro}}
Call `bro::quest_cancel(quest_id)`;
it returns when the host accepts the cancellation, and the quest's terminal state arrives through the quest watch.
Read the retained end with `bro::quest_check`.
{{eliff #harness = claude}}
`quest cancel <quest-id>` returns once the quest has ended;
`--timeout <s>` bounds the wait and exits 3 when it passes first, with the end still on its way.
{{end}}
Only the session that summoned a quest can cancel it, so a grandchild is stopped by cancelling the child that summoned it.
Ending the ride's root session still stops every in-flight child at once.
A child that ran for a while has usually left durable state behind
— a trail, a retained failed workspace, a pushed branch, an open PR, a review watcher now dead, or task comments.
Reconcile that state *after* the cancel returns, not before:
a PR can appear during shutdown.
Record on the task what was left unattended.
