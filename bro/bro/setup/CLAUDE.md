# setup/CLAUDE.md

How to bring up a fresh checkout, plus the credential schemas the project reads from `.configs/`. Run any script with `--help` for flags.

## Setup

```bash
./setup/setup_repo.sh   # submodules, uv sync, sync-scripts, .configs check
source .venv/bin/activate
```

For a fresh machine, `./setup.sh` (repo root) runs `setup_env.sh` (system tools: stow, uv, docker, AWS CLI, Claude Code) then `setup_repo.sh`. Both scripts are idempotent.

`uv sync` creates `.venv`, installs runtime + `dev` + `cdk` dependency groups from `uv.lock`, and editable-installs the project. Lockfile (`uv.lock`) is committed; refresh with `uv lock --upgrade` when bumping deps. Runtime deps live in `pyproject.toml` `[project] dependencies` (exact `==` pins); dev/cdk deps live in `[dependency-groups]`. The editable install registers the CLI console scripts declared in `[project.scripts]` — after activating the venv, every CLI is a bare command on `PATH`.

### Worktrees

Worktrees get their own `.venv`. The `.claude/hooks/session_start.sh` hook seeds submodule git-dirs from the main repo (APFS clonefile on macOS, `--reflink=auto` on Linux) and runs `uv sync` on first session in the worktree; subsequent sessions bail.

**Never run `uv sync` against the main repo from inside a worktree.** The editable install hardcodes absolute paths and would pin the shared venv to a worktree that may later disappear.

## Files

- `setup_env.sh` — installs system tools (stow, claude-code, docker via colima on macOS, awscli, uv). macOS and Ubuntu only
- `setup_repo.sh` — submodules + `uv sync` + `sync-scripts` + `uv sync` again + registers repo-local `git golc` alias + `.configs` symlink sanity check
- `bootstrap_session_log.sh` — one-time IAM/SSM setup for session-log sync (creates `cw-session-log-sync` IAM user + key, writes `.configs/session_log.json`). Run once after deploying `SessionLogStack`
- `claude_commit_footer.py` — prints the per-commit footer with cumulative per-model token totals plus the session id (`> created with Claude Code <version> (<model>: N,NNN, …; session: <id>)`).

  The session id links a commit back to its source transcript; the comma-separated totals are precise enough that `usage-report <git-range>` can sum them across a commit range.
- `git_golc.py` — `git golc` alias backend (repo-local). Renders `git gol`-style oneline-graph log with a per-commit credits column (per-model, rounded — e.g. `O:18K S:1.2M`).

  Two-pass: collects per-sha totals from commit-message footers (same format as `claude_commit_footer.py`), then substitutes a sentinel in a `--graph --color=always` render. Pages through `less -RFX` on tty. Alias is registered by `setup_repo.sh`.
- `docker_smoke_test.sh` — sourceable helper for service `verify_deps.sh` scripts (`smoke_build`, `smoke_start`, `smoke_await`, `smoke_curl`, `smoke_assert_status`); picks docker or podman via `$OCI_CMD` from `infra/deploy_lib.sh`
- `print_anthropic_key.sh` — prints `.configs/anthropic.json`'s `api_key`. Wired as `apiKeyHelper` by `cw --bro` so claude reads the key without the "Detected a custom API key" confirmation that `ANTHROPIC_API_KEY` would trigger every session
- `container/` — Dockerfile + entrypoint for the `cw -c` container image; `bump-claude-code.sh` rebuilds with the pinned `claude-code-version`. Image bundles the docker CLI; `cw.py` bind-mounts the host docker socket so deploy scripts inside the container can build + push against the host daemon
- `ubuntu/` — Ubuntu-only install helpers (currently `install_stow.sh`)
- `dotfiles/` — GNU Stow dotfiles submodule. `.configs` at the repo root is a symlink into `setup/dotfiles/dotfiles/dot-ppp`

## Configuration

Credentials live in `.configs/` (symlink into the dotfiles submodule).

- `notion.json` — Notion token + database IDs (`tasks_db_id`, `events_db_id`, `projects_db_id`, `media_db_id`)
- `google_api.json` — Google OAuth client config
- `gmail_creds.json` — cached Gmail OAuth token (JSON-serialised)
- `flow_mcp.json` — `{ "url": "https://flow.<delegated_subdomain>", "token": "<bearer-token>" }` for the deployed flow MCP server (`cw --mcp http` and external MCP clients)
- `focus.json` — `{ "url": ..., "token": ... }` for the focus HTTP client
- `infra.json` — `{ "apex": ..., "delegated_subdomain": ... }` consumed by `infra/cdk/config.py`
- `session_log.json` — `{ "aws_access_key_id", "aws_secret_access_key", "region", "bucket", "table" }` for `sync-session-log` (created by `bootstrap_session_log.sh`)
- `anthropic.json` — `{ "api_key": "sk-ant-..." }` shared Anthropic Console API key for any in-repo Anthropic API usage
- `tmdb.json` — `{ "api_key": "<v3-key>" }` The Movie Database v3 API key (get one at themoviedb.org → Settings → API). Read lazily by the Librorian bro's TMDb data source
- `brave.json` — `{ "api_key": "<subscription-token>" }` Brave Search API key (api.search.brave.com → Subscriptions; free tier gives 2,000 queries/month at 1 qps). Read lazily by the `WebSearch` data source
