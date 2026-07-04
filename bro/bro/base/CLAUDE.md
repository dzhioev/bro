# base/CLAUDE.md

Low-level shared utilities used across the whole repo. Some modules (`base.args`, `base.log`) happen to import no third-party package at load time, so consumers that run outside the venv (e.g. `setup/claude_commit_footer.py` from the post-commit git hook) can import them. Some modules expose a CLI (`base.credentials`, `base.time-util`); run those with `--help` for flags.

## Modules

- `args.py` — `Parser` (subclass of `argparse.ArgumentParser`): the one place in the repo that imports `argparse`. Adds repo-wide global flags (`--verbose`, `--ic`, `--allow-env`, `--print-env`), per-flag env-var overrides, mutually-exclusive group declarations, `dispatch()` for subcommand handlers registered via `set_handler`, and `reconstruct()` (namespace → canonical argv). `parse(argv)` is the entry every CLI calls — see the "CLI relationship" below.
- `credentials.py` — client-side secret resolver (`__cli_name__ = 'credentials'`). `get` / `get_json` / `try_get` / `available` resolve a named secret against an ordered `Source` list (`local`, searching `<project>/.configs` then `~/.ppp`; `ssm`, reading an AWS SSM parameter from the region the source names); a generated `credentials.json` overrides the built-in registry, and `CREDENTIALS_REGISTRY=<file>` overrides both for one process. `build_scoped_store` emits an in-memory per-container store (`cw` `docker cp`s it in), `apply_grant_revoke` layers per-session `--grant`/`--revoke` onto it, and `install_hooks` emits the shell wiring for secrets a tool reads from outside the resolver (git, the aws CLI). Schemas live in `setup/CLAUDE.md`.
- `spawn.py` — `run` / `popen` wrappers that detach every child into a fresh session (`start_new_session=True`, stdin `/dev/null`) so an interactive `/dev/tty` open fails instead of blocking; `run` also SIGKILLs the whole process group on timeout. Used by every agent shell-out.
- `log.py` — module-level `logging` to stderr (`debug` / `info` / `warning` / `error` / `exception`), tagging each record with the caller's module as `scope`. Info level on by default; `--verbose` raises it to debug.
- `time_util.py` — timezone-aware `Moment` / `Duration` (`datetime` / `timedelta` subclasses), parsers (`parse_moment`, ISO, date), and `utc_now`. Local tz is `Europe/Nicosia`. Prints the current time as a CLI.
- `name_map.py` — `NameMap`: case-insensitive, whitespace-tolerant name → value lookup (strip + casefold, exact match only). For matching a free-form name an LLM or human emits against a known set; collisions and misses raise with the available names listed.
- `text_window.py` — windowed views over large text for tool output: `apply_limit` caps to a line + byte budget (keeping head or tail) with inline `[...skipped before/after...]` markers and a fat-finger clamp; `numbered_window` layers a cat -n-numbered partial read (0-based `offset`) on top. `DEFAULT_LIMIT` / `MAX_LIMIT` are the shared cap policy.
- `yesno.py` — `yesno(question, default)` interactive y/n prompt.
- `project_root.py` — `PROJECT_ROOT`, the repo root as a `Path`.

## CLI relationship

Every CLI in the repo is a bare `def main(argv)` whose body builds a `base.args.Parser` and ends in `return fn(**parser.parse(argv))` or `return parser.dispatch(argv)`. The global-argv read happens once, in a generated `_entrypoints.py` shim, not in `main` — see the root `CLAUDE.md` "Commands" section and `template.py`.
