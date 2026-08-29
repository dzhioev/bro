# Shared low-level utilities

Used across the whole repo.
Some modules (`bro.base.args`, `bro.base.log`) happen to import no third-party package at load time, so consumers that run outside the venv (e.g. `bro.workflow.commit_footer` from the commit git hooks) can import them.
Some modules expose a CLI (`bro.base.credentials`, `bro.base.time-util`);
run those with `--help` for flags.

## Modules

- `args.py` — `Parser` (subclass of `argparse.ArgumentParser`):
  the one place in the repo that imports `argparse`.
  Adds repo-wide global flags (`--log`, `--verbose`, `--ic`, `--allow-env`, `--print-env`),
  per-flag env-var overrides,
  mutually-exclusive group declarations,
  `dispatch()` for subcommand handlers registered via `set_handler`,
  and `reconstruct()` (namespace → canonical argv).
  `parse(argv)` is the entry every CLI calls
  — see the "CLI relationship" below.
  `command_signature(('bro', 'list'))` reads an installed command the other way round, returning an argparse-free `CommandSignature`
  — the command's summary and the arguments it declares, minus the repo globals
  — for callers that describe a command to something else.
  It leans on the CLI relationship twice:
  the console-script name resolves to its module through the two names `sync-scripts` publishes every CLI under,
  and the parser is captured by intercepting the `parse` call the module's `main` ends in, so nothing the command does ever runs.
- `credentials.py` — client-side secret resolver (`__cli_name__ = 'credentials'`).
  The code registry maps kinds to a required description and an optional install hook;
  it is assembled from `bro/base/registry.json` and installed `bro.credentials` contributions, and rejects every source-bearing or otherwise unknown field with the store migration named.
  `Store(registry, store_dir, selection)` reads one exclusive directory:
  plain material is `creds/<name>.cred`, and `creds.json` may annotate one typed source per name (`ssm` or a `bro.credential_sources` minting type).
  `get` / `get_json` / `try_get` / `available` address kinds through the explicit selection;
  the `get_instance` siblings address the stored name exactly.
  `$cred` references expand during resolution, with kind targets applying the same selection and instance targets reading storage directly.
  `known_names()` is the code registry's kinds, while the CLI's `--instance` list enumerates the store directory and typed annotations.
  `build_scoped_store(store, names, optional=…)` emits `creds/<kind>.cred` plus typed annotations in `creds.json`, and reports the declared kinds that resolved separately from transitive `$cred` pulls.
  `scoped_view_store` is the lazy, kinds-bounded sibling over the passed store.
  `install_hooks(registry, kinds, store, directory, env)` applies only the named kinds and resolves hook values through that store.
  `CREDENTIALS_REGISTRY`, `BRO_CONFIGS_DIR`, `registry.json`, and `credentials.json` are retired resolver inputs and fail loudly.
  Schemas live in `bro/setup/AGENTS.md`.
