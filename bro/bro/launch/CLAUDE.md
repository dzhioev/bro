# do/CLAUDE.md

Bros launching and managing layer — thin helpers that drive a `Bro` toward a specific outcome. Today it's two trivial wrappers; this package is the home for whatever orchestration shows up next.

## Files

- `do.py` (`ask`) — `async do(bro, what)`; forwards to `bro.run(what)`. CLI: `ask <bro-name> <what>` (aliased to `ask` rather than `do` since `do` is a shell keyword; `do.do` also works)
- `do_task.py` (`do-task`) — `async do_task(bro, task)`; ask a bro to fix a flow task. The `task` arg is opaque (id, dashed UUID, Notion URL, or description); the bro's system prompt is responsible for normalising it. CLI: `do-task <bro-name> <task>`

Both modules expose their function for library use (`from do.do import do`, `from do.do_task import do_task`) and as a CLI runnable via `python -m do.do` / `python -m do.do_task` or the registered console scripts; the CLI resolves the bro name through `bro.registry.get_bro`.
