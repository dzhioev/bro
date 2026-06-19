# setup/CLAUDE.md

How to bring up a fresh checkout, plus the credential schemas the project reads (secrets live in `~/.ppp`). Run any script with `--help` for flags.

## Setup

```bash
./setup/setup_repo.sh   # uv sync, sync-scripts, ~/.ppp check
source .venv/bin/activate
```

For a fresh machine, `./setup.sh` (repo root) runs `setup_env.sh` (system tools: stow, uv, docker, AWS CLI, Claude Code) then `setup_repo.sh`. Both scripts are idempotent.

`uv sync` creates `.venv`, installs runtime + `dev` + `cdk` dependency groups from `uv.lock`, and editable-installs the project. Lockfile (`uv.lock`) is committed; refresh with `uv lock --upgrade` when bumping deps. Runtime deps live in `pyproject.toml` `[project] dependencies` (exact `==` pins); dev/cdk deps live in `[dependency-groups]`. The editable install registers the CLI console scripts declared in `[project.scripts]` — after activating the venv, every CLI is a bare command on `PATH`.

### Worktrees

Worktrees get their own `.venv`. The `.claude/hooks/session_start.sh` hook runs `uv sync` on first session in the worktree; subsequent sessions bail.

**Never run `uv sync` against the main repo from inside a worktree.** The editable install hardcodes absolute paths and would pin the shared venv to a worktree that may later disappear.

## Files

