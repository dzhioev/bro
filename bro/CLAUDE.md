# bro/CLAUDE.md

Bro is the agent system: independent specialised agents (a "Bro") each run as a stateless LLM loop with their own system prompt and MCP-tool access. Conceptual model and rationale: `DESIGN.md`. Run any script with `--help` for flags.

## Layout

- `bro.py` — `BaseBro` ABC (the framework) and the framework helpers (`Tool`, `ScatterTool`, `BroRaised`).

  Subclasses set `name`, `description`, and class-level `system_prompt = "..."` plus optionally `data_sources = [...]` and `mcp_servers = [...]`. Each `mcp_servers` entry is either:
  - an `MCPServer` instance (typically `flow.MCPServer(...)` / `infra.MCPServer(...)`), or
  - a `() -> MCPServer` factory for stateful servers that need per-instance materialisation.

  `BaseBro.__init__` walks the MRO from base to most-derived class and **concatenates each class's own `mcp_servers` and `system_prompt`** — so `class PPPDev(Dev): mcp_servers = [flow.MCPServer()]` declares only what PPPDev *adds*; Dev's own `mcp_servers` entry (the dev MCP server) flows through automatically (same for `system_prompt`). The legacy `super().__init__(system_prompt=...)` escape hatch remains for callers that need a dynamic prompt (e.g. PM injects current local time at instantiation).

  `BaseBro` also auto-prepends every `prompts/shared/*.md` to the system prompt and appends a `## Data sources` block describing each declared `DataSource`.

  **LLM spec.** `llm_spec` is a class-level attribute holding the bro's `LLMSpec` — a provider-specific frozen dataclass (e.g. `llm.llms.chat_gpt.LLMSpec` with typed `model`, `reasoning_effort`, `service_tier`) that carries the knobs the LLM accepts and validates them in `__post_init__`. Default is `chat_gpt.LLMSpec()` (gpt-5, no reasoning effort, no service tier); each bro overrides the whole spec at class level. Adding a new knob touches only the provider's spec, never `BaseBro`. Construction-time overrides go through `BaseBro.create(spec)` — it instantiates the class then replaces `llm_spec`, so subclass constructors never have to forward anything. Specs also expose `dump()` → dict / `LLMSpec.from_dict(data)` for serialization round-trip (used by `flow.process_inbox` to log the full spec into one DynamoDB column); `from_dict` eagerly imports the known providers so dispatch works in processes that haven't pulled the provider module in themselves.

  **Skills.** A bro can declare named procedures as markdown files under `<bro_pkg>/skills/*.md` with YAML frontmatter (`name`, `description`, body). `BaseBro.skills` walks `<pkg>/skills/` along the MRO and returns `{name: Path}`; derived classes override parents on name collision, parallel to `mcp_servers` / `system_prompt`. When skills are present, `__init__` auto-appends a `## Available skills` block to the system prompt and the service server mounts a `skill(name)` tool whose body returns the skill's markdown body for the LLM to follow. `get_skill_body(name)` and `skill_descriptions()` expose the same data programmatically for non-LLM consumers. Skill file format mirrors Claude Code's `SKILL.md` so the same file can be surfaced through both the bro framework and the Claude Code harness.

  **Interactive vs non-interactive paths.** Non-interactive runs (`bro.run()`, the `bro run` / `ask` / `do-task` CLIs, sub-Bros invoked via `Tool`) expose a built-in `raise` service tool — the agent calls it to abort with a reason when the request cannot be fulfilled (missing credentials, no appropriate tool, contradictory constraints, unclear/uninterpretable input); the call raises `BroRaised(reason)` out of `bro.run()`. Interactive paths (`bro.send()`, the HTTP server backing the iOS app, the `call` CLI) don't expose `raise` and inject a symmetric interactive-mode note instead — the agent is told to ask clarifying questions rather than guess, and to describe any blocker in its reply.

  **Tracing.** Non-interactive runs stream a live trace of reasoning summaries, tool calls, tool outputs, and assistant text to stderr — timestamped plain log lines via `llm.tracer.BoringTracer` by default, or colored `rich` panels via `RichConsoleTracer` when `--rich` is passed (`ask --rich`, `do-task --rich`, `bro run --rich`); both honor `tracer=` on `BaseBro.run` for explicit overrides. Subclasses override `_make_tracer()` to set the default tracer; test bros return `NullTracer()` for silence.

  Assistant text flows through `on_assistant_message(text, terminal)` with the flag distinguishing mid-stream chatter (interim, model is still calling tools) from the terminal reply (also returned from `LLM.send`). `BoringTracer` / `RichConsoleTracer` render both — terminal as `reply` (bright), interim as `assistant`. Callers that already render the return value themselves (e.g. `do.call`'s `TextTracer` / `TUITracer`) branch on `terminal=True` and skip to avoid double-emitting.

  Post-init injection of extra servers is available via `extend_mcp_servers(...)` for tests or unusual hosts.
- `bros/` — one package per concrete agent (`bros/<name>/__init__.py`); `bros/__init__.py:init()` registers each with the registry.

  `bros/bro/` defines the concrete `Bro(BaseBro)` — the "default bro" registered as `bro`, with a minimal go-to system prompt and no MCP servers. **All other bros inherit from this `Bro`** (not `BaseBro` directly), so they pick up the shared defaults via the MRO walk; inherit from `BaseBro` only when you want to opt out of those defaults.

  Specialists:
  - `assistant/` — chat + flow tools
  - `pm/` — Flow inbox triage
  - `librorian/` — steward of the Flow media library (adds, maintains, recommends)
  - `devoops/` — autonomous service deploys with a dry-run safety reflex; tools live in `infra/mcp.py`
  - `dev/` — generic developer with file/shell/search tools from sibling `mcp.py` (exposes `read_file`, `write_file`, `edit_file`, `bash`, `grep`, `glob`, `read_reference` as `FunctionTool`s; `bash` and `grep` shell out, file ops are thin Python; `MCPServer(*names)` is the `InProcessMCPServer` subclass — same shape as `flow.MCPServer` / `infra.MCPServer`; see sibling `REFERENCE.md` for the shared output cap / skipped-content markers / fat-finger clamp the LLM fetches via `read_reference`)
  - `ppp_dev/` — PPP-specific developer: inherits `dev/` + `/pr` + `/land`, adds the flow toolset and the `/fix` skill (task-driven workflow); `dive-in` seeds `/fix` as its first user message
- `datasources/` — `DataSource` ABC + connectors to read-only sources. The base ABC declares only `name`, `summary`, and `as_mcp_server()`; the `SearchableDataSource` subclass adds the canonical `search` + `fetch` pair and exposes them as `<name>-search` / `<name>-fetch` tools. Subclass `DataSource` directly for sources that don't fit the search/fetch shape and override `as_mcp_server()`. Currently:
  - `wikipedia.py` — Wikipedia REST API, query-aware fetch via `mu`
  - `tmdb.py` — movies + series; needs `.configs/tmdb.json`
  - `open_library.py` — books, no auth
  - `web_search.py` — Brave Search; needs `.configs/brave.json`
  - `current_time.py` — current local date and time; single `current-time-get-time` tool
  - `file.py` — `FileSource(name, summary, path)`: surface a static reference file as a single `<name>-read` tool. Use for canonical docs the bro consults on demand (e.g. PPPDev's `environment` source pointing at `prompts/environment.md`, the same playbook auto-injected into `cw ss` Claude Code sessions)
- `registry.py` — process-wide registry of bro classes: `register(cls)`, `get_class(name)`, `create_bro(name, llm_spec=None)`, `list_classes()`. `create_bro` returns a fresh instance every call. First lookup triggers `bros.init()`
- `run.py` (`bro`) — CLI: `bro run <name> <input>`, `bro list`, `bro show <name> [--system-prompt]` (markdown info card; renderer in `show.py`)
- `server/server.py` (`bro.server.server`) — aiohttp wrapper exposing the `assistant` bro on `POST /v1/chat/completions` (OpenAI-compatible). Used by the iOS app
- `app/ios/` — iOS chat client; see `app/ios/CLAUDE.md`

## PM Bro

`bros/pm/` is the single source of truth for Flow inbox triage policy — status taxonomy, importance/driver semantics, every per-kind policy (payment receipts, subscription renewals, bill payments, media items, title metadata extraction, sphere tags, …). Its `SYSTEM_PROMPT` is canonical: do not duplicate the policies elsewhere. Two delivery surfaces consume it:

- `process-inbox` TUI (`flow/process_inbox.py`) — Textual app; instantiates `PM()` with no MCP servers and constrains it to a JSON suggestion protocol so the user previews every change before it applies. Engine details in `flow/process_inbox.REFERENCE.md`
- `cw ss --bro pm` — Claude Code session with PM's system prompt and the flow MCP toolkit; PM calls `update_task` / `add_task` itself (no preview). Use when you want to triage from a chat UI rather than the TUI

Changes to triage policy go in `bro/bros/pm/__init__.py` and propagate to both surfaces on next process start.

## Adding a Bro

Create `bros/<name>/__init__.py` with `from bro.bros.bro import Bro` and a `class YourBro(Bro)` (inherit from the concrete `Bro` so you pick up the shared defaults; use `BaseBro` only when you want to opt out). Declare `name`, `description`, and `system_prompt` as class attributes; add tool sources as class attributes too:

- `system_prompt = "..."` — class-level. When you `class B(A)` and both declare `system_prompt`, `__init__` concatenates A's then B's (MRO base-to-derived) so subclasses only declare their *additions*.
- `data_sources = [YourSource()]` for read-only data connectors
- `mcp_servers = [flow.MCPServer()]` for the full flow toolset
- `mcp_servers = [flow.MCPServer('add_task', 'list_tasks')]` to scope to specific tools (validated at construction)
- `mcp_servers = [infra.MCPServer()]` for the devops toolset; same `*tool_names` API
- `mcp_servers = [MCPServer()]` (imported from `bro.bros.dev.mcp`) for the developer toolset (read_file/write_file/edit_file/bash/grep/glob, plus read_reference for the shared output cap / skipped-content / clamp rules)
- Stateful servers that need a fresh instance per Bro can pass a factory: `mcp_servers = [some_factory]` where `some_factory: () -> MCPServer`
- `mcp_servers` is also walked along the MRO and concatenated — `PPPDev(Dev)` with `mcp_servers = [flow.MCPServer()]` ends up with Dev's dev MCP server *plus* `flow.MCPServer()`.
- `llm_spec = chat_gpt.LLMSpec(...)` (or any other provider's `LLMSpec`) overrides the LLM recipe. Per-instance overrides go through `YourBro.create(spec)`.
- Drop markdown files into `bros/<name>/skills/*.md` (YAML frontmatter `name`, `description`, body) to declare skills the LLM can invoke via the `skill` service tool. Skills follow the same MRO walk as `system_prompt` and `mcp_servers`: each ancestor's `skills/*.md` files contribute, but because skills are keyed by name (not concatenated), derived classes override parents on name collision (`ppp_dev/skills/fix.md` would shadow a `dev/skills/fix.md` of the same name). Anything dropped into `bros/bro/skills/` becomes available to every bro by default — the shared-skill mechanism.

**Register the new bro manually** — import `YourBro` in `bros/__init__.py:init()` and append it to the list iterated there. There is no auto-discovery; the import + list entry is required for `create_bro('your-name')` to work.

## Adding a DataSource

Set `name` (slug) and `summary` (one-line; injected into the system prompt of every Bro that uses it). For the common search/fetch shape, subclass `SearchableDataSource` and implement `async search(query, limit) -> list[Hit]` and `async fetch(id, query=None) -> str`; `fetch` receives the original user query so the connector can return a focused summary (see `wikipedia.py` for the `mu`-based pattern). For other shapes (e.g. a singleton fact like `current_time.py`), subclass `DataSource` directly and override `as_mcp_server()` to expose whatever tools fit. When an upstream HTTP/network failure makes the source temporarily unusable, raise `bro.datasources.base.SourceUnavailable(source, reason)` rather than letting raw transport exceptions escape — the agent loop turns it into a tool result the model can route around. Bind to a Bro by declaring `data_sources = [YourSource()]` on its class.
