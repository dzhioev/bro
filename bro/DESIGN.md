# Bro Redesign

## What is a Bro

A Bro is an independent agent — a unit of automation with its own specialization,
tools, and system prompt. Running a Bro means feeding input through an agentic loop
(LLM → tool calls → repeat → final response). Each run is stateless: fresh LLM
instance, no conversation history carried over.

## Agent model

A Bro is a Python class:

```python
from bro.bro import Bro
from flow.mcp.bridge import create_flow_server


class InboxProcessor(Bro):
  name = 'inbox-processor'
  description = 'triages tasks in the Notion inbox'

  system_prompt = """
  You are an inbox processor. For each inbox item, ...
  """

  def mcp_servers(self):
    return [create_flow_server()]
```

### Base class

```python
class Bro(ABC):
  name: str                              # unique, kebab-case
  description: str                       # one line, shown in tool listings
  system_prompt: str                     # specialization instructions

  def mcp_servers(self) -> list[MCPServer]:
    """tool sources this Bro has access to; override to provide tools"""
    return []

  async def run(self, input: str) -> str:
    """single-turn: fresh LLM, returns the final text response"""

  async def send(self, message: str) -> str:
    """multi-turn: lazily creates and reuses an LLM across calls"""
```

`run()` is stateless — fresh LLM per call, no history carried over. `send()` is
multi-turn — it lazily creates an LLM on first call (with system prompt + user message)
and reuses it for subsequent calls (user message only). The underlying `ChatGPT` LLM
uses `_last_response_id` for conversation continuity via the OpenAI Responses API, so
previous context is available without re-sending the full message history.

### LLM backend

The initial implementation uses the existing `ChatGPT` LLM backend, which works with
any OpenAI-compatible completion API (GPT, Claude via OpenAI-compat endpoint, local
models, etc.). The agentic tool-use loop already lives in `ChatGPT.send()`. The Bro
layer doesn't touch it — it just composes system prompt + tools + input and delegates.

Model selection: `Bro` base class has a `model` property (default from config);
subclasses override when needed. The LLM backend receives the model name and uses it
in API calls.

### Parallel execution (map)

A Bro can process multiple inputs concurrently. Since `run()` is stateless (fresh LLM
per call, no shared state), cloning is trivial — just call `run()` N times in parallel:

```python
class Bro(ABC):
  async def run(self, input: str) -> str: ...

  async def map(self, inputs: list[str], max_concurrency: int = 5) -> list[str]:
    """run this Bro on each input concurrently; returns results in input order"""
    semaphore = asyncio.Semaphore(max_concurrency)

    async def bounded_run(input: str) -> str:
      async with semaphore:
        return await self.run(input)

    return await asyncio.gather(*[bounded_run(x) for x in inputs])
```

`max_concurrency` bounds parallel API calls (rate limits, cost). Each clone gets its
own LLM instance, tools, and context — no interference.

Example: an orchestrator fetches 10 inbox items, then fans out:

```python
items = await flow.peek_inbox_batch(limit=10)
processor = get_bro('inbox-processor')
results = await processor.map([item.description for item in items])
```

### Sub-agent (Bro as Tool)

A Bro can be exposed as a tool so other Bros can invoke it. Two tool shapes:

**`BroTool`** — single-input, synchronous from the caller's perspective:

```python
class BroTool(Tool):
  """wraps a Bro as a callable tool"""

  async def call(self, arguments: dict) -> str:
    return await self._bro.run(arguments['input'])
```

**`BroScatterTool`** — parallel map over multiple inputs:

```python
class BroScatterTool(Tool):
  """runs cloned Bros in parallel over a list of inputs"""

  @property
  def parameters(self) -> dict:
    return {
      'type': 'object',
      'properties': {
        'inputs': {
          'type': 'array',
          'items': {'type': 'string'},
          'description': 'list of inputs to process in parallel',
        },
      },
      'required': ['inputs'],
    }

  async def call(self, arguments: dict) -> str:
    results = await self._bro.map(arguments['inputs'])
    return json.dumps(results)
```

