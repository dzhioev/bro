# LLM recipes and shared execution contracts

Provider-neutral declarations and the contracts shared by harness engines. A persona names an `LLMSpec`; `providers.py` resolves the launch grammar; observers, trackers, usage records, and MCP live objects are the common seams engines consume. The bro-native engine and provider clients live in `bro.native`.

## Design

- **Empty package hub.** `__init__.py` re-exports nothing, and consumers import submodules directly (`from bro.llm.llm import LLMSpec`). `usage.py` reaches no further than `bro.base` and `bro.monitor`, which lets an environment without an engine read a usage record; a hub re-export would put unrelated live dependencies on every import of this package.
- **Recipe boundary.** `llm.py`, `providers.py`, and `llms/` are declaration-side. Importing them may resolve and inspect every built-in recipe, but must not import `bro.native`, the OpenAI SDK, or the live MCP layer. `NativeLLMSpec` marks recipes the bro-native engine accepts; `bro.native.providers` owns mapping those recipes to clients.
- **Tool declaration boundary.** Persona and toolset declarations import `bro.mcp`: its metadata, conditions, and factories do not import this package until a live server is built. `bro.llm.mcp` owns only the resulting tool/server execution path.

## Modules

- `llm.py` — the `LLMSpec` ABC: a frozen recipe (model + provider knobs) with `needed_secrets`, optional `.fast()` / `.with_effort(level)` transforms (a level from `EFFORT_LEVELS`, the neutral low/medium/high/xhigh/max vocabulary), and a `dump` / `from_dict` round-trip keyed by a `TYPE` discriminator. `NativeLLMSpec` is the marker for recipes the bro-native engine can run; a provider whose harness drives its own loop (`claude_code`) remains a bare `LLMSpec`.
- `providers.py` — the provider roster: `_PROVIDER_MODULES` (name → the module declaring its `LLMSpec` / `DEFAULT_MODEL` / `MODELS` short-name table, imported per name), model resolution (short name, then full id, then verbatim — the table is a convenience, not a whitelist), `provider_of_model`, and `LLMSelection`: the `provider:model:effort` grammar with its `+fast` suffix. `LLMSelectionError` is the operator-facing failure. The flags themselves are `bro/launch/llm_flags.py`.
- `mu.py` — typed call/content convenience over the OpenAI Responses API. `mu(prompt, result, *contents)` blocks for the roundtrip; `mu.aio(...)` is the awaitable form used on agent loops. Both take `model` and `reasoning_effort`. A reply the model never finished raises `TruncatedResponse` or `IncompleteResponse`. `mu` reads the `openai` key itself, so its caller's component must declare that secret for host hydration.
- `openai_content.py` — OpenAI input-content conversion shared by `mu` and the native OpenAI client. SDK imports stay inside conversion calls, so importing recipes remains SDK-free.
- `mcp.py` — the live tool layer. `Tool` / `MCPServer` ABCs, `FunctionTool`, and `ToolRegistry`, which assembles servers under `namespace__tool` wire names and dispatches calls. `Context[T]` is the per-call state envelope; `ToolControlSignal` escapes an agent loop instead of becoming a tool result. Rendering, validation, `InProcessMCPServer`, namespace wrappers, and canonical/wire-name conversion complete the serving path. Declaration objects and factories live in `bro.mcp` and import these live objects only when built.
- `cli_tool.py` — the build side of `bro.mcp.sh(...)`: one installed CLI command served as a generated tool in the `sh` namespace, with a schema derived from `bro.base.args.CommandSignature` and a shell-free fixed argv.
- `observer.py` — the closed provider-neutral `ObservedEvent` union and `Observer.on_event(event)` sink. Providers emit reasoning, interim assistant text, and call-ID-aware tool events; runners emit turn boundaries. `NullObserver` is the explicit no-op.
- `usage.py` — shared per-model usage accounting, bro-run publishing and Claude transcript discovery, footer formatting/parsing, and the `usage` CLI. This module speaks of **vendors**, not providers: `vendor_of(slug)` answers who billed a model, while `providers.py` answers which launch recipe runs it.
- `tracker.py` — the dependency-free `Tracker` ABC, durable sibling of `Observer`, plus `NullTracker`. Trail records and harness recorders are owned by `bro/trails/`.
- `llms/` — provider recipes:
  - `openai.py` — the OpenAI `NativeLLMSpec`: model, reasoning effort, service tier, and compact threshold; `needed_secrets` → `openai`, `.fast()` → priority tier, and every shared effort level maps through. The live Responses client is `bro.native.llms.openai`.
  - `claude_code.py` — the model and knobs a Claude Code session runs under, as a recipe with no in-process client. Session surfaces read `model` / `effort` / `fast_mode` and lower them onto Claude's own flags.
  - `echo.py` — the dependency-free native recipe used by tests and the native LLM diagnostic CLI; its client is `bro.native.llms.echo`.

## Conventions

- A new provider recipe lives in `llms/`, exports `LLMSpec` with `DEFAULT_MODEL` and a `MODELS` short-name table, and takes one row in `providers._PROVIDER_MODULES`. Its `TYPE` is the roster name. Short names stay unique across providers.
- A recipe run by bro-native subclasses `NativeLLMSpec` and has a matching client module in `bro.native.llms`, registered in `bro.native.providers`. Recipe modules never construct clients.
- Importing a recipe module must stay cheap: every bro constructs its `LLMSpec` at class-definition time, so provider SDKs are never imported at module level there. OpenAI SDK enums used for validation are mirrored locally with a sync test pinning them to the SDK.
- A `Tool.call` returns `str` for unstructured output or a JSON-ready `dict` for structured output; `ToolControlSignal` escapes the native loop.
- Acronyms stay all-caps in identifiers (`LLMSpec`, `MCPServer`). Provider names are lowercase and hyphenated when needed (`openai`, `claude-code`, `echo`); only module filenames use underscores.
