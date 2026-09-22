# Data sources

`DataSource` ABC + `SourceUnavailable` (`base.py`) + connectors to read-only sources.
The base ABC declares `name`, `summary`, `namespace` (a property, `f'{name}-source'`), `needed_secrets` / `optional_secrets`, and `as_mcp_server()`.
The `SearchableDataSource` subclass (`searchable.py`) adds the canonical `search` + `fetch` pair, exposed as bare `search` / `fetch` tools inside the source's `<name>-source` namespace (wire name `<name>-source__search`).
Subclasses implement `search` and `_fetch_content(id)` (raw record);
the base `fetch(id, query=None)` returns the raw record when no query is given and otherwise summarises it for the query via `mu` (one `source_summary` prompt for every source).
That summary path reads the LLM key, so `SUMMARY_SECRET='openai'` is a base-level `optional_secret` (best-effort, hydrated when present);
with it absent, a non-null `query` raises rather than silently returning raw text.
The fetch tool's description advertises which mode is live via a `#features` directive (`bro/AGENTS.md`, "Optional credential tier").
Subclass `DataSource` directly for sources that don't fit the search/fetch shape and override `as_mcp_server()`, stamping `self.namespace` onto the returned server.
Adding one: `bro/reference/extending.md`, "Adding a data source".

## Modules

- `http.py` — shared HTTP transport for the sources (`get_json` / `get_text`):
  throwaway per-request sessions with a tight timeout and the common User-Agent
- `wikipedia.py` — Wikipedia REST API;
  `_fetch_content` returns the article extract (base layers the query summary)
- `web_search.py` — Brave Search;
  needs the `brave` secret
- `current_time.py` — current local date and time;
  single `get_time` tool (wire `current-time-source__get_time`)
- `references.py` — the core reference docs as ready-made instances:
  one `FileSource` per doc (`environment` → `bro/prompts/environment.md`;
  `template` / `conditions` / `ride` / `dive-in` / `extending` → the same-named `bro/reference/*.md`), plus `man('<topic>')`, which resolves a topic against that roster into a `ManPage` declaration.
  A bro lists whichever shape fits in `data_sources`;
  `bro-dev` contributes its development-style source separately
- `file.py` — `FileSource(name, summary, path, render=True)`:
  surface a static reference file as a single `read` tool (wire `<name>-source__read`) whose description carries the summary.
  One rendering is read by every harness, so the body must be surface-neutral
  — `read` renders with no facts and a `#harness` directive raises;
  `render=False` serves the file verbatim, for a doc whose payload is the directive syntax itself.
  Use for canonical docs the bro consults on demand;
  the ready-made instances live in `references.py`
- `man.py` — `ManSource(name, summary, pages)`:
  a roster of `FileSource` pages served as one `read(topic, offset=0)` tool (wire `<name>-source__read`), the unix `man` shape
  — a doc is declared once and served either as its own dedicated tool or as a topic here.
  `ManPage` is the declaration-side entry a bro lists in `data_sources`, one per topic;
  `BaseBro` folds every one it selects into a single `ManSource` via `manual(...)` (repeats collapsing), so classes across an MRO contribute pages to one manual instead of colliding on its namespace.
  The tool description carries the roster with each page's summary and the `topic` parameter its enum, so a surface seeing only the tool listing knows what can be read;
  lookup is case- and whitespace-tolerant and a miss raises with the topics listed.
  Output is capped at `PAGE_LIMIT` lines, so a page longer than that is read across successive `offset`s
