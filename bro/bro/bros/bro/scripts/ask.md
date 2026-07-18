---
name: ask
description: This skill should be used when the user asks to relay a question or job to another bro — "/ask librorian to add Dune to the library", "ask the pm bro whether the inbox holds anything urgent", "have devoops deploy flow-mcp", "summon ppp-dev". Turns the phrasing into a summon (an isolated one-shot run of the target bro with its own credentials), picks whichever summon client the session has, decides foreground vs background, and relays the answer with the failure modes handled. A summon succeeds only when the target is in the session's allow-list — most bros seed none, and grants come from the launch surface — so a denial is a normal outcome the skill relays.
version: 1.0.0
---

# Ask

Relay a request to another bro via **summon**: the target runs your prompt as a one-shot in its own isolated container with its own credentials, and the answer comes back synchronously. You only formulate the request, fire the client, and relay the result — all protocol, authorization, and spawning are host-side.

## Parse the phrasing

From the user's wording extract:

- **target** — the bro to summon (`devoops`, `pm`, …). The session has a summon allow-list; don't second-guess it, just try — a disallowed target fails immediately with a clear reason.
- **prompt** — the request, rewritten to be fully self-contained. The target shares no context with this session: no conversation history, no working tree, no environment. Spell out concrete names, refs, and expectations ("list the deploy targets and their kinds", not "list them"). Ask for what the user actually wants back — the reply is the only thing that returns.

Optional knobs, normally only when the user asks for them: a per-call timeout in seconds (default 1800 — sized for a deploy), a base git ref for the child (default: this workspace's current HEAD, so the target builds on the code as committed here — uncommitted changes never transfer; deploys included, so pass a ref explicitly when the child must build something other than what you have checked out), and the child's hold — its user-involvement level (default unattended; the child runs isolated with no human channel, so raise it only when the user explicitly wants otherwise).

Exception — set the timeout unprompted when the child's run is open-ended: a full-cycle dev child (a `/fix` that runs through `/pr` and the review watch, or a `/pr` re-entry) idles for human review latency, so the default kills it mid-watch. Size the timeout in hours (e.g. 28800), not minutes.

## Pick the client

Use whichever the session has — they speak the same mechanism:

- **Bash available** (a cw-session): `bro run --summon <target> '<prompt>'` (`--timeout <s>`, `--into <ref>`, `--hold <level>`); bare `summon <target> '<prompt>'` is the thin alias. It prints the request id and the started trail id to stderr, then blocks until the answer lands on stdout; non-zero exit + stderr on failure.
- **No Bash, the `summon` tools present** (`bro::summon` / `bro::summon_check` — `mcp__bro__summon` / `mcp__bro__summon_check` in a `--bro` claude session): call `summon` with `target` and `prompt` (optional `timeout`, `into`, `hold`). It blocks and returns the answer; failures come back as the tool error with the reason. `detach: true` returns the request id right away instead; `summon_check(request_id)` peeks non-blockingly (`{state: pending|completed, …}`) and `summon_check(request_id, wait: true)` blocks and collects.
- **Neither** — this session can't summon; say so instead of improvising.

## Foreground vs background

A summon typically runs **minutes** (container launch + a full LLM run of the target).

With Bash, run anything that isn't trivially quick in the background (claude's foreground Bash cap is ~10 min — shorter than the 1800s summon default, so a foreground wait can be killed mid-run while the child keeps going): use the harness's background run (`run_in_background`), keep working, and collect the output when the completion notification arrives. To peek mid-run, use `trails show <trail-id>` with the trail id from the summon's stderr, or `summon check <request-id>` — non-blocking: prints the answer if the result is already in, says `still running` (exit 3) if not, and never disturbs the backgrounded wait. Alternatively `summon --detach` prints the request id and exits; collect later with `summon check --wait <request-id>`.

Every summon prints its request id up front (stderr in blocking mode, stdout with `--detach`) — note it. Any summon is reclaimable by that id, foreground included: if a waiting process is killed mid-flight, the result is buffered, `summon check <id>` polls for it, and `summon check --wait <id>` collects it.

Without Bash there is no true backgrounding, but the tools cover the long-run case: a blocking `summon` call fits anything conversational (tell the user it may take minutes); for a run that would outlast the surface's tool-call patience, `summon(…, detach: true)` returns the request id, and you check on it with `summon_check` between turns — non-consuming, so polling is safe — collecting with `wait: true` when it reports completed.

## Relay the answer

The stdout / tool result is the target's terminal reply. Relay it to the user, attributed ("devoops says: …"), trimmed of nothing substantive. If the user asked for a follow-up action on the answer, continue with it.

## Failure modes

- **Denied** — the target isn't in the summoner's allow-list, or the summon would nest past the depth cap. Immediate, no child spawned; the error names the reason. For the session itself the list is fixed at launch: the fix is relaunching with `--grant @<target>` (on `cw ss` / `dive-in` / `bro run` / `bro chat` or their aliases) — tell the user that; nothing in-session can widen it. A summoned bro follows its own static `may_summon` seeds instead — grants don't reach it, so its denials are fixed by seeding the bro in code.
- **Raised / error** — the target ran but couldn't fulfill the request; the reason is the failure text. Relay it — rephrasing the prompt or picking another target is a user decision.
- **Failed (launch / exit / timeout)** — the child never started, died, or was killed at the timeout. The message carries the reason and a trails hint; `trails show <trail-id>` has the full trace.
- **Wait expired with no terminal** — the result was lost or the child is still running; the error says which trail to inspect. A killed or detached wait is recoverable: `summon check <request-id>` polls, `summon check --wait <request-id>` collects the buffered result (the `summon_check` tool does the same for tool-only sessions).

## Do not exit with a summon in flight

When the session's root process exits, in-flight summoned children are killed. Before ending the session (or letting it end), wait for pending summons or collect them with `summon check --wait`; if a result was lost this way it is still recoverable from the child's trail.