An orchestrator Bro composes specialists via both patterns:

```python
class Assistant(Bro):
  name = 'assistant'
  description = 'general-purpose assistant that delegates to specialists'

  def mcp_servers(self):
    from bro.registry import get_bro
    translator = get_bro('email-translator')
    return [
      create_flow_server(),
      InProcessMCPServer([
        BroTool(translator),               # translate one email
        BroScatterTool(translator),         # translate many in parallel
        BroTool(get_bro('inbox-processor')),
      ]),
    ]
```

The LLM decides which to use: single `BroTool` for one item, `BroScatterTool` when it
has a batch. Both are regular tools — the parallelism is invisible to the calling LLM.

## Data sources

A `DataSource` describes and provides a connector to a read-only data source (a book,
a database, a web reference like Wikipedia). It has three jobs:

1. **Self-description.** Each source has a `name` and a one-line `summary`. The Bro
   base class injects every source's summary into the system prompt under a `## Data
   sources` section so the LLM knows what is available without enumerating raw tools.
2. **Uniform query shape.** Two methods: `async search(query, limit) -> list[Hit]` and
   `async fetch(id, query=None) -> str`. The default `as_mcp_server()` exposes them
   as `<name>-search` / `<name>-fetch` tools. `fetch` receives the original user query
   so the connector can return a focused summary instead of the raw record.
3. **Read-only by contract.** Unlike a generic `MCPServer`, a `DataSource` never
   mutates state — so it is safe to bind to any Bro.

```python
class Librorian(Bro):
  name = 'librorian'
  description = 'research assistant that looks things up across read-only data sources'

# in bros/__init__.py
register(Librorian, data_sources=[Wikipedia()])
```

Concrete connectors live in `bro/datasources/`. The Wikipedia connector pulls the
article extract from the REST API, then runs `mu()` with a fine-tuned prompt to
return a query-biased summary — the LLM downstream sees a tight digest, not the
whole article.

## Registry

Bros live in `bro/bros/`, one file per agent. The registry discovers them at import
time — every `Bro` subclass in that package is registered by `name`.

```
bro/
  bro.py            # Bro base class, BroTool
  registry.py       # discover + get_bro(name)
  bros/
    __init__.py
    assistant.py    # default Bro for iOS app
    inbox_processor.py
    email_translator.py
    ...
```

`registry.py`:

```python
_REGISTRY: dict[str, Bro] = {}

