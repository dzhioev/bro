# do/CLAUDE.md

Bros launching and managing layer — thin helpers that drive a `Bro` toward a specific outcome. Today it's two trivial wrappers; this package is the home for whatever orchestration shows up next.

## Files

- `do.py` (`do`) — `async do(bro, what)`; forwards to `bro.run(what)`. CLI: `do <bro-name> <what>`
- `do_task.py` (`do-task`) — `async do_task(bro, task_id)`; ask a bro to fix a flow task by id. CLI: `do-task <bro-name> <task-id>`

Both modules expose their function for library use (`from do.do import do`, `from do.do_task import do_task`) and as a CLI runnable via `python -m do.do` / `python -m do.do_task` or the registered console scripts; the CLI resolves the bro name through `bro.registry.get_bro`.
