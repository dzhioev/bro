# LLM recipes and shared execution contracts

Provider-neutral declarations and the contracts shared by harness engines.
A persona names an `LLMSpec`;
`providers.py` resolves the launch grammar;
observers, trackers, usage records, and MCP live objects are the common seams engines consume.
The bro-native loop lives in `bro.native`; wire clients implement the core port.

## Design

- **Empty package hub.** `__init__.py` re-exports nothing, and consumers import submodules directly (`from bro.llm.llm import LLMSpec`).
  Records and wire decode sides stay SDK-free, so an environment without an engine can read recorded responses and usage;
  a hub re-export would put unrelated live dependencies on every import of this package.
- **Recipe boundary.** `llm.py`, `providers.py`, and `llms/` are declaration-side.
  Importing them may resolve and inspect every built-in recipe, but must not import `bro.native`, the OpenAI SDK, or the live MCP layer.
  `NativeLLMSpec` marks recipes the bro-native engine accepts;
  the native engine opens their wire through `wires.open`.
- **Tool declaration boundary.** Persona and toolset declarations import `bro.mcp`:
  its metadata, conditions, and factories do not import this package until a live server is built.
  `bro.llm.mcp` owns only the resulting tool/server execution path.

## Modules

- `llm.py` — the `LLMSpec` ABC:
  a frozen recipe (model + provider knobs) with `needed_secrets`, optional `.fast()` / `.with_effort(level)` transforms (a level from `EFFORT_LEVELS`, the neutral low/medium/high/xhigh/max vocabulary),
  and a `dump` / `from_dict` round-trip keyed by a `TYPE` discriminator.
  `WIRE` names the recipe’s recorded protocol.
  `selection` and `with_selection` project and reconstruct model/effort choices, with each recipe declaring its concrete effort vocabulary and deferrable fields.
  `NativeLLMSpec` is the marker for recipes the bro-native engine can run, each naming its model;
  a provider only a harness driving its own loop runs (`anthropic`) remains a bare `LLMSpec`.
  `LLMUnavailable` is the provider-neutral failure a client raises once its provider stays down through the client's retries.
  `LLMStopped` carries the neutral stop and the provider's own stop reason for an uncompleted leg.
- `providers.py` — the provider roster.
  `_PROVIDER_MODULES` maps a name to the module declaring its `LLMSpec`, `DEFAULT_MODEL`, `MODELS` short-name table, and `FAILURE_SIGNATURES`, imported per name.
  Model resolution tries the short name, then full id, then verbatim;
  the table is a convenience, not a whitelist, and a provider whose surface also takes names that are not model ids declares `MODEL_ID`, the shape a verbatim name must have.
  A model named without a provider resolves among the providers a harness serves (`bro.harness.Harness.llm_providers`):
  `carriers` and `provider_of_model` take that set, and `resolve` lays a selection over a standing recipe within it.
  `failure_signatures` exposes how a provider's client failures read in a run's error output, classified into `llm.py`'s neutral `FAILURE_CATEGORIES`.
  `price` prices one call from the provider's raw usage record and effective service tier.
  `price_table` exposes the current provider-owned table and its provenance.
  `LLMSelection` owns the `provider:model:effort` grammar with its `+fast` suffix, and `over` lays one selection over another
  — a launch's flags over a configured default.
  `LLMSelectionError` is the operator-facing failure.
  The flags themselves are `bro/launch/llm_flags.py`.
- `mu.py` — typed call/content convenience over the OpenAI Responses API.
  `mu(prompt, result, *contents)` blocks for the roundtrip;
  `mu.aio(...)` is the awaitable form used on agent loops.
  Both take `model` and `reasoning_effort`.
  A reply the model never finished raises `TruncatedResponse` or `IncompleteResponse`.
  `mu` reads the `openai` key itself, so its caller's component must declare that secret for host hydration.
- `openai_content.py` — Responses input-content conversion shared by `mu` and the Responses wire client.
  SDK imports stay inside conversion calls, so importing recipes remains SDK-free.
- `retry.py` — `retry_llm(request, transient)`, retrying through a provider outage until `RETRY_WINDOW_SECONDS` run out,
  then raising `llm.py`'s provider-neutral `LLMUnavailable`.
  The recipe classifies transient errors.
- `mcp.py` — the live tool layer.
  `Tool` / `MCPServer` ABCs, `FunctionTool`, and `ToolRegistry`, which assembles servers under `namespace__tool` wire names and dispatches calls.
  `encode_tool_result(dict | str)` returns compact JSON with non-ASCII characters unescaped, or the string unchanged.
  `Context[T]` is the per-call state envelope;
  `ToolControlSignal` escapes an agent loop instead of becoming a tool result.
  Rendering, validation, `InProcessMCPServer`, namespace wrappers, and canonical/wire-name conversion complete the serving path.
  Declaration objects and factories live in `bro.mcp` and import these live objects only when built.
