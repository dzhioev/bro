---
name: ask
description:

This spell should be used when the user asks to relay a question or job to another bro
— "[[ask researcher to compare the storage options]]", "ask the reviewer whether the change is safe", "have deployer roll out the API", "summon developer"
— including asking for an interactive child the user will drive themselves ("summon a dev session for me", a manual summon).
Turns the phrasing into a summon (an isolated one-shot run of the target bro with its own credentials),
picks whichever summon client the session has, decides foreground vs background,
and relays the answer with the failure modes handled.
A summon succeeds only when the target is in the summoner's allow-list
— the session reads its own off the banner, fixed at launch
— so a denial stays a normal outcome the spell relays.

version: 1.11.0
---

# Ask

Relay a request to another bro via **summon**:
the target runs your prompt as a one-shot in its own isolated container with its own credentials, and the answer comes back synchronously.
You only formulate the request,
fire the client,
and relay the result
— all protocol,
authorization,
and spawning are host-side.

## Parse the phrasing

From the user's wording extract:

- **target** — the bro to summon (`reviewer`, `deployer`, …).
  The session's allow-list is on its banner as `may_summon` (`bro::banner`)
  — read it rather than probing:
  a target it does not name is denied, and `none` means this session cannot summon at all.
  Being listed is not a promise the run succeeds;
  the failure modes below still apply.
- **prompt** — the request, rewritten to be fully self-contained.
  The target shares no context with this session:
  no conversation history,
  no working tree,
  no environment.
  Spell out concrete names,
  refs,
  and expectations ("list the deploy targets and their kinds", not "list them").
  Ask for what the user actually wants back
  — the reply is the only thing that returns.

Optional knobs, normally only when the user asks for them:
a per-call timeout in seconds (default 1800 — sized for a deploy),
a base git ref for the child (default:
this workspace's current HEAD, so the target builds on the code as committed here
— uncommitted changes never transfer;
when the request turns on a specific commit,
branch or tag, pass it here rather than naming it in the prompt),
the child's hold — its user-involvement level (default unattended; the child runs isolated with no human channel, so raise it only when the user explicitly wants otherwise)
— the child's harness
— `claude` runs the target as a one-shot managed Claude Code session (default `bro`: the target's own LLM process)
— and the child's LLM recipe
— `provider:model:effort` with an optional `+fast` suffix and any field left empty, resolved within the child's harness, so `::high` keeps the base provider and model (default:
the target bro's own recipe on the bro harness,
Claude Code's own on claude).

The child's scope is a knob too:
grants and revokes,
each value a credential name or `@bro` for a summon target of the target's own.
They start from the target's own declarations, not yours
— nothing of your scope reaches the child unless you name it, and you can only name what you hold yourself:
a credential in your own scope,
a bro in your own allow-list.
Grant only what the request actually needs and the user asked for:
a credential the target's manifest lacks (`staging_api` for an integration run),
a different instance of a selected credential kind,
or a bro the target has to reach onward (`@reviewer` so a developer child can hand off a review).
A credential grant replaces the target's selected same-kind name.
Both directions are strict, so naming the exact credential or bro the target already has (or, for a revoke, lacks) fails the summon rather than passing quietly.

The harness and LLM knobs above answer to that same bound, since the driving loop they pick brings credentials of its own:
what the pair adds on top of the target's default scope has to be in your scope too, so a session running under the bro harness cannot ask for a `claude` child unless its own launch hydrated the Claude OAuth token.
Relay that denial like any other
— the fix is on the user's launch line, not in the request.

Exception — set the timeout unprompted when the child's run is open-ended:
a full-cycle dev child (a [[fix]] run through [[run pr]] and the review watch, or a [[run pr]] re-entry) idles for human review latency, so the default kills it mid-watch.
Size the timeout in hours (e.g. 28800), not minutes.

## Pick the client

Prefer Bash where the session has it:
its background run ends in a harness completion notification that wakes you, while a detached tool summon has no wake-up at all and leaves the session dark until someone prompts it.
The mechanism is the same either way:

- **Bash available** (a managed Claude session):
  `summon <target> '<prompt>'` (`--timeout <s>`, `--into <ref>`, `--hold <level>`, `--grant <name>`, `--revoke <name>`, `--llm <recipe>`, `--harness <name>`).
  It prints the request id and the started trail id to stderr,
  then blocks until the answer lands on stdout;
  non-zero exit + stderr on failure.
- **No Bash, the `summon` tools present** (`bro::summon` / `bro::summon_check` — the `--raw` claude session case):
  call `summon` with `target` and `prompt` (optional `timeout`, `into`, `hold`, `grant`, `revoke`, `llm`, `harness`).
  It blocks and returns the answer;
  failures come back as the tool error with the reason.
  `detach: true` returns the request id right away instead;
  `summon_check(request_id)` peeks non-blockingly (`{state: pending|completed, …}`) and `summon_check(request_id, wait: true)` blocks and collects.
- **Neither** — this session can't summon;
  say so instead of improvising.

## Foreground vs background

A summon typically runs **minutes** (container launch + a full LLM run of the target).

With Bash, run anything that isn't trivially quick in the background (claude's foreground Bash cap is ~10 min — shorter than the 1800s summon default, so a foreground wait can be killed mid-run while the child keeps going):
use the harness's background run (`run_in_background`),
keep working,
and collect the output when the completion notification arrives.
To peek mid-run, use `rewind show <trail-id>` with the trail id from the summon's stderr, or `summon check <request-id>`
— non-blocking:
prints the answer if the result is already in,
says `still running` (exit 3) if not,
and never disturbs the backgrounded wait.
Alternatively `summon --detach` prints the request id and exits;
collect later with `summon check --wait <request-id>`.

