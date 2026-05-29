# dev tools reference

Shared behaviour for the dev MCP server tools (`read_file`, `write_file`, `edit_file`, `bash`, `grep`, `glob`, `read_reference`).
Per-tool descriptions are intentionally terse and point here for the shared rules.

## Output cap (`limit`)

Tools that return variable-length output (`read_file`, `grep`, `glob`, `bash`) take a `limit: int` parameter.

- **Default**: 100 lines / ~15 KB. Fits most useful results without wasting tokens.
- **Maximum**: 2,000 lines / ~300 KB. Larger values are silently clamped.
- Byte budget per call = `limit * 150` (rough per-line average).

If a result exceeds the budget, the rest is dropped and announced inline via markers (see below). To get more, either:

- raise `limit` (up to 2,000), or
- narrow the query (e.g. pass a glob, narrower path, use `offset`, scope the regex).

## Skipped-content markers

Truncation is announced inline. Markers report both line and byte counts of what was dropped:

```
[...skipped before: 3,420 lines / 412.0 KB...]
... kept content ...
[...skipped after: 127 lines / 18.0 KB...]
```

- `before` markers appear when content was dropped from the head — e.g. `read_file(offset=…)`, or `bash` (which keeps the tail).
- `after` markers appear when content was dropped from the tail — e.g. `grep`, `glob`, `read_file` (which keep the head).
- Sides with nothing dropped emit no marker (no `0 lines skipped` noise).

`bash` keeps the **tail** (shell diagnostics live at the end — final error, exit message). Other tools keep the **head**.

## Fat-finger clamp

`limit > 2,000` is silently clamped to 2,000; `limit < 1` is clamped to 1. The clamp is announced inline on the relevant marker:

```
[...skipped after: 100 lines / 15 KB — limit 50,000 clamped to 2,000...]
```

If nothing was actually dropped, the marker collapses to just:

```
[...limit 50,000 clamped to 2,000...]
```
