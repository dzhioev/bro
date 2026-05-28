# do/CLAUDE.md

Bros launching and managing layer — thin helpers that drive a `Bro` toward a specific outcome. Each file is a single-purpose CLI wrapper; this package is the home for whatever orchestration shows up next.

## Files

- `do.py` (`ask`) — `async do(bro, what)`; forwards to `bro.run(what)` (one-shot). CLI: `ask <bro-name> <what>` (aliased to `ask` rather than `do` since `do` is a shell keyword; `do.do` also works)
- `call.py` (`call`) — entry point for an interactive chat with a bro.

  By default opens the Textual chat UI in `call_tui.py` (IM-style: scrollable history, left/right bubbles, timestamp + date separators, animated "Typing…", Ctrl+D to quit, backtick (`) opens a stats modal). Falls back to raw mode (`[HH:MM:SS] bro: <reply>` lines + `> ` prompt) when stdin/stdout isn't a TTY; `--raw` forces it. The raw REPL helper `call_raw(bro, initial)` is library-callable.

  The Bro's `interactive=True` machinery picks up automatically in both modes: no `raise` tool, symmetric interactive-mode note injected into the system prompt.
- `call_tui.py` — Textual `ChatApp` plus its widgets (`MessageBubble`, `BubbleRow`, `DateSeparator`, `TypingIndicator`, `StatsScreen`). Mounts an `Input` on the bottom and a `VerticalScroll` of bubbles above. `bro.send()` runs in a Textual `@work(exclusive=True)` so the UI stays responsive while the LLM responds
- `do_task.py` (`do-task`) — `async do_task(bro, task)`; ask a bro to fix a flow task. The `task` arg is opaque (id, dashed UUID, Notion URL, or description); the bro's system prompt is responsible for normalising it. CLI: `do-task <bro-name> <task>`

All modules expose their function for library use (`from do.do import do`, `from do.call import call_raw`, `from do.do_task import do_task`) and as a CLI runnable via `python -m do.<module>` or the registered console scripts; CLIs resolve the bro name through `bro.registry.get_bro`.

## Container isolation

`ask` / `do-task` re-exec the bro in a throwaway `cw -c`-style container (workspace `var/cw/containers/<cli>-<bro>-<hex>/`, dropped on exit) so the bro's tools operate on `/workspace`, not host paths. `CW_IN_CONTAINER=1` short-circuits the hop; `--no-container` opts out for host-side debugging. The launch primitive is `cw.run_in_container`. Interactive `call` runs in the calling process — it needs a real TTY.
