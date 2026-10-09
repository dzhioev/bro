# dev tools reference

Shared behaviour for the dev MCP server's file and search tools.
Per-tool descriptions intentionally point here for the details.

## Output cap (`limit`)

Tools that return variable-length output (`read_file`, `grep`, and `glob`) take a `limit: int` parameter.

- **Default:** 100 lines, enough for most useful results without wasting tokens.
- **Maximum:** 2,000 lines;
  larger values are silently clamped.
- Every call is capped at about 30 KB too, whichever bound is reached first.
  A few very long lines therefore stop on bytes, and raising `limit` does not bypass that bound.

If a result exceeds the budget, the rest is dropped and announced inline.
To get more, raise `limit` up to 2,000 or narrow the query with an offset, path, or pattern.

## Skipped-content markers

Truncation markers report the dropped line and byte counts:

```
[...skipped before: 3,420 lines / 412.0 KB...]
... kept content ...
[...skipped after: 127 lines / 18.0 KB...]
```

These tools keep the head of their result, so dropped content normally appears in an `after` marker.
An offset can additionally produce a `before` marker.

## Timeout (`timeout_seconds`)

`grep` takes `timeout_seconds: int`, defaulting to 45.
On expiry its whole process group is killed, and the tool returns a `TIMED OUT` result.
Raise the bound when a very large search legitimately needs longer.

The in-process file tools (`read_file`, `write_file`, and `edit_file`) have no timeout.
They reject FIFOs, devices, sockets, and directories up front because opening those paths could block indefinitely.

## Fat-finger clamp

`limit > 2,000` is clamped to 2,000, and `limit < 1` is clamped to 1.
The clamp is announced in the relevant marker:

```
[...skipped after: 100 lines / 15 KB — limit 50,000 clamped to 2,000...]
```

When nothing was dropped, the marker carries only the clamp:

```
[...limit 50,000 clamped to 2,000...]
```
