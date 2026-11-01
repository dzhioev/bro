# LLM recipes and shared execution contracts

Provider-neutral declarations and the contracts shared by harness engines.
Provider declarations export `Recipe`;
`providers.py` resolves the launch grammar;
observers, trackers, usage records, and MCP live objects are the common seams engines consume.
The bro-native loop lives in `bro.native`; wire clients implement the core port.

## Design

- **Empty package hub.** `__init__.py` re-exports nothing, and consumers import submodules directly (`from bro.llm.llm import Recipe`).
  Records and wire decode sides stay SDK-free, so an environment without an engine can read recorded responses and usage;
  a hub re-export would put unrelated live dependencies on every import of this package.
- **Recipe boundary.** `llm.py` and `providers.py` are declaration-side and import no SDK, harness or live MCP layer.
  Provider declarations live in the distributions under top-level `llms/`; their package hubs and credential modules import nothing.
  A native recipe names a wire with an installed client.
  The Responses and Chat wires provide clients; Messages is decode-only.
- **Tool declaration boundary.** Persona and toolset declarations import `bro.mcp`:
  its metadata, conditions, and factories do not import this package until a live server is built.
  `bro.llm.mcp` owns only the resulting tool/server execution path.

## Modules

- `llm.py` — the `Recipe` base:
  a frozen recipe with a required model, provider-native fields and framework flags, and one field-driven `dump` / `from_dict` round-trip keyed by a `TYPE` discriminator.
  Field types and writable values are validated by the base; providers add combination checks.
  Records require `type` and `model`, omit unset `passthrough`, use defaults for missing fields and refuse unknown keys.
  Reading a record does not check admission.
  `WIRE` names the recipe's recorded protocol.
  `LLMUnavailable` is the provider-neutral failure a client raises once its provider stays down through the client's retries.
  `LLMStopped` carries the neutral stop and the provider's own stop reason for an uncompleted leg.
- `providers.py` — the provider roster.
  `bro.llm.providers` entry points name declaration modules exporting `Recipe` and `FAILURE_SIGNATURES`.
  `parse_recipe`, `bind_recipe` and `format_recipe` own complete recipe text, typed binding and canonical formatting; their operator-facing failures are `RecipeError`.
  `RETIRED_MODELS` holds dates beside each class's `MODEL_INPUTS`; price rows cover exactly both tables, and a refusal names a retired model's date.
  Declaration loading checks these tables and the field schema.
  `known_names()` reads sorted metadata without imports; `declaration(name)` loads and validates only the named provider.
  Searches warn and skip broken declarations; an explicit selection fails by name.
  `require_admitted` checks recipes bound from text and every recipe entering a run, including one rebuilt from a record.
  `failure_signatures` exposes how a provider's client failures read in a run's error output, classified into `llm.py`'s neutral `FAILURE_CATEGORIES`.
  `price` prices one call from the provider's raw usage record and effective service tier.
  `price_table` exposes the current provider-owned table and its provenance.
  The shared `--llm` flag is `bro/launch/llm_flags.py`.
- `mu.py` — typed one-shot calls configured by the `mu` credential.
  `mu(instructions, result, *content, role=None)` blocks; `mu.aio(...)` is awaitable.
  The material's default recipe and credential, or the named role's pair, open a wire through `wires.open` and call `structured`.
  Content is the wire port’s `Text` and `File`; see `bro/reference/mu.md` (man topic `mu`).
  Uncompleted replies raise `LLMStopped`.
  Operator configuration: `bro/reference/permissions.md`, "Auxiliary LLM credentials".
- `resolved.py` — `ResolvedRecipe`, the recipe-and-source record, its JSON and environment boundary, and canonical display.
- `retry.py` — `retry_llm(request, transient)`, retrying through a provider outage until `RETRY_WINDOW_SECONDS` run out,
  then raising `llm.py`'s provider-neutral `LLMUnavailable`.
  The recipe classifies transient errors.
  An optional `capacity` predicate also retries refused responses on a bounded doubling schedule; `on_capacity` receives each response about to be retried.
  The schedule restarts for every call, logs each wait, and cancellation ends a wait without another attempt.
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
  `Text` and `File` are validated content items; `EXTENSION_TYPES` types file paths and `require_accepted` enforces the model gate.
  A leg carries the recorded exchange and its decoded parts, stop, reply and billed counts.
  `UnusableResponse` retains a received leg when reply extraction fails and explicitly reports whether the wire accepted the exchange.
  A rejected exchange retains pending inputs and is recorded with `stopped: failed`; an accepted one acknowledges its inputs.
- `wires.py` — the `bro.llm.wires` decode roster and `bro.llm.wire_clients` client roster.
  Decode-side loading is lazy; a wire declaring `LEGACY` claims untagged records, and several claimants are an error.
  `open(recipe, secrets)` constructs the client named by `recipe.WIRE` from provider credential material, refusing request options that overlap the client's `OWNED` fields.
- `usage.py` — shared per-model usage accounting, the env-pointed usage file every harness publishes its cumulative usage through, footer formatting/parsing, and the `usage` CLI.
  `from_wire_counts(wire, raw)` normalizes raw usage through the wire roster.
- `tracker.py` — the dependency-free `Tracker` ABC, durable sibling of `Observer`, plus `NullTracker`.
  Trail records and harness recorders are owned by `bro/trails/`.
- `pricing.py` — canonical table digests and the once-per-process warning for a table read more than 30 days after its as-of date.

## Conventions

- Provider and wire adoption, declaration contracts, client extras and isolation: `bro/reference/llm_providers.md` (man topic `llm-providers`).
- A harness serves a recipe according to its own capabilities; the native harness uses installed wire clients.
- Price tables use the provider's raw billing vocabulary and record source and effective date.
- Knob and model validation belongs to the provider declaration, not the roster or a caller.