def register(bro_cls: type[Bro]) -> None: ...
def get_bro(name: str) -> Bro: ...
def list_bros() -> list[Bro]: ...
```

Discovery: importing `bro.bros` triggers registration. Each Bro file ends with
`register(InboxProcessor)` (explicit, no magic).

## Execution layer

### a) Console

```
bro run <name> --input "process the inbox"
bro run <name> --interactive                 # REPL loop
bro list                                      # show registered Bros
```

CLI entry point: `bro/run.py` → console script `bro.run` (alias `bro`).

### b) Scheduled (cron)

Two options, not mutually exclusive:

1. **start-session**: `start-session inbox-processor` spins up a cw session that runs
   the Bro — already has tmux + container infra
2. **AWS Step Functions / EventBridge**: schedule `bro run <name>` as a periodic task
   in the existing infra stack

Cron-triggered Bros must be fully autonomous (no interactive input). The Bro's
`system_prompt` encodes what to do; `run()` receives a fixed input like "go" or a
timestamp.

### c) By other Bros (sub-agent)

Two tool shapes (see Sub-agent section above):

- **`BroTool`** — single input, blocks until the sub-Bro finishes. The calling LLM
  uses this when it has one item to delegate.
- **`BroScatterTool`** — parallel map. The calling LLM passes a list of inputs; the
  sub-Bro is cloned and runs all inputs concurrently (bounded by `max_concurrency`).
  Blocks until all clones finish, returns all results.

Both are regular tools — the calling Bro doesn't need to know about the parallelism.
Recursion depth: not explicitly limited, but practically bounded by token budgets.

### d) HTTP server (iOS app)

The existing `bro/server/server.py` continues to serve `/v1/chat/completions`. Changes:

- The server instantiates the **default Bro** (assistant) per request instead of a raw
  LLM. The Bro's system prompt and tools are baked in.
- Each request creates a fresh LLM via `bro._create_llm()` and calls `llm.send()`.
  For multi-turn conversation with `_last_response_id` continuity, use `Bro.send()`
  programmatically (the HTTP server can adopt this in a future iteration with
  per-session Bro instances).
- Optional: `?bro=<name>` query parameter to target a specific Bro.

The iOS app doesn't change — it still sends OpenAI-format requests to the same
endpoint and gets OpenAI-format responses.

## Concrete Bros (initial set)

| Name | Purpose | Tools | Trigger |
|------|---------|-------|---------|
| `assistant` | general-purpose chat (iOS app default) | flow, sub-agent Bros | HTTP |
| `inbox-processor` | triage Notion inbox | flow | cron, console |
| `email-translator` | translate emails | flow, gmail | sub-agent, console |

More Bros from the backlog (future):
- `telegram-agent` — handle Telegram messages
- `writing-coach` — learn and apply personal writing style
- `report-generator` — create productivity reports

## Changes to existing code

### `llm/llm.py`
- `LLM` ABC: replaced `tell()` + `ask()` with single `send(messages) -> str`
- `get_llm()` stays as-is

### `llm/llms/chat_gpt.py`
- Implements `send()` with `_last_response_id` tracking for conversation continuity
  via OpenAI Responses API `previous_response_id`
- Accept `model` parameter (default `'gpt-5'`)
- Accept optional `base_url` for OpenAI-compat endpoints (Claude, local models)

### `bro/server/server.py`
- Replace raw `get_llm()` factory with Bro-based factory
- The server gets the default Bro's system prompt and tools from the Bro instance

### New files
- `bro/bro.py` — `Bro` base class, `BroTool`
- `bro/registry.py` — discovery and lookup
- `bro/run.py` — CLI entry point
- `bro/bros/` — individual Bro definitions

### pyproject.toml
- New console scripts picked up by `sync-scripts` (bro.run, individual Bros)

## Implementation plan

### Phase 1 — core (this task)

1. `bro/bro.py` — `Bro` base class with `run(input)`, `map(inputs)`, `BroTool`, `BroScatterTool`
2. `bro/registry.py` — register / get_bro / list_bros
3. Make `ChatGPT` model configurable (constructor arg instead of hardcoded `'gpt-5'`)
4. `bro/bros/assistant.py` — default Bro with flow tools
5. `bro/run.py` — CLI: `bro run <name> --input "..."` and `bro list`
6. Update `bro/server/server.py` to use the default Bro
7. Tests

### Phase 2 — first specialist Bros

1. `bro/bros/inbox_processor.py` — port the inbox processing skill
2. Wire `assistant` to delegate to `inbox-processor` via `BroTool`
3. Cron scheduling for inbox processor

### Phase 3 — execution features

1. `--interactive` REPL mode in `bro run`
2. `base_url` support in `ChatGPT` for Claude/local model endpoints
3. HTTP server: `?bro=<name>` routing

### Phase 4 — more Bros

1. `email-translator`
2. `telegram-agent`
3. Others from the Bro project backlog

## Open questions

1. **Config**: should there be a central config for model defaults and API keys per
   backend, or does each Bro manage its own? (Lean: central config in `.configs/bro.json`,
   Bro overrides where needed.)
2. **Streaming**: the current `send()` pattern is non-streaming. The iOS app
   doesn't support streaming either. Defer streaming to a future phase?
3. **Error handling in sub-agents**: if a sub-Bro fails (tool error, LLM refusal), how
   should the calling Bro see it? (Lean: return error text as the tool result, let the
   caller handle it.)
4. **Token budgets**: should Bros have configurable max_tokens? (Lean: yes, as a base
   class property with sensible defaults.)
