# Design review: a second model provider, and providers, wires and harnesses as plug-ins

This file is a design review only and is never merged.
It carries the design of [#1091](https://github.com/dzhioev/bro/issues/1091) so that reviewers can comment on it in line.
When the review ends, this pull request closes without merging.

Review it as a document: gaps, contradictions, unverified assumptions, a missing rollout order, and better alternatives; not as code, tests or CI.
The specification comes first.
The review record after it lists what changed against the version approved on 2026-10-26 and is not part of the specification.

## Design

### 1. Outcome, scope, constraints

Outcome: a coordinator can run a bro on DeepSeek for a stage by naming a recipe (`llm: deepseek:pro:high`), `bro-dev` first;
DeepSeek completes work of the kind the default providers' safety systems stopped (defensive test-input generation, [#1086](https://github.com/dzhioev/bro/issues/1086)) with model-written code;
no framework code names a provider, a wire or a harness;
and a reference page records how the next provider or model is adopted.

Settled with the user, in the order the discussion reached them:

1. The first additional provider is DeepSeek's own API: default model `deepseek-v4-pro`, short names `pro` and `flash`, selected by an explicit recipe, with no preset alias, no new bro and no persona variant.
2. DeepSeek runs on its Chat Completions wire.
   The native loop becomes one provider-neutral loop over a wire port; records, trail projection and fork replay are per wire.
3. Providers, wires and harnesses are plug-ins found through entry points, in the manner of file types.
   Framework code selects them by injection and names none of them.
   OpenAI and Anthropic are plug-ins like any other.
4. A wire is one distribution holding its decode side and its client side.
   The port is in core.
   Anthropic's vocabulary is a decode-only wire.
5. Plug-in kinds live in the top-level directories `llms/`, `wires/` and `harnesses/`.
   A harness and its trail format are two distributions in the harness's directory, `harnesses/<name>/harness/` and `harnesses/<name>/trails/`, because a uv workspace member cannot sit inside another member's directory (D28).
   `filetypes/` stays as it is for now.
6. There is no default harness and no default recipe.
7. Auxiliary one-shot calls take their LLM from a `mu` credential, and a caller picks a role within it.
8. The Claude-only build (core and `bro-ride` without the native harness) is not a requirement.
9. The reference page is `bro/reference/llm_providers.md`.
10. There is no early canary: DeepSeek is added regardless of a pre-check, and its capability for #1086 is checked at V (§8), after implementation.
11. Work lands in two landings, layout and decode first and behavior after, with the hosted trails server redeployed between them (§11).
12. A provider's capacity stop is retried by the wire: a first wait of 10 s, doubling to 300 s, six retries, and then the run ends (U6).
13. Installations are replaced, not mixed: an older installation, frozen runtime or client configuration may break when a newer one replaces it.

Settled: every decision of §12 is `ratified` (ruled on by the user),
`approved` (proposed by the design and approved with it by the user on 2026-10-26) or `proposed` (raised by the review of this pull request and settled when it is approved), except D35, which is `open`.

Out of scope: changing the safety behavior of the existing providers;
retrying #1086 on a provider known to block it;
applying to a vendor access program (OpenAI Daybreak, Anthropic Cyber Verification Program), which stays open to the user in parallel.

Deferred, each with its trigger: continuing a conversation on a different wire (F2), when someone needs to move a blocked conversation across providers;
a second host for the same model family, covered by the reference page's procedure;
streaming, if single calls outlive their timeout (X4);
client-side context compaction, if a context nears the provider's window (X5);
a header `format` key separate from `harness`, when a harness writes an envelope another harness already writes (a session driven by `pi`, say);
the uniform `filetypes/<type>` layout, as a later cleanup;
a shared entry-point registry helper, since the pattern already exists in about a dozen near-identical copies and the rosters of §4.7 add three more.

Constraints: core (`bro`) imports no other workspace member, and declaration and decode modules import no provider SDK at module level (`bro/llm/AGENTS.md`);
backward compatibility is not a constraint (item 13) except that recorded trails stay readable (R2) and that the hosted trails server serves a wire before any record of it is written (§11);
each rule below is stated once and referenced by its id.

### 2. Evidence and what stays unverified

Verified on 2026-10-26.
`api.deepseek.com` is reachable from a boxed session (401 without a key).
DeepSeek's [docs](https://api-docs.deepseek.com/quick_start/pricing/) list `deepseek-v4-pro` (V4-Pro-0813) and `deepseek-flash` (V4.1-Flash-1017, multimodal), 1M context, function calling,
thinking on by default with `reasoning_effort` `low`/`high`/`max` (`none` turns it off), bearer-key auth, per-account concurrency limits answered with 429, and automatic prefix caching.
The legacy ids `deepseek-chat` and `deepseek-reasoner` were scheduled for retirement on 2026-07-24 and are no longer listed.
In thinking mode with `tools`, every assistant message's `reasoning_content` must be passed back in every later request or the API answers 400.
A response's `finish_reason` is one of `stop`, `length`, `content_filter`, `tool_calls`, `insufficient_system_resource`, `aborted`, and tool-call `arguments` "may be invalid".
`usage` carries `prompt_tokens` (= `prompt_cache_hit_tokens` + `prompt_cache_miss_tokens`), `completion_tokens` and `completion_tokens_details.reasoning_tokens`.
The pinned `openai` 3.33.0 SDK keeps DeepSeek's non-standard fields (`reasoning_content`, `prompt_cache_hit_tokens`) on a parsed response, so the chat wire needs no new dependency.
The Responses API DeepSeek also offers is stateless and silently ignores `previous_response_id`, `store`, `include`, `context_management` and `truncation`;
the design does not use it.

Independent evidence the provider does not gate this work: [arXiv 2607.15263](https://arxiv.org/abs/2607.15263) counts refusals on Cybench CTFs as GPT-5.6 Sol 106/117, Terra 39/117, Claude Fable 5 117/117, DeepSeek V4 Flash 0/117, V4 Pro 0/114.
The same weak safeguards make prompt injection a likely hazard rather than a measured one:
jailbreaks succeed 77–100% of the time on V4 ([FAR.AI](https://www.far.ai/blog/security-stress-test-deepseek-v4-pros-safeguards), [Neo Research](https://neoresearch.ai/research/deepseek-v4-pro-safety-evaluation/)),
and NIST CAISI's 2025 study of earlier DeepSeek models found agents hijacked far more often than US models ([report coverage](https://www.techrepublic.com/article/news-deepseek-security-gaps-caisi-study/));
§9 turns this into a risk.

Facts about this repository the design leans on, each checked in the code:
`bro` and `bro.trails` are namespace packages while `bro.llm`, `bro.harness` and `ride` are regular ones (a regular package cannot be contributed to, so a plug-in's package is a leaf such as `bro.deepseek`);
the credential store ignores a stored name whose kind is not registered, so a key file may exist before its plug-in is installed;
`trail_cost` takes the provider from the trail header's `native.llm.type`, so pricing never guesses a provider from a model slug;
`usage.vendor_of` has no caller outside its own test;
nothing catches `mu`'s `IncompleteResponse` or `TruncatedResponse`.

Verified in the review of 2026-10-26, each against the code at `1f09698b` or the vendor's pages.
The pinned uv (0.13.1, `bro/setup/uv-version`) cannot lock a workspace member that sits inside another member's directory, because its search for the workspace root stops at the enclosing project;
sibling directories and grouping directories lock, sync and build.
The trails server stores rows verbatim (the `bro` format whitelists no row attributes, so a writer's added key such as `wire` is accepted), computes the projection when a trail is read,
and folds the header's per-model usage from the raw `response.usage` and `response.model` at write time, whatever the vocabulary.
Today's `bro` format projects a chat-shaped record as its `llm_call` event alone.
The server answers `/health` without checking its formats, refuses a trail whose format is not installed with a 400 that `NetworkStore` does not retry, and runs two tasks under a rolling deploy;
recording is mandatory.
`stage_bro_wheel` stages exactly one wheel, and the Dockerfile asserts one.
The operator's trails are hosted, so HP4 applies.
`LLMSpec.from_dict` imports every provider, `Installation.take` resolves every bro's recipe under every harness and every provider's key needs, and the `--llm` preset fallback rides on the roster check in `LLMSelection`.
The host `llm` list is `projects.<repo>.bros.<bro>.llm`, read by the authority reading and not by an in-process `bro run`.
Claude's hooks and status line are built into the `--settings` argument at every launch, none of the six `ride.claude.*` scripts is a session command, and the neutral `ride` runtime imports nothing from `ride.claude`.
`mu` has three callers (spell dispatch, source summary, page reader), and among its content classes only `Text` and `JSON` have any.
A `$cred` reference with a cacheable source is expanded into the session's copy of the material, and one that names only a kind resolves against the instances the hydrating store selects for the kinds the session holds.
DeepSeek's pages, read the same day, state the effort mapping (`minimal` to `low`; `medium` and `xhigh` to `high`), `thinking` passed in `extra_body` by the OpenAI SDK, the `max_tokens` defaults,
function names of at most 128 characters with no documented limit on the number of tools (a native `bro-dev` sends 51), a 400 for `required` or named `tool_choice` in thinking mode,
429 above 500 concurrent requests per account for `deepseek-v4-pro`, a connection closed after ten minutes without the start of inference, per-model prices for cache-hit input,
cache-miss input and output with off-peak rates at half of peak (peak is 01:00–04:00 and 06:00–10:00 UTC on weekdays, Chinese public holidays excepted),
and an Anthropic-compatible API beside the Responses one.

Unverified until the first live run (the live probe after Landing 2, then V): X1 DeepSeek completes the actual #1086 build (checked by V, §8);
X2 the exact shape of its responses, including `response.model` strings, the `arguments` of a no-argument tool call and whether that spelling is accepted when sent back;
X3 behavior at `reasoning_effort: max`, which one vendor with a stake claims raises refusals;
X4 how long a single non-streaming call runs at high effort;
X5 history growth under the pass-back rule over a long session;
X7 the price table's numbers, read from the vendor page when the table is written;
X8 whether thinking mode accepts a history whose earlier assistant turns carry no `reasoning_content`, including a history recorded with thinking off and continued with thinking on (the live probe answers it);
X9 whether DeepSeek accepts every tool schema the `bro-dev` roster sends;
X10 how often the capacity stop occurs and whether its response carries partial content or usage.
(X6, whether the operator's trails are hosted, was checked in the review: they are.)

### 3. Concepts

| Term | Meaning |
|---|---|
| plug-in | A distribution that contributes entries to the entry-point groups of §4.1. Plug-ins are found from installation metadata without importing them. (The persona-enabled package sketched in #1054 is a different notion that composes with these.) |
| roster | The installed entries of one entry-point group, by name, read from metadata; a duplicate name is an error. Framework code reaches a plug-in only through a roster. |
| provider | A model service named by one string that is the `--llm` provider slot, the recipe's `TYPE` and the credential kind holding its key: `openai`, `anthropic`, `deepseek`, `echo`. A provider plug-in contributes a declaration and a credential kind. |
| recipe | A frozen instance of a provider's `LLMSpec`: provider, model and the provider's knobs. It carries what a wire needs from the provider: `WIRE`, `ENDPOINT`, `request_options()`, `needed_secrets()`, `is_transient()` and `accepts()` (P2). |
| wire | A protocol with its own record shape: `responses` (OpenAI Responses API), `chat` (Chat Completions), `messages` (Anthropic Messages, decode only) and `echo` (diagnostic). A wire plug-in has a decode side and, when it can be driven, a client side. |
| decode side | A wire's SDK-free module: `NAME`, `CHAINING`, `LEGACY` where the wire claims untagged records, `decode` and `counts`. It defines what a recorded response means (WP2). |
| client side | A wire's class implementing the port; it imports the SDK (WP3). |
| chaining | Where conversation state lives. `server`: the provider keeps it, and a request carries the new items plus a chain id (`responses`). `client`: the client keeps the history and resends it whole every exchange (`chat`). A property of the wire. |
| port | The `Wire` interface in core that a client side implements (§4.3). |
| input | A neutral item a loop hands a wire to send: `SystemPrompt`, `UserMessage`, `Notification` (framework text sent in the user role) or `ToolOutput`. |
| exchange | The port operation: encode inputs, send one request, return its leg. |
| leg | The neutral result of an exchange, one request and its response: the record to persist, usage counts, ordered parts, the stop. A conversation turn (counted by `turn_index`) holds one or more legs. |
| part | One item of a response, in order: `Reasoning`, `Message`, `Call` or `Unknown`. |
| stop | Why a response ended, in the neutral vocabulary `completed`, `truncated`, `filtered`, `capacity` and `failed` (U4); a wire's `decode` maps its provider's own words onto it. |
| capacity stop | A response whose stop is `capacity`: the provider interrupted it for lack of resources and the request is not at fault, so the wire sends it again (U6). |
| step | One entry of a trail, the persisted record of a run: `user_input`, `notification`, `llm_call` or `tool_result`, with extras such as `turn_index`. |
| record | The persisted body of an `llm_call` step, `{request, response}`, tagged with its wire (R1, R2). |
| loop | `Loop`, the provider-neutral agent loop of the native harness; an `LLM` the runner drives. |
| harness | What drives an agent session: `bro` (the native harness: loop, runner, fork and recorder; distribution `bro-native`) and `claude` (Claude Code; distribution `bro-claude`). |
| trail format | A harness's envelope for its trails: header fields, record framing, classification, lineage and projection, keyed by the header's `harness`. It decodes wire payloads through the wire roster and holds no wire's response grammar (HP3). |
| role | A named job an auxiliary call does (`dispatch`, `summary`, `reader`): chosen by the caller of `mu`, mapped to an LLM by the operator in the `mu` credential (A3). |
| `mu` | The auxiliary one-shot LLM credential kind (A1) and the call it configures. |
| landing | A set of stages that reach master together through an integration branch of their own (§11). |

### 4. Architecture

#### 4.1 Plug-in kinds and layers

Four kinds of plug-in, each in its own directory family, each a uv workspace member:

| Kind | Directory | Distribution | Package | Registers | Depends on |
|---|---|---|---|---|---|
| provider | `llms/<name>/` | `bro-<name>` | `bro.<name>` | `bro.llm.providers`, `bro.credentials` | `bro`; its wire's distribution |
| wire | `wires/<name>/` | `bro-wire-<name>` | `bro.wire_<name>` | `bro.llm.wires`; `bro.llm.wire_clients` when it can be driven | `bro`; the SDK in the extra `client` |
| harness | `harnesses/<name>/harness/` | `bro-<name>` | `bro.<name>` | `bro.harnesses` and the groups it registers today | `bro`, `bro-ride`, its format distribution |
| trail format | `harnesses/<name>/trails/` | `bro-<name>-trails` | `bro.<name>_trails` | `bro.trail_formats` | `bro`; the wires its envelope always carries |

The workspace gains `llms/openai`, `llms/anthropic`, `llms/deepseek`, `wires/responses`, `wires/chat`, `wires/messages`, `harnesses/native/harness` (today's `native/`),
`harnesses/claude/harness` (today's `ride/ride/claude`, split from `bro-ride`), `harnesses/native/trails` and `harnesses/claude/trails`.
A uv workspace member cannot sit inside another member's directory (the pinned uv stops its search for the workspace root at the enclosing project),
so `harnesses/<name>/` holds no `pyproject.toml` of its own and the harness and its trail format are siblings in it (D28).
The trail format entry is named as its harness: `bro` in `bro-native-trails`, `claude` in `bro-claude-trails`.
`bro-ride` keeps the managed-workspace runtime and depends on no provider and no harness; `bro` depends on no other member.
The new groups are `bro.llm.providers`, `bro.llm.wires` and `bro.llm.wire_clients`; every other group keeps its meaning (`bro/reference/extending.md`).

| Layer | Owns | Lives in |
|---|---|---|
| Runner | trail lifetime, observer, inbox, watches (unchanged) | `harnesses/native/harness` (`bro.native.runner`) |
| Loop | tool execution and errors, inbox drains and notifications, interrupted-turn bookkeeping, turn and call indices, recorded steps, observer events, cumulative usage | `harnesses/native/harness` (`bro.native.loop`) |
| Wire client | encoding inputs, request assembly, sending under retry, conversation state, building a leg from a response, restoring state from a trail, the structured one-shot | the wire's distribution, behind the port in core (`bro.llm.wire`) |
| Wire decode | what a recorded response means: parts, stop, usage vocabulary | the wire's distribution, SDK-free; read by trail formats, fork, usage and cost |
| Rosters | reading entry points, selecting a plug-in by name, opening a wire client from a recipe | core: `bro.llm.providers`, `bro.llm.wires` |
| Provider | the recipe and everything declarative: models, prices, failure signatures, credential kind | `llms/<name>` |
| Harness | driving a session; the native harness holds the loop, runner, fork and recorder, the Claude harness holds Claude Code's assembly, gates and recorder | `harnesses/<name>/harness` |
| Trail format | one harness's envelope: parse, classify, project, lineage | `harnesses/<name>/trails` |
| Records | wire tags and `DecodedCall` | `bro/llm/records.py` (core, SDK-free) |

#### 4.2 Records

**R1 Shape.** An `llm_call` step's body is `{request, response}`.
`response` is the provider's JSON reply as dumped, nulls included.
`request` holds the request fields of the exchange, `tools` among them (always a list, possibly empty; the recorder moves it to the content-addressed tool store),
with the exchange's new items under the wire's own field (`input` for `responses`, `messages` for `chat`) as W4 fixes.
The step also carries `turn_index`, `call_index`, `wire` and, when the response has an id, `response_id`.

**R2 Tag.** `wire` is the wire's `NAME`.
A record without the tag is claimed by the wire whose decode side declares `LEGACY = True`;
only `responses` does, because every record written before the tag existed is a Responses record, so the rule is exact rather than a default.
Two claimants are a roster error.
A tag naming a wire that is not installed, and an untagged record nobody claims, project the whole record as one `harness_event` (the projection's catch-all event kind).

**R3 Decode.** `bro.llm.wires.decode(name, response)` returns a `DecodedCall`: `model`, raw `usage`, `service_tier`, ordered `parts`, `stop` (U4) and `refusal`;
`calls`, `reply` and `terminal` derive from the parts, `terminal` also needing a `completed` stop.
Part indexes are 1-based positions in the response's item sequence.
A wire's decode side defines the mapping:

- `responses`: the `output` items in order;
  `reasoning` yields one `Reasoning` per non-empty summary text (sharing the item's index), `message` a `Message` of its joined `output_text` parts (commentary when `phase` is `commentary`), `function_call` a `Call`, anything else `Unknown`.
- `chat`: the sequence is the choice's non-empty `reasoning_content`, its non-empty `content`, then each tool call;
  `finish_reason` gives the stop;
  a response without exactly one choice is one `Unknown`.
- `messages`: the `content` blocks in order;
  a `thinking` block with text is a `Reasoning`, `text` a `Message`, `tool_use` a `Call`, any other block (`redacted_thinking` among them) an `Unknown`;
  the stop reads `stop_reason`.
- `echo`: one `Message` of the response's `text`.

A `Call` carries its raw argument string, the parsed arguments (`{'_raw_arguments': raw}` when the string is not JSON, as events show today) and whether they are a JSON object.
An empty or whitespace-only argument string parses as `{}`, because a tool with no parameters may be called with none and DeepSeek's spelling of that call is unverified (X2).
A `Message` in a response that also has a `Call` is interim.
`reply` is the non-commentary message texts joined by a blank line, or none when the response has calls.
Decode never raises on an unrecognized shape; a client that cannot use a response raises itself (W2).

**R4 Projection.** A trail format projects an `llm_call` through `decode`:
the call event (model, usage, service tier and, when it is not `completed`, the stop), then `reasoning`, `assistant` (with `terminal`) and `tool_call` events at their part indexes, and `harness_event` for `Unknown`.
The `bro` format takes the wire from R2; the `claude` format decodes its content blocks as `messages`.
A trails server without a record's wire installed projects that record as R2 says and stores everything intact (HP4).

Example, a chat record's second leg (tool output sent, final text returned):

```json
{"kind": "llm_call", "wire": "chat", "turn_index": 0, "call_index": 2, "response_id": "c2",
 "body": {
  "request": {"model": "deepseek-v4-pro", "tools": ["…"], "thinking": {"type": "enabled"}, "reasoning_effort": "high",
              "messages": [{"role": "tool", "tool_call_id": "call_a", "content": "README.md\nbro/"}]},
  "response": {"model": "deepseek-v4-pro",
               "choices": [{"finish_reason": "stop",
                            "message": {"role": "assistant", "reasoning_content": "The listing shows…", "content": "The repo has…"}}],
               "usage": {"prompt_tokens": 1000, "prompt_cache_hit_tokens": 900, "prompt_cache_miss_tokens": 100, "completion_tokens": 50}}}}
```

It decodes to `Reasoning(1)`, `Message(2)`, stop `completed`, reply `The repo has…`, terminal.
The first leg of the same turn decodes to `Reasoning(1)`, `Call(2, call_a)`;
the tool step linked to that call carries `{step_id: <that llm_call>, index: 2}`, the index the projection gives the `tool_call` event.
A record written before this design has no `wire` key and decodes as `responses`.

#### 4.3 The wire port

A client side implements `Wire`: `NAME` and `CHAINING` (from its decode side), `OWNED` (the request fields the wire sets itself),
`exchange(inputs, tools, request_timeout) -> Leg`, `restore(prefix)`, `seed_chain(response_id)` and `structured(prompt, result, contents, request_timeout)`.
`bro.llm.wires.open(recipe, secrets)` finds the class through `recipe.WIRE` in `bro.llm.wire_clients`, constructs it from the recipe and the provider's credential material, and returns it;
the native harness and `mu` both open wires this way.
A `Leg` holds the record, step extras (`response_id`), `wire`, `model`, usage counts (the four billed classes, U1, or none when the provider reports no usage), parts, stop, refusal and reply.

**W1 Tools.** The loop resolves the run's tools once and passes them to every exchange; the wire encodes them into its format.

**W2 Exchange.** A wire encodes the inputs in its item format, assembles the request from its state, those items, the encoded tools and the recipe's `request_options()`,
sends it with the caller's `request_timeout` under the shared retry (U3), captures the response as the leg's record, advances state (W3) and returns the leg.
`wires.open` refuses a recipe whose `request_options()` names a field in `OWNED`.
A content part the recipe's `accepts` refuses (an image for a model without vision) raises `UnsupportedContent` naming the part and the model before anything is sent;
`UnsupportedContent` and `LLMStopped` (L9) are new exceptions beside `LLMUnavailable` in `bro.llm.llm`.
It raises `LLMUnavailable` when the retry window runs out, and `RuntimeError` for a completed response it cannot use: no choice or output, neither a call nor reply text, a refusal with no text, a tool call missing its id or name.
A stop other than `completed` is returned in the leg for the loop to record and raise (L9), except `capacity`, which the wire retries first (U6).

**W3 State.** A server-chained wire's state is the id of the last response received; each request sends the new items and, once state exists, `previous_response_id`.
A client-chained wire's state is the history, every item it sent and every item it received, in order;
each request sends history plus the new items and no chain field of any kind.
State advances only when a response with the stop `completed` arrives;
a cancelled or failed exchange, and a response with any other stop, leaves it unchanged, and the loop re-sends (L8).
For example, exchange 2 of a chat run sends `system, user, assistant(call_a), tool(call_a)`.

**W4 Record economy.** A leg's recorded request carries the exchange's new items only, under both chainings; a client-chained wire never records the history it resends.
The recorded request of exchange 2 above holds only `tool(call_a)`.

**W5 One interpretation.** A wire builds a leg's parts, stop and usage counts by running its `decode` (R3) and `counts` (U1) over the response JSON it also records, so what the loop acts on, what is recorded and what is projected cannot diverge.

**W6 Restore.** `restore(prefix)` rebuilds state for a fork (F4) from the prefix: the system prompt and the recorded steps through the fork point.
A wire rebuilds from the steps' own records, never from the recorded requests: the text of `user_input` and `notification` steps, the response JSON of `llm_call` steps and the raw output of `tool_result` steps, which it encodes as it sends them (L5);
content a step body does not hold, such as an image attached to a user message, is not replayed.
Each wire sanitizes what it replays: items as the API accepts them as input, with response-only fields and nulls stripped and Responses `compaction` items dropped.

**W7 Chain seed.** `seed_chain(response_id)` exists on server-chained wires only;
any other wire raises, so a stateless provider can never be handed a chain it would silently ignore.

**W8 Conformance.** Every wire with a client passes one shared suite over canned responses: a plain reply;
reasoning plus a reply;
interim text with calls;
calls with valid, empty and malformed arguments;
usage counts;
each stop of U4, a `capacity` stop on an injected clock (answered on a retry, and exhausted after the sixth);
record, then `restore`, then the next request carries the same history the live run sent;
and a call's index equal to the index the projection gives its `tool_call` event.
`echo` emits no calls and passes the cases that need none.

**W9 Structured one-shot.** `structured` sends one stateless request, a system prompt and one user message of the encoded `contents`, asks for output matching the pydantic model `result`, validates it and returns the instance.
`responses` enforces the schema through its JSON-schema output; `chat` asks for JSON mode, states the schema in the prompt and validates with pydantic.
An empty or invalid reply raises.
It retries under U3, the capacity retry included, applies the recipe's `request_options()`, adds nothing to a loop's usage, and raises `LLMStopped` for a stop other than `completed` (L9).
Contents follow W2's encoding and its `UnsupportedContent`.

#### 4.4 The loop

Behavior is preserved from the current client unless a rule says otherwise; the existing client tests carry over as the check.
`Loop` implements `LLM`, whose `send` and `wake` the runner calls; `LLM` also gains the seams `restore` and `seed_chain`, which `Loop` hands to its wire.

**L1 Order.** An exchange's inputs are: the pending inputs of an interrupted exchange (L8);
then, when the inbox has news, one `Notification`;
then the incoming messages, where a `system` message becomes `SystemPrompt` and is dropped once after a restore (the restored state carries the prompt).
A wake sends the notification alone and raises if the inbox has none.

**L2 Steps.** Each incoming user message records a `user_input` step before the exchange and each drained notification a `notification` step.

**L3 Indices.** `call_index` rises by one per exchange;
`turn_index` rises when a notification turn starts or a user message arrives after the conversation already had user input.

**L4 After an exchange.** The loop records the `llm_call` step with `turn_index`, `call_index`, `wire` and the leg's extras;
publishes cumulative usage keyed by the leg's model;
raises `LLMStopped` if the stop is not `completed` (L9);
then emits observer events in part order (`ReasoningEvent`, `InterimAssistantTextEvent`, `ToolCallEvent`) and remembers each call's source (step id, part index) for its tool step.

**L5 Tools.** Calls run in order, each with its call's source as the tracker's current tool step.
A tool exception becomes an error output the model reads;
`ToolControlSignal` propagates;
each result emits `ToolResultEvent` and records a `tool_result` step with the raw output and `is_error`.
Outputs become `ToolOutput` inputs; the wire sends them as text.

**L6 Malformed arguments (new).** A call whose arguments are not a JSON object becomes an error output (`tool 'x' failed: arguments are not a JSON object`) without invoking the tool.
Today such a call ends the run; DeepSeek's docs warn arguments "may be invalid", and a model that is told can retry.

**L7 News after tools.** After a batch of outputs the loop drains the inbox and appends one `Notification` after the outputs.

**L8 Interruption.** Cancelled during tool execution, or after a leg with calls arrived and before its outputs exist (including while the leg is being recorded, which today leaves a chain whose calls nothing answers):
each unanswered call gets the fixed interrupted output (recorded as an error `tool_result`) and every output so far becomes pending.
Cancelled during an exchange: that exchange's inputs become pending.
Pending inputs lead the next exchange (L1) and are never lost between a drain and an exchange.

**L9 Stops (new for the Responses wire).** A leg whose stop is not `completed` makes the loop raise `LLMStopped(reason)` once its `llm_call` step is recorded and its usage published, before any event or tool runs,
so the trail shows the stop, the cost is counted and the run fails with the reason named.
A `filtered` stop reads as the provider's filter ending the response, not as a truncated answer, and a `capacity` leg reaches the loop only once U6's retries are exhausted, so the run then fails naming the provider's capacity.
The exchange's inputs become pending as in L8, so a session that continues re-sends them first.
Today an `incomplete` Responses status is ignored and its partial text returned.

**L10 Reply.** With no calls the loop returns the leg's reply; the Responses wire keeps today's reply extraction exactly (refusal handling and the text fallback).

Example: a background job exits while the user types "continue".
The next exchange's inputs are `[Notification('[notification: job 3 exited]'), UserMessage('continue')]`.
If that exchange is cancelled, both are pending and lead the next one, ahead of any news that arrived since.

#### 4.5 Fork and resume

**F1** `fork` is wire-neutral: it walks the chain of ancestors, gathers the steps through the fork point and asks the target `LLM` to `restore` or `seed_chain`.
It names no provider and reaches into no client's fields.

**F2 Same wire only.** Every `llm_call` step in the replayed chain must carry the target recipe's `WIRE` (R2);
otherwise the fork is refused before anything is built, naming both wires and suggesting a recipe on the recorded wire.
A recipe with no client-driven wire is refused the same way.

**F3** Server-side seeding applies when the target wire is server-chained, provider and model equal the recorded ones, the fork point is an `llm_call` with a response id and a `completed` stop and the prompt is unchanged;
every other case restores client-side.
A client-chained wire is never seeded with a chain, whatever response ids its records carry.

**F4** A client-chained restore rebuilds the history from the steps; a server-chained restore rebuilds its input list and sends it whole once.
`latest_fork_point` finds pending calls and stops through `decode`, so it is correct for either wire.
A recorded `llm_call` whose stop is not `completed` never entered the conversation (W3): `latest_fork_point` passes over it and `restore` leaves it out, so a run that ended on a stop resumes from the point before the failed call.

**F5** `ride resume` continues the recorded recipe, so it is always same-wire.

| Recorded | Target recipe | Fork point | Result |
|---|---|---|---|
| responses, openai `gpt-6.1-sol` | openai `gpt-6.1-sol` | `llm_call` with response id, same prompt | `seed_chain` |
| responses, openai `gpt-6.1-sol` | openai `gpt-5.6-terra` | any | client-side `restore` |
| chat, deepseek `pro` | deepseek `pro` | `llm_call` | client-side `restore`, never a chain |
| responses, openai | deepseek | any | refused (wire mismatch) |
| chat, deepseek | openai | any | refused (wire mismatch) |

#### 4.6 Usage, pricing, retry, stops, families

**U1 Usage.** A wire's decode side exports `counts(usage)`, mapping that wire's raw usage vocabulary onto the four billed classes (`bro.llm.usage.CLASSES`);
`from_vendor_counts(raw)` becomes `from_wire_counts(wire, raw)`, which dispatches by wire name through the roster, so core sniffs no vocabulary.
`messages` reads Anthropic's disjoint fields;
`responses` reads `input_tokens` with `input_tokens_details`, the cached and cache-written parts coming off the total;
`chat` reads `prompt_tokens` with `prompt_cache_hit_tokens`, else `prompt_tokens_details.cached_tokens`, and `completion_tokens`, reasoning tokens staying inside the output.
For example `{prompt_tokens: 1000, prompt_cache_hit_tokens: 900, prompt_cache_miss_tokens: 100, completion_tokens: 50}` (chat) and
`{input_tokens: 1000, input_tokens_details: {cached_tokens: 900, cache_write_tokens: 0}, output_tokens: 50}` (responses) both give input 100, cache_write 0, cache_read 900, output 50.
A caller that holds only raw usage names the wire: the native loop through its leg, Claude's usage publisher as `messages`, the benchmark through the trial's provider recipe class.

**U2 Pricing.** Each provider's `price(model, usage, service_tier, table)` reads the provider's raw billing vocabulary.
DeepSeek's table holds the vendor's per-model rates for cache-hit input, cache-miss input and output at list (peak) rates;
the vendor halves them off-peak, so a computed cost is an upper bound, high by at most 2×;
a model with no row is unpriced.

**U3 Retry.** `retry_llm(request, transient)` replaces `retry_openai`: it repeats while `transient(error)` through `RETRY_WINDOW_SECONDS`, then raises `LLMUnavailable`.
The recipe's `is_transient` decides: OpenAI treats connection errors, timeouts and status ≥ 500 as transient and a 429 as terminal (it also means exhausted quota);
DeepSeek treats those and 429 (rate or concurrency limit) as transient, and 400, 401, 402 (balance) and 422 as terminal.

**U4 Stops.** A wire's `decode` maps its stop vocabulary onto `completed`, `truncated`, `filtered`, `capacity` and `failed`, and `LLMStopped` carries the provider's own word beside the neutral one.
`chat`: `stop` and `tool_calls` complete; `length` truncates; `content_filter` filters; `insufficient_system_resource` is `capacity`; `aborted` and any other value fail.
`responses`: `completed` completes;
`incomplete` truncates (`max_output_tokens`) or filters (`content_filter`) and fails for any other reason;
`failed` and any other status fail.
`messages`: `max_tokens` truncates, `refusal` filters, every other value (a streaming fragment's null included) completes.
`echo` completes.

**U5 Families.** `usage.model_family(slug)` labels a slug for commit footers.
A provider whose slugs need a rule beyond the generic one exports `MODEL_SLUG` (a pattern of the slugs it bills) and `model_family(slug)` (P2);
core asks the provider whose pattern matches, importing the roster lazily, and labels any other slug by stripping a trailing `-YYYY-MM-DD` date.
Anthropic's `Opus 4.8` rule moves to `bro-anthropic`.
`usage.vendor_of` and its pattern list have no caller and are deleted.

**U6 Capacity retry.** A response whose stop is `capacity` is not an answer:
the wire sends the same request again from the same state (W3), after a wait of 10 s before the first retry and then twice the previous wait up to 300 s, so the six waits are 10, 20, 40, 80, 160 and 300 s.
If the sixth retry also ends in `capacity`, the wire returns that response as the leg and L9 ends the run.
Any other response ends the sequence, so every exchange starts again at 10 s.
A wait ends as soon as the exchange is cancelled (L8), each retry is logged, and none is recorded as a step.
Each send is itself under U3.
For example, a capacity stop answered on the third retry costs 70 s of waiting, and a provider that stays at capacity costs 610 s and then ends the run.

#### 4.7 Plug-in contracts

Provider plug-ins:

**P1 Registration.** A provider plug-in registers `[project.entry-points."bro.llm.providers"] <name> = "<module>"` (its declaration module) and
`[project.entry-points."bro.credentials"] <name> = "<module>:<KIND>"` (its credential kind, a dict like the entries of `bro/base/registry.json`).
The entry name is the provider name, follows `[a-z][a-z0-9-]*` and equals the recipe's `TYPE`; two distributions claiming one name are an error.
Entry points are installation metadata: sync the environment after adding or removing one.

**P2 Declaration module.** It exports:

- `LLMSpec`, the recipe class, with `TYPE` and, for a provider a wire can drive, `WIRE` (the wire's name), `ENDPOINT` where the SDK default is wrong, `request_options()` (a dict of the wire's request fields lowered from the recipe's knobs),
  `needed_secrets()` (the credential kind), `is_transient(error)` (U3) and `accepts(part_kind)` (`text` and `image` by default; DeepSeek's `deepseek-v4-pro` does not accept `image`);
- `DEFAULT_MODEL`, `MODELS` (short names to ids) and `MODEL_ID` where a verbatim name needs a shape;
- `FAILURE_SIGNATURES`;
- `PRICE_TABLE`, `PRICE_TABLE_SHA256`, `price`, and `price_table_from_content` where benchmark retention prices its calls;
- `MODEL_SLUG` and `model_family(slug)` where the provider's slugs need a label rule (U5).

The reasoning, tier and compaction lowering the OpenAI client does today moves into the OpenAI recipe's `request_options()`.

**P3 Roster.** `bro.llm.providers.known_names()` reads entry-point metadata, imports nothing and returns names sorted.
A declaration is imported when a recipe names its provider, and by every surface that must learn what providers offer:
a bare model name searched among providers, the providers a harness serves (`Harness.llm_providers`, computed from the roster; the native harness serves those whose `WIRE` has a client entry),
the effort vocabularies the permission declaration lists, each provider's credential needs in the installation reading, and `model_family`.
`LLMSpec.from_dict` resolves a stored recipe by its `type`, which is the entry name, and imports that declaration alone;
nothing imports every provider, and `NativeLLMSpec` is retired because a harness serves a recipe when its wire has a client.
On import a declaration is checked: its `LLMSpec.TYPE` equals the entry name.
A declaration that fails to import is skipped by every search above with a warning naming it, and fails by name when a recipe selects it, so a broken plug-in affects only its own recipes.
An unknown provider is an error naming the installed providers and saying to install the plug-in that provides it.
A recipe naming a wire with no client (`anthropic`'s `messages`) is not served by the native harness: launching it there is refused, naming the wire, and a harness that drives its own loop serves it.

**P4 Credential.** A provider's key is a credential kind contributed by its plug-in, with material `{"api_key": ...}` unless the provider documents another shape;
a named instance is `<name>+<instance>`.
A run whose key is missing is refused at start by the runner's existing credential gate.

**P5 Imports.** A declaration module imports no SDK at module level and nothing from a harness; `is_transient` imports the SDK's exception types inside the function.
A provider distribution depends on its wire's distribution, with the extra `client` when the wire is client-driven.

Wire plug-ins:

**WP1 Registration.** A wire registers `[project.entry-points."bro.llm.wires"] <name> = "<decode module>"` and, when it can be driven, `[project.entry-points."bro.llm.wire_clients"] <name> = "<module>:<Client>"`.
The entry name is the wire's `NAME`, follows `[a-z][a-z0-9-]*`, and is unique per group.
`messages` registers no client.

**WP2 Decode side.** It exports `NAME`, `CHAINING` (`server` or `client`), `LEGACY` (absent means false), `decode(response)` (R3) and `counts(usage)` (U1).
It imports no SDK at module level and nothing from a harness, and depends on `bro` alone, so a trails server installs decode sides without any SDK.

**WP3 Client side.** The class implementing `Wire` (§4.3).
It may import the SDK, which the distribution lists in its extra `client`.
A wire's client and decode side are developed together and share one version.

**WP4 Conformance.** W8's suite is parameterized over the wires that have a client; a new wire adds its canned responses and runs unchanged.

Harness and trail-format plug-ins:

**HP1 Harness.** A harness registers `bro.harnesses` `<name> = "<module>:<HARNESS>"` as today, and the other groups it registers today.
It may import the providers and wires it depends on.
The Claude harness depends on `bro-anthropic` and `bro-wire-messages`.

**HP2 Format registration.** A format registers `bro.trail_formats` under the harness's name, the value the trail header's `harness` carries.
A harness depends on its format distribution, so any installation that can write its trails can read them.

**HP3 Envelope and payload.** A format owns the envelope: header fields, record framing, step classification, lineage and the event kinds.
It decodes a payload through the wire roster (R3, R4) and holds no wire's response grammar.
The `claude` format always carries Anthropic content blocks, so `bro-claude-trails` depends on `bro-wire-messages`;
the `bro` format reads the wire each record names, so `bro-native-trails` depends on no wire.

**HP4 Servers.** A trails server installs the formats and the wire decode sides it serves, and declares them:
one list of distributions, read by the image build (which stages one wheel for each), by the Dockerfile and by the server itself,
which exits at startup unless every declared distribution is installed and its `bro.trail_formats` and `bro.llm.wires` entries load.
Without that check a server missing a format passes `/health` and then refuses every write, and recording is mandatory.
A trail whose format is not installed is refused with today's error; a record whose wire is not installed projects as a `harness_event` (R2).
The image verification blazes, appends and projects one native, one Claude and one chat trail.
The order in which servers and writers change is §11.

#### 4.8 Auxiliary one-shot calls

**A1 Kind.** `mu` is a credential kind registered in core, described as the auxiliary one-shot LLM configuration.
It is not literal, so references in its material expand.

**A2 Material.** A JSON object: `llm`, a recipe in the `--llm` grammar naming a provider whose wire is client-driven;
`credential`, that provider's credential material, normally a `$cred` reference that a session's hydration follows;
and an optional `roles`, an object mapping a role name to its own `{llm, credential}`.
The top-level pair is the default.

**A3 Calling.** `mu(prompt, result, *contents, role=None)`,
and `mu.aio` with the same arguments, read the kind's material, take `roles[role]` when the caller names a role the material defines and the default pair otherwise,
resolve `llm` through the provider roster, open the wire client with the recipe and the credential, and call `structured` (W9).
The call takes no model or effort: the material decides.
`contents` are the neutral `Content` classes (`Text`, `JSON`, `JSONList`, `Image`, `ImageFile`, `PngImage`, `PDFFile`) without the OpenAI-shaped `dump()` they carry today;
each reduces to text, image or file parts that the wire encodes (W2).

**A4 Consumers.** The spell interpreter (role `dispatch`), the source summary (`summary`) and the page reader (`reader`) declare `mu` as an optional credential, as they declare an LLM key today,
and read their gates from `credentials.available('mu')`;
a bro or tool that cannot work without it declares it required.
Without the kind they degrade as they do without a key today: cast falls back to the spell tools, a summary request errors, `look` is unavailable.
There is no fallback to a provider key.
A benchmark preset whose trials hold `openai` mounts `cast` today; it holds a `mu` instance so that its trials keep it.

**A5 Failure.** Material that does not parse, names an unknown provider, one whose wire has no client or no `structured`, or lacks its credential, raises an error naming the `mu` credential and the fault.
A malformed role entry never falls back to the default pair.
A stop other than `completed` raises `LLMStopped`, which replaces `IncompleteResponse` and `TruncatedResponse`.

**A6 Instances.** Which `mu` instance a session holds is the operator's pick, per project or per bro (`creds: ["mu+cheap"]`).
Code never names an instance, and `credential_policy_test` confines named-instance reads to the resolver; a role is how a caller chooses within the one held material.

**A7 Content.** A content part a wire cannot carry raises `UnsupportedContent` rather than being dropped.

**A8 References.** A reference in `mu` material that names only a kind (`{"$cred": "openai"}`) resolves against the instance the hydrating store selects for the kinds the session holds,
so a session that does not hold `openai` does not follow the operator's `creds` pick for it.
Material names the instance (`openai+` for the empty one, `openai+work`) and then means the same in every session.

Example, material that reproduces today's behavior (the default for `dispatch`, one entry each for `summary` and `reader`):

```json
{
  "llm": "openai:terra:low",
  "credential": {"$cred": "openai+"},
  "roles": {
    "summary": {"llm": "openai:luna5.6:low", "credential": {"$cred": "openai+"}},
    "reader": {"llm": "openai:luna5.6", "credential": {"$cred": "openai+"}}
  }
}
```

A stage that must keep auxiliary data off OpenAI drops the kind (`drop: ['mu']`) or holds an instance whose entries name another provider.

#### 4.9 Isolation

**N1 Rule.** Framework code imports no plug-in package (provider, wire, harness or trail format) and holds no string literal equal to a provider's name.
It reaches providers through the roster (P3), wires through the wire rosters (WP1) and harnesses through `bro.harnesses`, builds clients with `wires.open`, and receives recipes from launches, host configuration and the `mu` credential.
No harness name is a default anywhere (Q5).

**N2 Where a name may appear.** In a distribution that declares a dependency on the plug-in it names: a plug-in's own packages;
a persona declaration that pins a recipe, in the distribution that holds the persona (`bro-dev`, `bro-local`, `bro-oops`, `bro-browser` depend on `bro-openai`);
a provider on its wire;
the Claude harness on `anthropic` and `messages`;
`bro-native` on its own `echo`.
Tests and docs are not framework code.

**N3 Enforcement.** `local/bro/local/provider_policy_test.py` walks every non-test module of the workspace as `credential_policy_test.py` does and fails on an import of a plug-in package,
or a string literal equal to a provider's name, in a distribution that does not declare a dependency on that plug-in;
the names are read from the workspace's `pyproject.toml` entry points.
`bro/brash.py`'s `echo` builtin is the one non-provider use.
Harness and wire names (`bro`, `chat`, `messages`) are common words in this code, so their literals are left to review, while imports of their packages fall under the same rule.

#### 4.10 Launch inputs: recipe and harness

**Q1 No default recipe.** `BaseBro.llm_spec` is a persona's declaration or absent.
`DEFAULT_LLM_SPEC` is deleted.
The registered personas that relied on it are `bro`, `bro-file-probe`, `bro-watch-probe`, `dev`, `eyebro` and `lead`.

**Q2 Native recipe.** A managed native launch (`ride`, a summon, `dive-in`) runs the launch's selection (`--llm`, a summon's `llm`) laid over the first entry of the host's per-bro `llm` list that its harness runs, laid over the persona's declaration;
the host list is `projects.<repo>.bros.<bro>.llm` in `~/.bro.json`, read by the authority reading.
An in-process `bro run` or `bro chat` has no host list: its flags lay over the persona's declaration.
A selection naming a provider is complete by itself, as that provider's default recipe with the named knobs;
one naming only a model, effort or fast inherits its provider from the base, and with no base is refused as Q3 says.

**Q3 Refusal.** When no complete recipe results, the launch is refused before anything starts, naming the ways to supply one: the launch's `llm` and, for a managed launch, the host list `projects.<repo>.bros.<bro>.llm`.
`default` in that list means the persona's declaration and does not resolve for a persona with none.

**Q4 Earlier readers.** Surfaces that read a bro's recipe before a launch (`bro show`, `ride scope`, the authority reading) treat an absent recipe as no LLM key;
`ride scope --llm` supplies one.
The Claude harness keeps its own session default, which names neither model nor effort, and is unaffected.

**Q5 No default harness.** `DEFAULT_HARNESS` and `DEFAULT_SUMMON_HARNESS` are deleted.
A launch's harness is the first present of: the launch's own (`--harness`, or the `harness` of a summon);
the project's `[tool.bro] harness` (`summon-harness` for a summon that names none);
the host config's top-level key of the same name in `~/.bro.json`;
otherwise the launch is refused, naming those three routes.
The value must be an installed harness's name.
The project's value beats the host's, as a repository states what it is built for.

**Q6 Readers.** `ProjectConfig`, `ride`'s CLI and flags, session assembly, `dive-in`, launch control, the authority reading and the authority scope take the harness from Q5 and carry it as possibly unset until a launch needs it.

**Q7 This repository.** Its `[tool.bro]` declares `harness = "claude"` and `summon-harness = "bro"`, the values the deleted defaults carried, so every launch from this checkout resolves as before.

**Q8 Parse versus resolve.** Parsing a `--llm` value, in a flag, a summon or the host config, checks its grammar only.
A provider or model is resolved against the roster when a launch reaches it, so a host config written for a richer installation parses in a poorer one:
an entry naming a provider the installation lacks is run by no harness there, is skipped with a verbose log line, and the launch moves to the next entry, or is refused naming the missing plug-in when none remains (Q3).
Today the parse checks the provider against the roster, so one such entry would fail every launch from the installation.
A `--llm` value of one word that names no installed provider is looked up as a preset first (the project's `[tool.bro.llm]`, then the host's `llm`, the host winning);
a hit is parsed in its place and a miss is refused, naming the installed providers and the presets.
A word that names an installed provider is that provider, and a preset cannot shadow it.
Today the preset fallback is the failure of the roster check that grammar-only parsing drops.

Example: `bro run dev 'hi'` is refused with `dev declares no LLM recipe: pass --llm provider:model`;
`bro run dev --llm openai:terra 'hi'` runs;
`ride along dev` is refused with `dev declares no LLM recipe: pass --llm provider:model, or set "llm" under projects.<repo>.bros.dev in ~/.bro.json`;
`bro-dev` declares its own recipe and runs unchanged.
In a repository whose `pyproject.toml` has no `[tool.bro] harness`, `ride along` with no `--harness` and no host key is refused with `no harness: pass --harness, set [tool.bro] harness, or set "harness" in ~/.bro.json`.

### 5. DeepSeek on the chat wire

| Item | Value |
|---|---|
| provider plug-in | directory `llms/deepseek/`, distribution `bro-deepseek`, leaf package `bro.deepseek`; depends on `bro` and `bro-wire-chat[client]` |
| entry points | `bro.llm.providers`: `deepseek = "bro.deepseek"`; `bro.credentials`: `deepseek = "bro.deepseek.credentials:KIND"` |
| wire plug-in | directory `wires/chat/`, distribution `bro-wire-chat`, package `bro.wire_chat`; the extra `client` pins `openai==3.33.0`; `bro.llm.wires`: `chat = "bro.wire_chat.decode"` (created by S1); `bro.llm.wire_clients`: `chat = "bro.wire_chat.client:Chat"` (added by S6); chaining `client`; `OWNED` = `model`, `messages`, `tools` |
| provider, wire, endpoint | `deepseek`, `chat`, `https://api.deepseek.com` |
| credential | kind `deepseek`, `{"api_key": …}`; no install hook |
| models | `DEFAULT_MODEL = deepseek-v4-pro`; `pro` → `deepseek-v4-pro`, `flash` → `deepseek-flash`; `MODEL_ID` = `deepseek-[a-z0-9-]+`, so the still-routed `deepseek-v4-flash` passes |
| knobs | `model`, `reasoning_effort` ∈ `none`/`low`/`high`/`max` or unset |
| neutral effort → value | low → `low`, medium → `high`, high → `high`, xhigh → `high`, max → `max` (DeepSeek's own mapping) |
| fast | unsupported; resolution falls back to the plain recipe |
| `request_options()` | unset: nothing (the service defaults to thinking on, effort high); `none`: `thinking: {type: disabled}`; otherwise `thinking: {type: enabled}` and `reasoning_effort` |
| `accepts` | `text` for every model; `image` only for `deepseek-flash` |
| `is_transient` | U3 |
| history message | the assistant message as returned, `content` null kept, `reasoning_content` when present, `tool_calls` when present, every other null or SDK-only field dropped |
| decoded fields | `choices[0].message.{reasoning_content, content, tool_calls}`, `finish_reason`, `usage` (U1) |
| failure signatures | the OpenAI SDK exception names to the neutral categories, plus `Error code: 402` to `usage-limit` |
| prices | U2, read from the vendor page when the table is written, with its provenance and as-of date |

Tools are sent as `{type: function, function: {name, description, parameters}}` with no `strict` and no `tool_choice` (DeepSeek rejects `required` and named choices in thinking mode).
The wire sends no `max_tokens`, so the service defaults apply (64K in thinking mode, 128K at `max`, 8K with thinking off); a hit on that bound is a `length` stop (L9).
The wire never sends `user_id`, `temperature` or `top_p`.
Reasoning arrives as raw chain of thought, not a summary; the display presets already decide how reasoning blocks render.
A context over 1M tokens is a 400 and ends the run; nothing is truncated.
The `insufficient_system_resource` stop is the `capacity` stop (U4, U6).
The service answers 429 above 500 concurrent requests per account for `deepseek-v4-pro` and closes a connection whose request has not started inference after ten minutes;
both reach the retry as transient errors (U3).
`pro` and `flash` collide with no other provider's short names today;
if one ever does, a bare name two providers carry is refused as `LLMSelection` refuses it now, and `deepseek:pro` names the provider.

#### 5.1 The other plug-ins

| Plug-in | Home |
|---|---|
| `openai` provider | `llms/openai/`, `bro-openai`, `bro.openai`: the recipe with the knobs the client lowers today (reasoning, tier, compaction), pricing and the `openai` credential kind; depends on `bro` and `bro-wire-responses[client]`. Distributions whose personas pin its knobs depend on it. |
| `responses` wire | `wires/responses/`, `bro-wire-responses`, `bro.wire_responses`: the decode side and the client side (today's `native/bro/native/llms/openai.py` without the OpenAI knobs, `bro/llm/openai_content.py`, and the Responses half of `mu`); `LEGACY = True`; the extra `client` pins `openai==3.33.0`. |
| `anthropic` provider | `llms/anthropic/`, `bro-anthropic`, `bro.anthropic`: the recipe, pricing, `model_family` and the `anthropic` credential kind; `WIRE = messages`; depends on `bro` and `bro-wire-messages`. |
| `messages` wire | `wires/messages/`, `bro-wire-messages`, `bro.wire_messages`: decode side only (R3, U1, U4), used by `bro-claude` and `bro-claude-trails`. |
| `echo` | the provider and the wire, both in `bro-native`: the engine's own diagnostic provider, which replies with its input. `bro.native.llm` takes `--provider` as a required argument. |
| native harness | `harnesses/native/harness/`, `bro-native`, today's `native/` moved: `Loop`, runner, fork, recorder, the `bro` command; depends on `bro`, `bro-ride` and `bro-native-trails`, and no longer on the OpenAI SDK. |
| Claude harness | `harnesses/claude/harness/`, `bro-claude`, package `bro.claude`: today's `ride.claude` package; its console scripts `ride.claude.*` become `bro.claude.*`; contributes the `claude_code` credential kind with its install hook; depends on `bro`, `bro-ride`, `bro-anthropic`, `bro-wire-messages` and `bro-claude-trails`. |
| trail formats | `harnesses/native/trails/`, `bro-native-trails`, `bro.native_trails`, format `bro` (today's `bro/trails/bro_format.py`); `harnesses/claude/trails/`, `bro-claude-trails`, `bro.claude_trails`, format `claude` (today's `claude_format.py` and `claude_lineage.py`). |

### 6. Survey: every site that names a provider, a wire or a harness, and what happens to it

| Site | What it names | Disposition |
|---|---|---|
| `bro/llm/providers.py` (`_PROVIDER_MODULES`, `LLMSelection.__post_init__`, `parse`), `native/bro/native/providers.py` (`_NATIVE_PROVIDER_MODULES`), `ride/ride/authority_reading.py` (`Configuration.parse` and `_policy` parse every host recipe eagerly) | provider names in selection code; provider and model checks at parse time | P3, Q8; the native table dissolves into `wires.open` and `Loop` |
| `bro/llm/llms/{openai,anthropic,echo}.py`, `native/bro/native/llms/{openai,echo}.py` | the providers themselves, in core and native | §5.1 |
| `native/bro/native/llms/openai.py` | loop, Responses wire and OpenAI knobs in one class | split: `loop.py` in the native harness, the Responses client in its wire, the knobs in the OpenAI recipe (P2) |
| `native/bro/fork.py` | `isinstance(OpenAI)`, the client's private fields, the Responses input list, `previous_response_id` eligibility | F1–F4 |
| `bro/trails/bro_format.py` | the Responses item shape | R3, R4; the format moves to `bro-native-trails` |
| `bro/trails/claude_format.py`, `claude_lineage.py` | the Claude transcript format inside core | move to `bro-claude-trails` (HP2, HP3) |
| `bro/llm/openai_retry.py` | OpenAI exceptions, 429 terminal | `retry.py` (U3); the exception knowledge moves into the recipes |
| `bro/llm/openai_content.py` | Responses content parts | the Responses client; the chat wire gets its own |
| `bro/llm/usage.py` | two raw vocabularies sniffed by shape, `_VENDOR_PATTERNS`, `vendor_of`, Claude's family rule in `model_family` | U1, U5 |
| `native/bro/native/llm.py` | `echo` as the diagnostic CLI's default | `--provider` required |
| `bro/bro.py` | `DEFAULT_LLM_SPEC` | Q1–Q4 |
| `bro/llm/mu.py`, `bro/spells.py` (`CAST_SECRET`, `interpret`), `bro/datasources/searchable.py` (`SUMMARY_SECRET`), `browser/bros/browser/mcp.py` (`_READER_SECRET`, `look`) | the OpenAI key and model ids hard-wired into auxiliary calls | §4.8 |
| `bro/base/registry.json` | the `openai`, `anthropic` and `claude_code` credential kinds | contributed by `bro-openai`, `bro-anthropic` and `bro-claude` (P1, HP1); `mu` is added |
| `bro/base/configs.py`, `bro/workspace/project.py`, `ride/ride/{cli,flags,session,dive_in,launch_control,authority_reading,authority_scope}.py` | `DEFAULT_HARNESS = 'claude'`, `DEFAULT_SUMMON_HARNESS = 'bro'` | Q5–Q7; `bro/base/host_config.py` gains the two top-level keys |
| `ride/ride/claude/**` | the Claude harness inside `bro-ride`, its `ride.claude.*` scripts and its recipe import | becomes `bro-claude` at `harnesses/claude/harness` (HP1); N2 allows `anthropic` and `messages` there |
| `ride/ride/claude/usage_publisher.py` | `from_vendor_counts` over Anthropic's vocabulary | `from_wire_counts('messages', …)` (U1) |
| `benchmark/bro/benchmark/{trial_store,retention,trajectory}.py` | `from_vendor_counts` | `from_wire_counts` with the wire of the trial's provider (U1) |
| personas pinning `openai.LLMSpec` (`local/bros/bro_dev`, `bro_eyebro`, `dev/bros/terminal`, `oops/bros/devoops`, `browser/bros/browser`) | per-persona recipe choices | stay, importing `bro.openai`; their distributions depend on it (N2) |
| `oops/bro/oops/infra/deploy_lib.sh` (`stage_bro_wheel`), `oops/trails/server/Dockerfile` and `verify_image.sh`, `oops/deploy_targets.py` (`paths`), `oops/bro/oops/{trails_deployment,deploy_lib}_test.py` | `bro[trails-server]` alone: one `bro` wheel, asserted to be exactly one, in the staging and in the image | HP4: the declared set, one wheel each, the startup check; `paths` gain the new directories; the tests that pin the old shape follow |
| `bro/launch/llm_flags.py` help, `bro/base/host_config.py` docstring, `bro/llm/llm.py` docstrings, `bro/base/text_window.py` comment, `bro/mcp.py` and `bro/base/condition.py` comments | provider and harness names in prose | reworded neutrally; examples may remain |
| `native/AGENTS.md`, `ride/AGENTS.md`, `bro/llm/AGENTS.md`, `bro/trails/AGENTS.md`, `bro/prompts/AGENTS.md`, `bro/AGENTS.md`, root `AGENTS.md`, `README.md`, `bro/setup/AGENTS.md`, `BOOTSTRAP.md`, `bro/reference/{ride,extending,template}.md` | OpenAI as the sole native provider, the cast and summary key, the group list, the map, paths and script names under `ride/ride/claude` | updated by the stage that changes the fact |
| `conftest.py`, `native/bro/native/distribution_test.py`, `ride/ride/packaging_test.py`, `local/bro/local/run_tests.py`, `local/bro/local/conftest.py`, `bro/bro_test.py`, `bro/prompts/prompts_test.py`, `bro/spells_test.py`, `benchmark/bro/benchmark/bundle_e2e_test.py`, `local/bro/local/packaging_policy_test.py`, `local/bro/local/environment_policy_test.py`, the `bro/trails/` tests that import the formats | default spec, member rosters, file lists (34 roster lines name `ride/ride/claude`), plugin wheel paths, imports of `ride.claude`, the Claude-only composition test | updated with the files; the Claude-only test is retired |
| `benchmark/agents/*`, `bench/` | `openai+benchmark` credential per preset | already data; a benchmark bundle installs the plug-ins its presets name |
| `bro/launch/llm_flags.py` (`selection_from_args`, `add_llm_flags`), `ride/ride/authority_scope.py` (`option_values`, `recipe`, `declaration`), `ride/ride/authority_reading.py` (`Installation.take`, `Configuration.parse`) | the roster check the preset fallback rides on; every provider's effort vocabulary and key needs; every bro's default recipe under every harness | Q4, Q8, P3 |
| `bro/llm/llm.py` (`LLMSpec.from_dict`, `NativeLLMSpec`), `bro/harness/__init__.py` and `ride/ride/harness.py` (`llm_providers`) | `load_all` over the closed provider table; the providers a harness serves as a class constant | P3 |
| `benchmark/bro/benchmark/bundle.py` (`WHEEL_PACKAGES`), `bundle_test.py`, `bundle_e2e_test.py`, `benchmark/pyproject.toml`, `benchmark/uv.lock` | the framework wheels a bundle builds and installs, and the member paths the project resolves | the wheels gain the plug-ins the presets name and the trail-format distributions (the project reads trails); paths follow the moves; presets that mount `cast` hold a `mu` instance (A4) |
| `benchmark/bro/benchmark/harbor_agent.py` | `known_names`, `failure_signatures` and `parse` over the roster; its own `DEFAULT_HARNESS = 'bro'` | the roster calls stay valid, since Q8 only loosens `parse`; the default is the adapter's own preset default, not a framework launch default, and stays |
| `ride/pyproject.toml`, `ride/ride/_entrypoints.py` | `pyyaml` and `watchfiles`, which only the Claude package uses, and the six `ride.claude.*` scripts and their shims | move to `bro-claude` and are regenerated (`sync-scripts`) |
| `bro/llm/usage.py` (footer parsing) | the historic `Claude Code` agent label of committed footers | stays: it reads history and is a data format, not a provider literal |
| docs: `bro/reference/{permissions,conditions}.md`, `bro/datasources/AGENTS.md`, `bro/launch/AGENTS.md`, `browser/AGENTS.md`, `benchmark/AGENTS.md`, `ride/ride/claude/AGENTS.md` | OpenAI as the gate or the default, `ride.claude.*` paths and script names | updated by the stage that changes the fact |

### 7. Selection and operation

DeepSeek is a recipe, not a bro.
A persona says who the agent is (prompt, tools, spells, authority) and a recipe says which model drives it;
the framework keeps them orthogonal (`llm` on a summon, `--llm` on ride, `projects.<repo>.bros.<bro>.llm` in the host config).
A `bro-dev` variant on DeepSeek would copy the prompt, spells and authority, still hold `github`, and gain nothing; every later provider would multiply personas.

A coordinator selects it with `bro::summon(target='bro-dev', llm='deepseek:pro:high', …)`;
the child runs on the native harness, the summon default of this repository (Q7), since the Claude harness serves only Anthropic.
Provider, model, effort and fast are open options, so no grant is needed.
`deepseek` is the provider's LLM key, so the start holds it automatically at the configured pick (the instance the store or the host config chooses, the empty instance by default);
`passes=['deepseek+<name>']` only selects another instance.
A missing key refuses the summon, naming the missing credential.
A reviewer stopped by the same classifier is rerun with `target='bro-eyebro', llm='deepseek:pro:high'`.
`ride scope` gains the shared LLM flags so the operator can check a recipe before using it:
`ride scope --bro bro-dev --harness bro --llm deepseek:pro:high` prints `provider: deepseek` in the descriptor and `deepseek+: PRESENT` or `MISSING` among the credentials;
today it can only describe the bro's default recipe.
The permission model needs no change: the provider, model and effort options are open to every start, and the `bro.cred=deepseek+` use entry is derived for every registered kind.

A running root froze the runtime and permission reading of the installation it started from, which predate the plug-ins;
the root that will coordinate DeepSeek stages is started after the installation is re-synced (§11).
A session that implements the DeepSeek stage cannot itself receive the `deepseek` kind,
so it works offline from fixtures written from DeepSeek's documented response shapes and from an in-test HTTP server that speaks them,
and leaves the live probe to a session of the fresh root or to the operator.

What the operator and the coordinator do, and in what order, is §11.

### 8. Verification

There is no check against the provider before implementation (ruling 10); verification is per stage, one live probe after Landing 2's rollout, and V.

**Per stage.**

- S1 is accepted when the existing projection and fork tests pass unchanged apart from import paths, `llm_call` steps carry `wire`, an untagged record projects as before, `from_wire_counts` equals `from_vendor_counts` on every existing usage fixture,
  and the `chat` decode side passes the decode cases of W8 on fixtures written from DeepSeek's documented response shapes.
- S2 when the existing suites pass in the new layout;
  each format projects the same events as before;
  no default harness remains and launches from this checkout resolve as before;
  the trails-server image builds from its declared set, exits at startup when a declared distribution is missing (a test), and projects one native, one Claude and one chat trail;
  `uv build --package` wheels of `bro-native`, `bro-claude` and the two format distributions contain no foreign tree;
  and a Claude workspace created before the rename resumes after it, its hooks being rebuilt at every launch.
- S3 when the ported client tests pass against `Loop` over the Responses wire, W8 runs with `responses` and `echo` as its members, neither `loop.py` nor `fork.py` names a provider,
  each of L6, L8 and L9 has a test that fails before its commit and passes after, a trail whose last `llm_call` stopped (L9) resumes from the point before it,
  and the capacity retry (U6) is tested on an injected clock.
- S4 when `mu` takes its LLM from the credential and a role, no auxiliary call site names a model id, a provider or a credential kind, core no longer depends on the `openai` SDK,
  and the live probes `spells_llm_test` and `reader_llm_test` pass against a `mu` credential.
- S5 when both rosters resolve every provider by entry point and `known_names()` imports nothing (a test), `provider_policy_test` passes,
  a persona with no recipe is refused as Q3 says, a one-word `--llm` value resolves as Q8 says, a broken declaration is skipped by every search and fails only its own recipes,
  and the existing suites pass in the plug-in layout.
- S6 when W8 passes for `chat` on fixtures written from DeepSeek's documented response shapes,
  the chat client passes a run against an in-test HTTP server that speaks them (the tool loop with `reasoning_content` passed back, a 402, a 429, a 500 and the capacity stop),
  `ride scope --llm deepseek:pro:high` reports the credential present or missing, a fork and resume round trip works on the chat wire, a chat record projects to the expected messages,
  and the live probe is written and registered in `LLM_PYTEST_FILES`.
- S7 when each step of the page maps to an S6 artifact.

**The live probe** is `deepseek_llm_test.py`, written by S6 and run once,
with the key, after Landing 2's rollout and before V (V may run it too), by a `bro-dev` session of the fresh root that holds `deepseek` (a pass of `deepseek+`) or by the operator on the host.
On the real API it runs: a three-leg tool loop with `reasoning_content` passed back,
a tool that takes no arguments among its calls (how its `arguments` is spelled, and whether that spelling is accepted when sent back, X2) and a user message after the tool result (no 400; the shapes of `usage` and `model`, X2);
one request carrying the full tool roster of `bro-dev` as the native harness sends it, 51 tools today, to see that every schema is accepted (X9);
a repeat of the second leg without `reasoning_content`, keeping the error body (X8);
a history recorded with thinking off and continued with thinking on (X8);
and a forced `length` stop with a tiny `max_tokens`.
It saves the raw responses, which hold no secret, so the chat wire's fixtures can be refreshed from real ones.
A finding that contradicts this design amends S6 under `## Design changelog`.

**V, the end-to-end proof** (after Landing 2's rollout, from a fresh root): a `bro-dev` stage on `llm: deepseek:pro:high` is given #1086's task text.
It passes when the session's trail shows the `deepseek` provider with no `LLMStopped` or refusal, a PR carries model-written generators and pinned outcomes with no human-authored replacement, and the `sandbox` stage is green on the corpus.
A protocol failure (an API error, a call the wire mishandles) is fixed in S6's artifacts and V is rerun.
A refusal or `content_filter` stop is evidence for #1091, not a reason to reword around it, and the next candidate is then chosen with the user.

### 9. Risks and limitations

Weak safeguards make injection a likely hazard (§2): a DeepSeek-run `bro-dev` holds the same credentials as any `bro-dev` (`github`, `brog`, `trails`).
Mitigate by routing only stages whose inputs are repository-local, keeping untrusted web content out of the prompt, and keeping the normal review.
A computed cost is an upper bound (U2).
A provider at capacity costs a run, or a `mu` call, up to ten minutes of waiting before it ends (U6).
The pass-back rule grows every request by all reasoning so far (X5); a context over 1M ends the run.
DeepSeek's model ids have changed several times this year;
`MODELS` is a convenience, not a whitelist, and a snapshot string in `response.model` is priced only if its table row exists (U2, X2).
A fork cannot cross wires (F2); continuing a blocked OpenAI conversation on DeepSeek is not supported by this design.
With no early check, a refusal of the #1086 task surfaces only at V, after Landing 2's rollout; S1 to S5 are provider-neutral and stand either way.
The chat wire's first contact with the real API is the live probe, so its fixtures begin as documented shapes (X2).

A recipe resolves only where its plug-in is installed: launching, forking or pricing a DeepSeek trail (`trail_cost`) in an installation without `bro-deepseek` fails by name.
Auxiliary calls stop until `mu.cred` exists, since nothing falls back to a provider key; a session holding `mu` also holds the credential material its entries reference.
The six personas that relied on the default recipe refuse a native launch until the operator supplies one (Q1, Q3).
Moving the Claude harness out of `bro-ride` renames its console scripts;
Claude's settings are rebuilt at every launch, so a resumed workspace picks the new names up, and a workspace launched with an explicit `--runtime-bundle` path keeps its recorded layout until that path is replaced.
Entry points are installation metadata, so a stale environment shows an old roster until it is synced (P1).
A stage's cost at list prices is roughly single-digit dollars (an unverified estimate).

### 10. Reference page

`bro/reference/llm_providers.md`, man topic `llm-providers`: how a provider or model is adopted.
It covers the plug-in contracts (P1–P5, WP1–WP4) and the isolation rule (N1–N3);
choosing a wire and what a new wire needs (a decode side, a client side, the conformance cases, a record tag, and its distribution in the trails-server image's declared set, HP4);
the adoption checklist (an optional early canary against the provider's API for capability, tool-loop rules and response shapes;
the provider distribution under `llms/` with its registrations in the root `pyproject.toml` workspace, pytest, pyright and ruff tables, the `AGENTS.md` map, `local/pyproject.toml`, `distribution_test.py`, `run_tests.py`,
`environment_policy_test` and `uv.lock`; the credential kind; the declaration module with models, effort mapping, failure signatures and price table;
tests with fixtures (captured from a live run where a key exists, otherwise written from the vendor's documented shapes), the live probe file that refreshes them and its roster entry in `run_tests.py`; the docs to update);
adopting a further model of an existing provider (a `MODELS` row, a price row, the id shape, the live probe);
operator steps, the `ride scope --llm` check and, for a provider that can serve auxiliary roles, its `mu` entries;
and a worked example, DeepSeek's two plug-ins.
It does not describe the generalization.
It is listed in `bro/datasources/references.py`, in the `man(...)` of the framework's developer and reviewer personas, in the maps (`AGENTS.md`, `bro/AGENTS.md`, `README.md`) and in the packaging policy test,
and `bro/llm/AGENTS.md` points to it instead of restating the procedure.
`extending.md` lists the three new entry-point groups.

### 11. Stages, landings and rollout

#### 11.1 Stages and landings

The work lands in two landings, each a set of stages that reach master together through an integration branch of its own (D29).
Landing 1 moves code and widens what a server can decode, and no run behaves differently except that a launch must name its harness (Q5); Landing 2 changes behavior.
Between them the hosted trails server is redeployed, so that it serves the chat wire before the first chat record can exist.

| Stage | Scope | Landing | Depends on |
|---|---|---|---|
| S1 | decode sides: `bro/llm/records.py` and the wire roster in core; `wires/responses`, `wires/messages` and `wires/chat` as decode-side members; the `wire` tag stamped by the current client; projection and `latest_fork_point` through `decode`; `from_wire_counts`; `vendor_of` deleted; no behavior change | 1 | none |
| S2 | harness distributions: `harnesses/native/harness` moved, `harnesses/claude/harness` split from `bro-ride` with its scripts renamed, the format distributions split from core, the `claude_code` kind to `bro-claude`, the default harness removed (Q5–Q7), the trails-server image (HP4), the Claude-only test retired, docs | 1 | S1 |
| S3 | loop and port: the port and `wires.open` in core, `Loop`, the Responses client in its wire, `echo` as a wire, the recipe's wire-facing members, the `LLM` seams, fork per wire, `retry_llm` with the capacity retry, neutral names and docs; L6, L8 and L9 as their own commits | 2 | S2 |
| S4 | `mu`: the kind, material and roles (§4.8), neutral contents, its callers, the benchmark presets and docs; `structured` for the Responses wire | 2 | S3 |
| S5 | providers and rosters: `llms/openai`, `llms/anthropic` with `model_family`, the `echo` provider, `bro.llm.providers` by entry point with tolerant reads (P3), recipe presence (Q1–Q4, Q8) including the authority reading, `bro show` and `ride scope --llm`, `--provider` required, `provider_policy_test`, docs | 2 | S3, S4 |
| S6 | DeepSeek: the client side of `wires/chat`, `llms/deepseek`, `structured` for chat, an in-test HTTP server speaking the documented shapes, a permissions cheat-sheet scenario ("run a child on another provider"), the live probe file, docs | 2 | S5 |
| S7 | the reference page (§10) | 2 | S6 |
| V | the end-to-end proof (§8) | after Landing 2 | S6, a fresh root |

A stage that must change a rule here appends the change to this task under `## Design changelog`.
Until S6 the port is validated by one real wire and `echo`; S6 may amend it, recording the amendment.
The implementation plan splits these stages into pull-request-sized units and fixes their order inside a landing.
Between S2 and S5 the Claude harness imports the Anthropic recipe from core, because `bro-anthropic` does not exist yet; S5 repoints it.

#### 11.2 Versions that meet

Installations are replaced, not mixed (§1): a newer installation, frozen runtime or client configuration is not required to work beside an older one.
Four contracts meet across versions anyway, because they are persisted or deployed apart.

| Contract | Rule |
|---|---|
| Recorded trails | A record without `wire` is a Responses record (R2). The tag is an added row attribute: `TRAIL_FORMAT` does not change, so no stored header or row needs a migration, and a writer that predates the tag keeps writing records every server reads. |
| The hosted trails server | It stores rows verbatim whatever it installs and computes the projection when a trail is read, from the formats and wire decode sides it has. A server from before Landing 1 stores a chat record and projects its `llm_call` event alone, without content; a server from Landing 1 projects all three wires. The service runs two tasks under a rolling deploy, so both versions are live for a while and both accept every write; they differ only in the projection of a wire the older lacks, and no chat record exists before Landing 2's rollout. |
| The trails-server image | It is built from one declared set of distributions and checks them at startup (HP4). A set that leaves a format out fails the roll, the new task never starting, instead of passing `/health` and refusing every write. |
| Client configuration | `~/.bro.json` and the credential store's `defaults` name new kinds and keys (`harness`, `deepseek`, `mu`) only once every installation that reads them is re-synced, because an older installation refuses such a file. A key file of an unregistered kind is ignored and may be placed early. |

Roots are the other side of the same rule: a root runs the runtime it froze when it started, so stages summoned by a root from before a re-sync run the old runtime on the new tree, and work that needs the new runtime starts new roots.

#### 11.3 Rollout order

After Landing 1 merges:

1. Re-sync the installation that invokes `ride` (in this checkout `./setup.sh`), and set `harness` and `summon-harness` in `~/.bro.json` for repositories whose `[tool.bro]` names none (Q5).
2. Redeploy the hosted trails server from master.
   Its image carries the `bro` and `claude` formats and the `responses`, `messages` and `chat` decode sides (HP4).
   Confirm that a native and a Claude trail recorded through it project as before.
3. Start new roots, or resume the coordinator, from the re-synced installation.

After Landing 2 merges:

4. Re-sync again.
   Picks that name a new kind are written only now.
5. Place the DeepSeek key as `~/.bro/creds/deepseek.cred` (mode 0600, content `{"api_key": "<key>"}`, entered without echo: `read -rs` under `umask 077`), top up a small balance (DeepSeek is prepaid, so the balance is the spending cap),
   and accept DeepSeek's data terms for sessions routed to it:
   storage in the PRC and training use as its policy permits.
   The key never goes into the repository, the issue, a trail, a prompt, a summon pass or an `--env` flag.
6. Create `~/.bro/creds/mu.cred` as in §4.8, naming instances in its references (A8).
   Cast, source summaries and `look` are unavailable until it exists.
7. For a managed native launch of a persona without a recipe (Q1: `bro`, `bro-file-probe`, `bro-watch-probe`, `dev`, `eyebro`, `lead`),
   set `projects.<repo>.bros.<bro>.llm` in `~/.bro.json` (`["openai:terra:medium"]` is the old default) or pass `--llm`;
   `bro-dev` and `bro-eyebro` declare theirs.
8. Check the recipe: `ride scope --bro bro-dev --harness bro --llm deepseek:pro:high` prints `provider: deepseek` and `deepseek+: PRESENT`.
9. Start a fresh root and run the live probe (§8).
10. Run V (§8).

The probe comes before V because it checks the wire's mechanics in seconds, where V would meet a protocol mismatch inside a full stage.
Each step is undone by restoring what it replaced: the host checkout at the previous master commit and its sync, the previous trails-server image (every image is tagged with its commit) deployed again, the configuration edits reverted.
Rolling the server back after chat records exist only degrades their projection (11.2) and loses no row.
For the coordinator: run Landing 1's stages and then tell the operator that steps 1 to 3 are due;
run Landing 2's stages as ordinary stages (none needs the key, and S6's offline tests use fixtures and an in-test server) and then tell the operator that steps 4 to 9 are due;
run V from the fresh root with `llm: deepseek:pro:high`;
never ask for the key or move it through a summon, a pass or a prompt.

### 12. Decision record

Status `ratified` means the user ruled on it, in the discussion or in the review of this pull request;
`approved` means the design proposed it and the user approved it with the design on 2026-10-26;
`proposed` means the review of this pull request raised it, and it is settled when the pull request is approved;
`open` waits for the user.

| Decision | Choice | Status | Why |
|---|---|---|---|
| D1 selection | an explicit recipe, no preset, no new bro | ratified | §7 |
| D2 auxiliary calls | configured by the `mu` credential, with caller-chosen roles (§4.8) | ratified | the session sees only its hydrated store, so a credential carries host-chosen configuration into it; roles give callers the choice instances cannot |
| D3 cross-wire fork | refused (F2) | approved | converting history across wires meets the thinking-mode pass-back rule with foreign turns, unverified (X8) |
| D4 behavior changes | L6, L9 and the L8 cancellation window apply to every provider, OpenAI included | approved | a weaker model emits bad arguments more often; a filtered or truncated answer must not read as complete; an interrupt must not strand unanswered calls |
| D5 effort mapping | DeepSeek's own (xhigh → high) | approved | one vendor-defined scale, no invented levels |
| D6 prices | list (peak) rates as an upper bound | approved | the off-peak schedule depends on Chinese public holidays and the call time, which `price` does not receive |
| D7 reference page | `bro/reference/llm_providers.md` | ratified | §10 |
| D8 credential kinds | each provider's kind is contributed by its plug-in; `claude_code` by `bro-claude`; `mu` is a core kind | ratified | provider keys belong with the provider; `mu` is provider-neutral |
| D9 risk posture | DeepSeek stages hold the usual `bro-dev` credentials, mitigated per §9 | approved | the stage must push, so `github` cannot be dropped |
| D10 isolation | every provider is a plug-in, OpenAI and Anthropic included; `echo` belongs to native | ratified | core can name no provider |
| D11 ride | `ride scope` takes the LLM flags | approved | the only way to see a non-default provider's credential presence |
| D12 recipe presence | no framework default recipe; an absent recipe is supplied by the launch or the host's per-bro list and otherwise refused (Q1–Q4) | approved | a default would be the one provider name core could not drop |
| D13 policy test | `provider_policy_test` enforces N1–N3, with the dependency rule as N2 | approved | the isolation rule must hold as plug-ins are added; a dependency is the one fact that says which plug-in a distribution may name |
| D14 Claude-only build | not a requirement; its composition test is retired | ratified | |
| D15 wires | plug-ins, one distribution each with a decode side and a client side; the port in core; Anthropic's vocabulary a decode-only wire | ratified | a trail must be decodable with no SDK installed; one wire serves every provider that speaks it |
| D16 harnesses | plug-ins under `harnesses/`; `bro-claude` split from `bro-ride` and depending on `bro-anthropic`; `bro-ride` depends on no provider | ratified | the Claude harness serves one provider and is not part of the managed-workspace runtime |
| D17 trail formats | one per harness, a sibling of the harness distribution in `harnesses/<h>/` (D28), decoding payloads through wires | ratified | envelope belongs to the harness, payload to the wire |
| D18 no default harness | none; precedence launch, then project, then host config, else refused (Q5) | ratified in outline; the project layer and its rank approved | a default is a harness name in core |
| D19 directories | `llms/`, `wires/`, `harnesses/`; `filetypes/` later | ratified | "provider" already means a file-type provider |
| D20 untagged records | claimed by the wire declaring `LEGACY` (R2) | approved | keeps the legacy rule exact without a wire name in core |
| D21 usage and families | `from_wire_counts` by wire; `vendor_of` deleted; `model_family` provider-exported (U1, U5) | approved | the vendor guess has no caller; the vocabulary is the wire's |
| D22 wire clients | reached through core's roster (`bro.llm.wire_clients`, `wires.open`) by both the native harness and `mu` | approved | `mu` is core and needs a client without a harness |
| D23 recipe surface | the recipe carries `WIRE`, `ENDPOINT`, `request_options()`, `is_transient()` and `accepts()`; there is no separate client-factory group | approved | one object says what a provider needs from a wire |
| D24 one-shot stops | `LLMStopped` replaces `IncompleteResponse` and `TruncatedResponse` in `mu` | approved | nothing catches the old two; one stop vocabulary (U4) |
| D25 parse versus resolve | parsing a recipe checks grammar only; providers and models resolve when a launch reaches them (Q8) | approved | once providers are plug-ins the provider set differs between installations, and one unknown entry would otherwise fail every launch from the poorer one |
| D26 no early canary | DeepSeek is added regardless of a pre-check; its capability for #1086 is checked at V, and the reference page lists a canary as optional (§10) | ratified | the user wants the provider either way; a refusal at V is evidence for #1091 and the generalization stands |
| D27 empty arguments | an empty or whitespace-only argument string parses as `{}` (R3) | approved | DeepSeek's spelling of a no-argument call is unverified (X2), and `""` would otherwise turn every such call into an L6 error |
| D28 harness and format layout | a harness and its trail format are two distributions in `harnesses/<name>/`, at `harness/` and `trails/` | ratified | a uv workspace member cannot sit inside another member's directory (the pinned uv 0.13.1 stops its search for the workspace root at the enclosing project), so the nesting D17 first chose cannot be built |
| D29 landings | two: layout and decode (S1, S2), then behavior (S3 to S7); the hosted trails server is redeployed between them | ratified | the server must serve every wire before the first record of it is written, and the restructure is proven in use before any behavior changes |
| D30 chat decode side | created by S1 with the other two decode sides; the client side comes with S6 | proposed | Landing 1's server image must already decode chat |
| D31 capacity stop | the wire retries `insufficient_system_resource`, as the `capacity` stop: waits of 10 s doubling to 300 s, six retries, then L9 ends the run (U6) | ratified | the vendor documents it as an interruption by resource limits, of the family of the 500 and 503 that U3 retries; the schedule is the user's |
| D32 server guard | the trails-server image is built from one declared set of distributions, and the server exits at startup unless they load (HP4) | proposed | `/health` passes a server missing a format, which then refuses every write, and recording is mandatory |
| D33 tolerant rosters | every read of the provider roster skips a broken declaration; `LLMSpec.from_dict` resolves by `type`; `Harness.llm_providers` is computed; `NativeLLMSpec` is retired (P3) | proposed | a broken plug-in must break its own recipes only, and `load_all` over a closed table is the last place that names every provider |
| D34 preset rule | a one-word `--llm` value naming no installed provider is a preset (Q8) | proposed | grammar-only parsing makes every bare word a provider slot, which silently retires the preset fallback that rode on the roster check |
| D35 `mu` roles and contents | role `reader` replaces `vision`, because the page reader sends page text; contents: all seven classes ported as A3 lists them, or `Text` and `JSON` only, the five without callers (`JSONList`, `Image`, `ImageFile`, `PngImage`, `PDFFile`) returning with their first caller | role: proposed; contents: open | `Text` and `JSON` are the only classes with callers today; the image and PDF classes are what the files roadmap (#1047, #1048) will want |
| D36 benchmark and `mu` | a benchmark preset whose trials mount `cast` holds a `mu` instance (A4) | proposed | the trial store holds `openai` today, which mounts `cast`; with `mu` as the gate the trials would lose it |
| D37 references in `mu` material | material names the instance it references (A8) | proposed | a kind-only reference resolves against what the session holds, not against the operator's pick |
| D38 restore sources | a wire restores from the steps' own records, never from recorded requests (W6) | proposed | it is what the Responses replay does today; stating it fixes what a new wire may rely on |
| D39 host recipe list | the host `llm` list applies to managed launches only (Q2) | proposed | the authority reading reads it; an in-process `bro run` never did |
| D40 stopped legs | a leg that stops other than `completed` is recorded and projected with its stop but never enters the conversation: the wire does not advance its state, the exchange's inputs stay pending, and fork points and restores pass over it (W3, L9, F3, F4) | proposed | L9 records the stopped step so the trail shows it; without this rule a resume would replay a half-finished answer and a continued session would leave a tool call unanswered |

## Review record

This part is not part of the specification.

### Changes against the version approved on 2026-10-26

Ruled by the owner in this review: the layout of a harness and its trail format (D28), two landings with the hosted trails server redeployed between them (D29), the capacity retry and its schedule (D31),
and that old installations may break (§1, item 13).
The review verified the repository and the vendor's pages (§2) and made the changes below; each is open to objection.

- §2 gains the verified facts, drops X6 (the operator's trails are hosted) and widens X2 and X8; X9 and X10 are new.
- §3 gains `stop`, `capacity stop` and `landing`; the role `vision` is now `reader`.
- §4.1 and §5.1: the harness distribution sits in `harnesses/<name>/harness/` (D28).
- §4.3, §4.4 and §4.6: the capacity stop and its retry (W2, W3, W8, W9, L9, U4, U6; D31);
  U4 fails on a stop word it does not list;
  a stopped leg never enters the conversation (R3, R4, W3, L9, F3, F4; D40);
  W6 states what a restore reads (D38).
- §4.7: every read of the roster tolerates a broken declaration, `from_dict` resolves by `type`, `Harness.llm_providers` is computed and `NativeLLMSpec` is retired (P3; D33);
  the trails server declares its distributions and checks them at startup (HP4; D32).
- §4.8: the role `reader` (D35), a benchmark preset holds a `mu` instance (A4; D36), references name their instance (A8; D37).
- §4.10: the host recipe list is `projects.<repo>.bros.<bro>.llm` and applies to managed launches only (Q2, Q3; D39);
  a one-word `--llm` value that names no provider is a preset (Q8; D34).
- §5 and §11: S1 creates the `chat` decode side and S6 adds its client (D30).
- §6 and §8 gain the sites and the acceptance that the verification found;
  the operator steps of §7 moved to §11.3;
  §9 drops the script-rename risk, because Claude's settings are rebuilt at every launch.
- §11 is rewritten: stages and landings (11.1), the contracts that meet across versions (11.2) and the rollout order (11.3).
- §12 gains D28 to D40, and D17 follows D28.

Open for the owner: the contents choice of D35.