- `configs.py` — the exclusive `BRO_STORE` directory (default `~/.bro`), the `~/.bro.json` host config beside it, and the installed bro distribution version shared by credential consumers and trail records.
- `host_config.py` — the host's launch policy (`~/.bro.json`):
  `project_instances(attachment)` reads the `kind+instance` selections recorded for a repository attachment (checkout path or git URL, normalized through `git_url.py`), None where no entry names it,
  and `project_scoped_kinds()` names the kinds some entry selects for
  — what a launch binding no entry may not read at all;
  the caller names the attachment, since resolving the operated repo belongs to the launch layer;
  `llm_presets()` reads the host-wide `--llm` preset names (`bro/launch/llm_flags.py` merges them over the operated project's own table).
  The scheme and its precedence are `bro/setup/AGENTS.md`, "Host config";
  `ride.scope.scoped_secrets` carries the result to each explicitly constructed credential store
- `git_url.py` — git remote URL grammar:
  `is_git_url`, `normalize_git_url` (the canonical spelling two spellings of one remote compare on), and `git_url_path`.
  Pure string handling, so callers that never invoke git
  — a project key match, a config read
  — reach it without the workspace layer
- `spawn.py` — `run` / `run_async` / `popen` wrappers that detach every child into a fresh session (`start_new_session=True`, stdin `/dev/null`) so an interactive `/dev/tty` open fails instead of blocking;
  `run` also SIGKILLs the whole process group on timeout.
  `run_async` is `run`'s awaitable counterpart for the agent's tool path
  — always capturing, and reaping the group when the await is cancelled as well as on timeout, so an interrupted tool call leaves no orphan.
  `kill_group` / `terminate_group` signal a child's whole group directly, for callers that manage lifetime themselves.
  `format_result` is the shape a finished child takes as agent-tool output
  — exit code, then the captured streams capped through `text_window`.
  Used by every agent shell-out.
  `console_script` resolves a console script beside the running interpreter, for machinery a process spawns beside itself rather than looks up on the PATH it was launched with.
- `liveness_test_helper.py` — `Liveness`, a FIFO a spawned process holds for as long as it lives:
  a test asserting that something was reaped blocks on its EOF instead of polling a pid the kernel is free to recycle underneath it.
- `suite_environment.py` — `rebuild_environment()`, which leaves a test process holding none of the session it started in:
  the framework's own environment namespaces cleared,
  the credential resolver's exclusive store pinned at an absent path,
  the zone pinned,
  the log level reset.
  It ships, so a pytest root outside this repository imports it the same way.
  `SESSION_NAMESPACES` / `SESSION_VARIABLES` / `KEPT_VARIABLES` are the sweep's own statement of what carries session state, for a policy holding the framework to it.
  `host_credential_store()` lifts the credential pin for a block, for a test that has to ask what the host holds.
- `offload.py` — `off_loop(function, …)`, awaiting a blocking call in a daemon thread.
  The `asyncio.to_thread` alternative wherever a call may still be running when the process wants to exit:
  the default executor's threads are joined at interpreter shutdown, so one abandoned call there delays the exit by its full remaining runtime.
  A cancelled `off_loop` await abandons the thread instead, leaving whatever it holds to the caller.
- `log.py` — module-level `logging` to stderr (`debug` / `verbose` / `info` / `warning` / `error` / `exception`), tagging each record with the caller's module as `scope`.
  VERBOSE is a custom level between DEBUG and INFO:
  top-level stages log INFO,
  stage detail logs VERBOSE.
  The threshold defaults to INFO, is set per invocation with `--log <level>` (`--verbose` is shorthand for `--log verbose`), and propagates to child processes:
  `set_level` exports `BRO_LOG_LEVEL`, which both `log.py` (at import) and `bro/setup/log.sh` (the shell-script counterpart, same line shape) read, so a launch CLI's verbosity reaches worktree provisioning, containers, and the inner session runner;
  an explicit `--log` overrides the inherited value.
- `lulid.py` — `lulid()`, the repo's id mint:
  a ULID restyled lowercase and dash-grouped 10-8-8 (`01kwphn3q5-w1fdwep2-apw9ag3b`).
  The restyle preserves lexicographic order, so lulids sort by mint time and are safe as range/sort keys.
- `time_util.py` — timezone-aware `Moment` / `Duration` (`datetime` / `timedelta` subclasses), parsers (`parse_moment`, ISO, date), and `utc_now`.
  Local tz is `Europe/Nicosia`.
  Prints the current time as a CLI.
- `name_map.py` — `NameMap`:
  case-insensitive, whitespace-tolerant name → value lookup (strip + casefold, exact match only).
  For matching a free-form name an LLM or human emits against a known set;
  collisions and misses raise with the available names listed.
- `condition.py` — first-class conditions over typed variables:
  `==` / `contains` on `var(...)` references build an immutable predicate at declaration time,
  `evaluate` decides it fail-fast against the facts,
  `when` / `iff` / `select` gate entries of declarative lists.
  One evaluator for both fronts
  — `template.py` directives lower onto it, code builds conditions directly.
  Full semantics:
  `bro/reference/conditions.md`.
- `template.py` — conditional template engine for static agent-facing text (tool descriptions, spell bodies):
  when/iff/eliff/else blocks and assert guards in `{{…}}` groups terminated by `{{end}}`, conditions lowered onto `condition.py`, plus `{{include}}` splices loaded through a caller-supplied resolver.
  Grammar and semantics:
  `bro/reference/template.md`;
  the consuming front (`render_text`) lives in `bro/mcp.py`.
- `text_window.py` — windowed views over large text for tool output:
  `apply_limit` caps to a line + byte budget (keeping head or tail) with inline `[...skipped before/after...]` markers and a fat-finger clamp;
  `numbered_window` layers a cat -n-numbered partial read (0-based `offset`) on top;
  `take_head` returns the budget-bounded prefix raw, for callers that paginate over a cursor instead of dropping the excess.
  `DEFAULT_LIMIT` / `MAX_LIMIT` cap the lines a caller asks for and `BYTE_LIMIT` caps the payload, whichever binds first.
- `source_root.py` — `SOURCE_ROOT`, the installed `bro` package directory, derived from the module's own location.
- `yesno.py` — `yesno(question, default)` interactive y/n prompt.

## CLI relationship

Every CLI in the repo is a bare `def main(argv)` whose body builds a `bro.base.args.Parser` and ends in `return fn(**parser.parse(argv))` or `return parser.dispatch(argv)`.
The global-argv read happens once, in the owning package's committed `_entrypoints.py` shim, not in `main`
— see the root `AGENTS.md` "Commands" section and the committed `_entrypoints.py` of any distribution.