- `setup_env.sh` — installs system tools (stow, claude-code, docker via colima on macOS, awscli, uv). macOS and Ubuntu only
- `setup_repo.sh` — `uv sync` + `sync-scripts` + `uv sync` again + registers repo-local `git golc` alias + `~/.ppp` presence check
- `bootstrap_session_log.sh` — one-time IAM/SSM setup for session-log sync (creates `cw-session-log-sync` IAM user + key, writes `~/.ppp/session_log.json`). Run once after deploying `SessionLogStack`
- `bootstrap_trails.sh` — one-time setup for the trails sink (reads `/trails/bearer-token` from SSM, derives `base_url` from the `infra` secret's `delegated_subdomain`, writes `~/.ppp/trails.json`). Run once after deploying `TrailsServerStack`
- `claude_commit_footer.py` — prints the per-commit footer with cumulative per-model token totals plus the session id (`> created with Claude Code <version> (<model>: N,NNN, …; session: <id>)`).

  The session id links a commit back to its source transcript; the comma-separated totals are precise enough that `usage-report <git-range>` can sum them across a commit range.
- `git_golc.py` — `git golc` alias backend (repo-local). Renders `git gol`-style oneline-graph log with a per-commit credits column (per-model, rounded — e.g. `O:18K S:1.2M`).

  Two-pass: collects per-sha totals from commit-message footers (same format as `claude_commit_footer.py`), then substitutes a sentinel in a `--graph --color=always` render. Pages through `less -RFX` on tty. Alias is registered by `setup_repo.sh`.
- `docker_smoke_test.sh` — sourceable helper for service `verify_deps.sh` scripts (`smoke_build`, `smoke_start`, `smoke_await`, `smoke_curl`, `smoke_assert_status`); picks docker or podman via `$OCI_CMD` from `infra/deploy_lib.sh`
- `print_anthropic_key.sh` — prints the `anthropic` secret's `api_key` via `credentials get anthropic --field api_key`. Wired as `apiKeyHelper` by `cw --bro` so claude reads the key without the "Detected a custom API key" confirmation that `ANTHROPIC_API_KEY` would trigger every session
- `container/` — Dockerfile + entrypoint for the `cw -c` container image; `bump-claude-code.sh` rebuilds with the pinned `claude-code-version`. Image bundles the docker CLI; `cw.py` bind-mounts the host docker socket so deploy scripts inside the container can build + push against the host daemon
- `ubuntu/` — Ubuntu-only install helpers (currently `install_stow.sh`)

## Configuration

Credentials live in the standalone `~/.ppp` store (the GNU Stow target of `dot-ppp`); the repo no longer carries them. Readers resolve them through `base.credentials` — `credentials.default_store().get_json(name)` — which searches `<repo>/.configs` (where the deployed services synthesize their configs at runtime) then `~/.ppp`. The built-in registry maps each secret name below to its `<file>`. The `credentials get <name> [--field <key>]` CLI exposes the same resolver to non-Python callers (e.g. the Anthropic apiKeyHelper); host scripts that write new secrets write directly to `~/.ppp`.

- `notion.json` — Notion token + database IDs (`tasks_db_id`, `events_db_id`, `projects_db_id`, `media_db_id`)
- `google_api.json` — Google OAuth client config
- `gmail_creds.json` — cached Gmail OAuth token (JSON-serialised)
- `flow_mcp.json` — `{ "url": "https://flow.<delegated_subdomain>", "token": "<bearer-token>" }` for the deployed flow MCP server (`cw --mcp http` and external MCP clients)
- `focus.json` — `{ "url": ..., "token": ... }` for the focus HTTP client
- `infra.json` — `{ "apex": ..., "delegated_subdomain": ... }` consumed by `infra/cdk/config.py`
- `session_log.json` — `{ "aws_access_key_id", "aws_secret_access_key", "region", "bucket", "table" }` for `sync-session-log` (created by `bootstrap_session_log.sh`).

  A persistently-broken sync (e.g. a missing IAM grant) surfaces via the health file `~/.claude/session-log-sync-health.json` that `sync-session-log` writes after each attempt — read by the cw statusLine and `cw banner`, which warn to re-run `bootstrap_session_log.sh`. Without it the failure is silent (the hooks discard the watcher's stderr).
- `trails.json` — `{ "base_url": "https://trails.<apex>", "token": "<bearer>" }` for the deployed trails server. Required for production bro runs — `BaseBro`'s default tracker factory raises when the file is missing rather than silently falling back. Created by `bootstrap_trails.sh`
- `anthropic.json` — `{ "api_key": "sk-ant-..." }` shared Anthropic Console API key for any in-repo Anthropic API usage
- `tmdb.json` — `{ "api_key": "<v3-key>" }` The Movie Database v3 API key (get one at themoviedb.org → Settings → API). Read lazily by the Librorian bro's TMDb data source
- `brave.json` — `{ "api_key": "<subscription-token>" }` Brave Search API key (api.search.brave.com → Subscriptions; free tier gives 2,000 queries/month at 1 qps). Read lazily by the `WebSearch` data source
- `cw_github_token_bro` — the GitHub token (scalar, not json) container sessions push with, under the `Bro` git identity that the pre-push hook fences from `master`. Registered as the `github` secret; declared by `ppp-dev`'s `extra_secrets`. Its install hook wires `git credential.helper` + `GH_TOKEN` in the container.
- `aws_credentials` — the AWS shared-credentials INI (`[default]\naws_access_key_id=…`) the deploy tooling authenticates with. Registered as the `aws` secret (a `cp`/symlink of `~/.aws/credentials`). Its install hook materializes it back to `~/.aws/credentials` (the AWS CLI's default path) in the container; declared by `devoops` (and `infra.MCPServer`), or opted in per session with `cw ss --grant aws`.

**Scoped per-bro hydration (containers).** `cw`/`do` give each container a scoped credential store rather than the whole `~/.ppp`. The host resolves only the secrets the session uses — the bro's manifest plus per-surface extras (LLM key for `ask`/`do-task`, session baselines for claude code) — packs them (one file per secret plus a scoped `credentials.json` carrying each secret's install hook) into an in-memory tar, and `docker cp`s that into the container's `~/.ppp` before it starts (no host-side store); any non-declared secret then resolves to a clean `SecretNotFound`. Hydration is strict: a missing secret fails the launch. Secrets a tool reads from outside the resolver (git, aws CLI) are wired in by registry-declared install hooks, applied generically by the entrypoint. Full mechanics: `reference/cw.md` ("Scoped credential hydration").
