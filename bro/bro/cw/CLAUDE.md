# cw/CLAUDE.md

`cw` launches claude in a managed workspace — either a host **worktree** or an isolated docker **container** clone. This package is the structural split of the former monolithic `cw.py`; behavior is unchanged. For *how it actually works* (subcommands, host vs container modes, credential seeding, env vars, `--resume`/`--bro`/`--auto`), read `reference/cw.md` — that document owns the behavior, this one owns the code layout.

## Module map

Leaf utilities (no intra-package deps, or only on each other):

- `constants.py` — `_CW_MODEL` (the claude model every session runs). Its own module so `bro.py` + `session.py` share it without a heavier import
- `git.py` — `git_out` (capture a git command's stdout) + `git_run` (the capture-output/text/env `subprocess.run` wrapper for git calls whose returncode/stdout is inspected) + `no_prompt_env` (`GIT_TERMINAL_PROMPT=0` overlay so git fails fast instead of prompting)
- `paths.py` — project-root / worktrees-dir / containers-dir / broker control-dir resolution, `_venv_env`, `_in_container`, `_latest_jsonl`
- `flags.py` — `add_forwarded_flags` / `extract_forwarded_argv` / `EFFORT_LEVELS`: the pass-through flag set `dive-in` forwards into `cw ss`
- `secrets.py` — scoped-credential logic (`_container_secrets` → a `ScopedSecrets(required, optional, docker_sock)` dataclass), `_finalize_secrets`, `_load_anthropic_key`, `_claude_code_token_env`, `_ppp_tarball`, the session baseline/default-bro constants
- `mcp.py` — session-local HTTP MCP serving: the fixed in-container port, `_http_mcp_config` / `_container_mcp_launch` (the claude mcp-config json + the `CW_MCP_HTTP_*` env the entrypoint reads), and `_start_host_mcp_server` / `_HostMCPServer` (the host-mode `mcp-server flow --http --port 0` lifecycle: OS-assigned port via port file, terminate on session exit)
- `system_prompt.py` — `_load_base_prompts` / `_session_append_prompt` (the `--append-system-prompt` text)
- `session_context.py` — `build_session_context` / `encode_session_context`: the typed launch-context records (system prompt, git state, MCP servers, root CLAUDE.md) `start_session` captures into `CW_SESSION_CONTEXT` for `sync-session-log` → `rewind`

Workspace inspection + docker mechanics:

- `docker.py` — thin docker wrappers (`running_mounts`, `find_container_id` — takes the mount `Path` so this stays a dependency-free leaf), image build (`_image_tag` / `_ensure_image`), `_docker_create_argv`, `_create_container` (create + scoped-store `docker cp`, shared by `containers.py` and `spawn.py`), and the container-config constants it writes
- `workspace.py` — the `Workspace` ABC (`HostWorktree` / `ContainerWorkspace`) owning the host/container duality for inspection + teardown (`path`, `ref`, `is_active`, `is_clean`, `remove`, `claude_projects_dir`, `subject` / `last_active`, the `from_ref` / `all` factories). The shared clean *policy* is one `_check_clean(runner, refresh_origin)` parameterized by a `_GitRunner` each subtype supplies (status-env, ancestry-env, head-ref provider, `bring_in_submodule_head` hook — container's alternate-objects dance lives in `ContainerWorkspace._git_runner`, a no-op on host). Kept as module-level helpers: the thin `_format_ref` / `_parse_ref` / `_CONTAINER_PREFIX` (banner + `exec_in_workspace` need format/parse without an instance), `_host_path_is_clean` (the host policy on an arbitrary path — `cw check-clean` with no ref), `_host_pidfile`, `_read_subject`, `_last_active`, and `_cleanup_image` / `_remove_container_dir` (the force-rmtree-with-root-escalation + image-discovery teardown primitives `ContainerWorkspace.remove` composes)
- `banner.py` — `cw banner`: the `SessionFacts` dataclass (the typed session facts both renderers read — `collect` classmethod ← env + /.dockerenv, `render_visual` / `render_llm` methods, `display_ref` property) + `render_banner` + the prompt-split / logo constants

Launch + lifecycle:

- `containers.py` — container launch (`run_in_container`, `_seed_container_claude_json`, `_replace_container_resume_hint`) and `exec_in_workspace`. `run_in_container` runs the session as the root peer of a broker — `_run_root_via_broker` constructs `Broker(UnixServerTransport(var/cw/broker), DockerSpawner())` and the post-exit finish runs after `Broker.run()` returns — unless the `_broker_enabled` gate (`BROKER_DISABLED` set, or broker unimportable) degrades it to the direct `docker start -a -i`
- `spawn.py` — `DockerSpawner`: cw's adapter for broker's async `Spawner` port, with the concrete `DockerLaunchSpec` it reads. Either launch mode builds the container via `_docker_create_argv` + `_create_container`, with the provisioned channel socket → `/run/broker.sock` and `BROKER_CHANNEL` pointed at it, then attaches `docker start` as an asyncio subprocess: the non-TTY child (`start -a`, stdout and stderr merged into one pipe and drained by an async task into a bounded ring buffer for `failed{output_tail}`) vs the interactive root (`start -a -i`, inherited stdio, a SIGINT forwarder for the attach's lifetime, empty `output_tail`). Not in the hub re-export — only `containers._run_root_via_broker` consumes it, function-locally (see the lazy-broker-import invariant)
- `worktrees.py` — host worktree create / provision / finish
- `listing.py` — `cw list` (one loop over `Workspace.all(proj)`; keeps the `ThreadPoolExecutor` fan-out — mounts fetched concurrently with the per-workspace subject/last_active reads)
- `clean.py` — `cw clean` (one `_assess` over `Workspace.all(proj)`; keeps the fan-out — host worktrees assessed concurrently with the `docker ps` mounts fetch, containers once it resolves)
- `bro.py` — `_populate_bro_skills` (skill-symlink surfacing) + `_bro_launch` (the `claude --bare` argv and session-local MCP env for `--bro`, via `cw.mcp`)
- `session.py` — the `SessionSpec` dataclass (the parsed session parameters; `to_command_argv` builds `CW_COMMAND` / the resume hint, `resume_variant` clears the create-only inputs) threaded through `start_session` → `cw`, plus `_resolve_base_ref` / `_deployed_mcp_argv`: the session-parameter plumbing both modes share
- `cli.py` — `build_parser` + `main`; `__cli_name__ = 'cw'` registers the `cw` alias on top of the canonical `cw.cli` script

## Workspace duality

The package's central concept is a *workspace backed by either a host worktree or a container clone*. That duality is expressed as the `Workspace` ABC in `workspace.py` (`HostWorktree` / `ContainerWorkspace`), which absorbs the host/container branching for the inspection + teardown surface — the call sites (`listing.py`, `clean.py`, `cli.py` check-clean, `session.py` resume/host-finish, `containers.py` resume-hint) loop over `Workspace` objects instead of threading a `container: bool` and forking on it. The boundary stops at inspection/teardown: **launch** stays two plain functions (host: `worktrees.py`; container: `containers.run_in_container`) that only *consume* a `Workspace` for the post-run clean/finish.

## Invariants

- **Hub re-export.** `__init__.py` re-exports exactly the cross-package surface that `do/` and `dive_in.py` consume (`run_in_container`, `render_banner`, `add_forwarded_flags`, `extract_forwarded_argv`, `_project_root`, `_worktrees_dir`, `_containers_dir`, `build_parser`). Those callers import these function-locally / patch them by the `cw.<name>` path, so the hub keeps them untouched — don't shrink it without checking `do/` (it patches `cw.run_in_container` in many test sites) and don't hoist `do/`'s function-local `cw` imports to module level (that would bind the name before a test can patch it).
- **Submodule → submodule imports.** Intra-package code imports `from cw.<module> import …`, never `from cw import …` / `import cw` — going through the hub mid-initialization is a partial-init hazard. The lone cycle (`docker._docker_create_argv` needs `containers._seed_container_claude_json`, and `containers` imports the docker helpers) is broken with a function-local import inside `_docker_create_argv`.
- **Lazy bro import.** `bro.registry` is imported function-locally (in `secrets`, `system_prompt`, `bro`) so `import cw` stays cheap — `dive_in.py` imports `cw` at module top level and the hub aggregates every submodule, so a module-level `bro.registry` import would tax every importer.
- **Lazy broker import.** The broker package is imported function-locally in `containers.py`, behind the `_broker_enabled` gate: `BROKER_DISABLED` — and an environment whose venv can't import broker — must short-circuit *before* any broker import, or the kill-switch couldn't save a launch that the import itself crashes. `spawn.py` imports broker at module level, which is why it stays out of the hub and is only imported past the gate.