Every summon prints its request id up front (stderr in blocking mode, stdout with `--detach`)
— note it.
Any summon is reclaimable by that id, foreground included:
if a waiting process is killed mid-flight, the result is buffered,
`summon check <id>` polls for it,
and `summon check --wait <id>` collects it.

Without Bash there is no true backgrounding, but the tools cover the long-run case:
a blocking `summon` call fits anything conversational (tell the user it may take minutes);
for a run that would outlast the surface's tool-call patience, `summon(…, detach: true)` returns the request id,
and you check on it with `summon_check` between turns
— non-consuming, so polling is safe
— collecting with `wait: true` when it reports completed.

## Manual summon — an interactive child the user drives

When the request needs the user *in* the child session
— "summon a dev session for me to drive",
"open an interactive reviewer I can talk to",
or a job that plainly needs human judgment mid-run
— make it a **manual summon**:
nothing is spawned;
instead the host registers the expectation and hands back a token,
and the user launches the session themselves.

- **Bash client**:
  `summon --manual --detach <target> '<prompt>'`
  — waits for the host to accept,
  then prints the token (the request id) on stdout and logs the launch command to relay;
  a denial fails right there, before any token exists.
  `--into`,
  `--grant`,
  `--revoke` still apply;
  `--timeout`,
  `--hold`,
  `--llm`,
  `--harness` are refused — the user's launch owns those.
- **Tool client**:
  `summon` with `manual: true`
  — returns the token and the launch command once the host accepts;
  a denial fails the call immediately.

Relay the token to the user as the ready-to-paste command
— `ride along --summoned <token> <target>`
— and note they may add their own launch flags (`--host`, `--llm`, `--hold`, `--workspace`, a claude/bro harness).
The prompt you passed becomes the session's first message;
the child bases on this workspace's HEAD *at the moment they launch* (or the `--into` ref you gave).

Then wait like any detached summon:
`summon check <token>` polls (pending until the user launches and the child announces itself),
`summon watch` streams the start/end events where mounted,
and the answer arrives when the child session delivers it via its `answer` tool.
There is no timer on a manual summon
— pace the polling to human time, and keep working meanwhile.
A child session the user quits without delivering surfaces as a failure;
that is an answerable outcome, not an error to retry.

## Relay the answer

The stdout / tool result is the target's terminal reply.
Relay it to the user, attributed ("reviewer says: …"), trimmed of nothing substantive.
If the user asked for a follow-up action on the answer, continue with it.

## Failure modes

- **Denied** — the target isn't in the summoner's allow-list,
  a scope override was malformed,
  a no-op,
  or beyond what the summoner holds,
  or the summon would nest past the depth cap.
  Immediate, no child spawned;
  the error names the reason.
  For the session itself the list is fixed at launch:
  the fix is relaunching `ride solo|along` (or `ask` / `call` / `dive-in`) with `--grant @<target>`
  — tell the user that;
  nothing in-session can widen it.
  A summoned bro starts from its own static `may_summon` seeds, so its onward denials are fixed at the summon that spawned it (grant `@<name>` there) or by seeding the bro in code.
- **Raised / error** — the target ran but couldn't fulfill the request;
  the reason is the failure text.
  Relay it — rephrasing the prompt or picking another target is a user decision.
- **Failed (launch / exit / timeout)**
  — the child never started,
  died,
  or was killed at the timeout.
  The message carries the reason and a trails hint;
  `rewind show <trail-id>` has the full trace.
- **Wait expired with no terminal**
  — the result was lost or the child is still running;
  the error says which trail to inspect.
  A killed or detached wait is recoverable:
  `summon check <request-id>` polls,
  `summon check --wait <request-id>` collects the buffered result (the `summon_check` tool does the same for tool-only sessions).

## Do not exit with a summon in flight

When the session's root process exits, in-flight summoned children are killed (an in-flight manual child is only detached — the user's session lives on, but its answer can no longer arrive).
Before ending the session (or letting it end), wait for pending summons or collect them with `summon check --wait`;
if a result was lost this way it is still recoverable from the child's trail.

## Stopping one deliberately

The protocol has no cancel:
stopping a summon means stopping the child's container.
A child that ran for a while has usually left state outside itself
— a pushed branch,
an open PR,
a review watcher now dead,
task comments.
Reconcile that state *after* the stop, not before:
a PR can appear in the seconds before the container dies.
Record on the task what was left unattended.
