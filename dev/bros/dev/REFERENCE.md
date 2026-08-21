# dev tools reference

Shared behaviour for the dev MCP server tools (`read_file`, `write_file`, `edit_file`, `bash`, `grep`, `glob`, `job`, `watch`, `kill`, `read_reference`).
Per-tool descriptions are intentionally terse and point here for the shared rules.

## Output cap (`limit`)

Tools that return variable-length output (`read_file`, `grep`, `glob`, `bash`, `watch`) take a `limit: int` parameter.

- **Default**: 100 lines. Fits most useful results without wasting tokens.
- **Maximum**: 2,000 lines. Larger values are silently clamped.
- Every call is capped at ~30 KB as well, whichever binds first: output of few but very long lines stops on bytes rather than on `limit`, and raising `limit` past that buys nothing.

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

## Timeout (`timeout_seconds`)

The shell-out tools (`bash`, `grep`) take a `timeout_seconds: int` parameter (default 45).

- On expiry the command's whole process group is killed — pipelines and any grandchildren die with it, so nothing is left running in the background.
- The tool returns a `TIMED OUT after Ns — killed.` result instead of output. If the command legitimately needs longer, re-run with a larger `timeout_seconds`.

The in-process file tools (`read_file`, `write_file`, `edit_file`) have no timeout — they can't be killed mid-call the way a subprocess can. Instead they refuse non-regular files (FIFO, device, socket, directory) up front, since those are the only inputs that could block them indefinitely.

## Fat-finger clamp

`limit > 2,000` is silently clamped to 2,000; `limit < 1` is clamped to 1. The clamp is announced inline on the relevant marker:

```
[...skipped after: 100 lines / 15 KB — limit 50,000 clamped to 2,000...]
```

If nothing was actually dropped, the marker collapses to just:

```
[...limit 50,000 clamped to 2,000...]
```

## Background jobs (`job`, `watch`, `kill`)

For long processes that outlive a `bash` call's timeout — test suites, PR watchers — run them as background jobs and read them iteratively.

`job(command)` starts `command` in the background (`bash -c`, stdout+stderr merged into one chronological stream) and returns a job id immediately. The stream spools continuously — a reader drains the pipe, so the job never blocks on unread output — and the record survives exit, so re-checks are repeatable. Jobs have no timeout; they run until they exit or are killed.

`watch(job_id, wait_seconds, limit, tail)` reads the job. Every return opens with a state line — `running` or `exited (code N)`. Two modes read through one per-job cursor, picked per call:

- **Incremental** (default): oldest-first pagination from the cursor. Pending output → returns immediately with the oldest `limit` lines; the cursor advances only past what was returned, and a `[...pending: N lines / X KB...]` marker announces the remainder — repeat `watch` to drain, nothing is dropped. Nothing pending and the job unfinished → blocks up to `wait_seconds` for new output or exit; a quiet window returns a bare state line as a heartbeat. Exited and fully drained → returns immediately.

- **Tail** (`tail=true`): for run-to-completion jobs. Wakes only on exit or the window's end, jumps the cursor to the spool end, and returns the last `limit` lines of the jumped-over section with a `[...skipped before...]` marker for its discarded middle — on exit the final diagnostics, on timeout a progress glimpse.

`wait_seconds` defaults to 10; `0` is a non-blocking poll. There is no upper bound — an iterative watcher (e.g. a PR watch loop) passes a large window explicitly and handles each return as one iteration.

`watch` is exclusive per job: the call holds the job for its whole wait, and a second concurrent `watch` on the same job fails immediately with the reason. Watches on different jobs run concurrently.

`kill(job_id)` terminates the job's whole process group — SIGTERM, escalating to SIGKILL after a 5s grace. It claims nothing: the exit it forces wakes a blocked `watch`. The record and spool stay readable afterwards for a final collect. Any jobs still running when the server process exits are group-killed.