- `cli_tool.py` — the build side of `bro.mcp.cli(...)`:
  one installed CLI command served as a generated tool in the `cli` namespace, with a schema derived from `bro.base.args.CommandSignature` plus a timeout, and a shell-free fixed argv.
- `observer.py` — the closed provider-neutral `ObservedEvent` union and `Observer.on_event(event)` sink.
  Providers emit reasoning, interim assistant text, background-job notifications, and call-ID-aware tool events;
  runners emit turn boundaries.
  `NullObserver` is the explicit no-op.
- `records.py` — SDK-free response meanings: ordered reasoning, messages, calls and unknown parts, with derived reply and terminal status.
- `wire.py` — the `Wire` client port, `Leg`, replay `Prefix`, and neutral inputs (`SystemPrompt`, `UserMessage`, `Notification`, `ToolOutput`).
  A leg carries the recorded exchange and its decoded parts, stop, reply and billed counts.
  `UnusableResponse` retains a received leg when reply extraction fails, so a caller can record and account for it before propagating the failure.
- `wires.py` — the `bro.llm.wires` decode roster and `bro.llm.wire_clients` client roster.
  Decode-side loading is lazy; a wire declaring `LEGACY` claims untagged records, and several claimants are an error.
  `open(recipe, secrets)` constructs the client named by `recipe.WIRE` from provider credential material, refusing request options that overlap the client's `OWNED` fields.
- `usage.py` — shared per-model usage accounting, the env-pointed usage file every harness publishes its cumulative usage through, footer formatting/parsing, and the `usage` CLI.
  `from_wire_counts(wire, raw)` normalizes raw usage through the wire roster.
- `tracker.py` — the dependency-free `Tracker` ABC, durable sibling of `Observer`, plus `NullTracker`.
  Trail records and harness recorders are owned by `bro/trails/`.
- `pricing.py` — canonical table digests and the once-per-process warning for a table read more than 30 days after its as-of date.
- `llms/` — provider recipes and provider-owned pricing:
  - `openai.py` — the OpenAI `NativeLLMSpec` plus standard, flex, and priority Responses pricing over raw token details and context bands,
    pricing a dated snapshot without a row of its own at its undated model's rates;
    the recipe carries model, reasoning effort, service tier, and compact threshold, with `needed_secrets` → `openai`, `.fast()` → priority tier, and every shared effort level mapped through.
    Its `request_options()` lowers those knobs; `is_transient()` classifies retryable errors.
    The client is `bro.wire_responses.client:Responses`.
  - `anthropic.py` — the model and knobs a Claude Code session runs under, as a recipe with no in-process client,
    plus Anthropic list pricing over transcript cache-write TTLs, a model's long-context tier, and the fast-mode rates a call's `speed` selects;
    session surfaces read `model` / `effort` / `fast_mode` and lower them onto Claude's own flags, a `model` or `effort` left None to the session's own default.
    `DEFAULT_MODEL` is a concrete id, bumped by hand.
  - `echo.py` — the dependency-free native recipe used by tests and the native LLM diagnostic CLI;
    its empty table prices no call, and its client and decode side are `bro.native.echo`.

## Conventions

- A new provider recipe lives in `llms/`, exports `LLMSpec` with `DEFAULT_MODEL`, a `MODELS` short-name table, `PRICE_TABLE`, `PRICE_TABLE_SHA256`, and `price`, and takes one row in `providers._PROVIDER_MODULES`.
  Its `TYPE` is the roster name.
  Price tables use the provider's raw billing vocabulary.
  Short names stay unique among the providers one harness serves.
- A recipe run by bro-native subclasses `NativeLLMSpec` and names a wire with a registered client.
  Recipe modules never construct clients; they expose `ENDPOINT`, `request_options()`, `is_transient()` and `accepts(part_kind)`.
- Importing a recipe module must stay cheap:
  every bro constructs its `LLMSpec` at class-definition time, so provider SDKs are never imported at module level there.
  OpenAI SDK enums used for validation are mirrored locally with a sync test pinning them to the SDK.
- A `Tool.call` returns `str` for unstructured output or a JSON-ready `dict` for structured output;
  `ToolControlSignal` escapes the native loop.
- Acronyms stay all-caps in identifiers (`LLMSpec`, `MCPServer`).
  Provider names are lowercase and hyphenated when needed (`openai`, `anthropic`, `echo`), and name the vendor, not the harness that serves it;
  only module filenames use underscores.
