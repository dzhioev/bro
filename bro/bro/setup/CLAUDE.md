# setup/CLAUDE.md

How to bring up a fresh checkout, plus the credential schemas the project reads (secrets live in `~/.ppp`). Run any script with `--help` for flags.

## Setup

```bash
./setup/setup_repo.sh   # uv sync, generate entrypoints bridge, ~/.ppp check
source .venv/bin/activate
```

For a fresh machine, `./setup.sh` (repo root) runs `setup_env.sh` (system tools: stow, uv, docker, AWS CLI, Claude Code) then `setup_repo.sh`. Both scripts are idempotent.

`uv sync` creates `.venv`, installs runtime + `dev` + `cdk` dependency groups from `uv.lock`, and editable-installs the project. Lockfile (`uv.lock`) is committed; refresh with `uv lock --upgrade` when bumping deps. Runtime deps live in `pyproject.toml` `[project] dependencies` (exact `==` pins); dev/cdk deps live in `[dependency-groups]`. The editable install registers the CLI console scripts declared in `[project.scripts]` — after activating the venv, every CLI is a bare command on `PATH`. Those launchers import a generated `_entrypoints.py` shim (gitignored, in the venv site-packages) that feeds `sys.argv` to each CLI's `main(argv)`; provisioning regenerates it via `sync_scripts --entrypoints` after `uv sync` (see the entry-point convention in the root `CLAUDE.md`).

### Worktrees

Worktrees get their own `.venv`. `cw` (host mode) creates the worktree and runs `provision_repo.sh` on every launch; it syncs the venv on the first launch and skips the slow `uv sync` on later ones (re-syncing only when `uv.lock`/`pyproject.toml` changed), while always refreshing the `_entrypoints.py` bridge and git hooks.

**Never run `uv sync` against the main repo from inside a worktree.** The editable install hardcodes absolute paths and would pin the shared venv to a worktree that may later disappear.

## Files

- `setup_env.sh` — installs system tools (stow, claude-code, docker via colima on macOS, awscli, uv). macOS and Ubuntu only
- `setup_repo.sh` — host repo setup: calls `provision_repo.sh`, then runs the `~/.ppp` presence check
- `provision_repo.sh` — the shared, idempotent "provision a checked-out repo" step: `uv sync` (skipped when the venv is already current) + regenerates the `_entrypoints.py` console-script bridge (skipped when `CW_VENV_BAKED=1`, i.e. the container reused the image's baked venv whose bridge already matches) + installs the `post-commit` git hook + registers the repo-local `git golc` alias. Called by all three surfaces that need a provisioned repo — `setup_repo.sh` (host main repo), `cw` host mode (host worktrees), and the container entrypoint. Tree creation (clone / worktree) and surface-specific wiring (credentials, bro-skills) stay with the callers
- `bootstrap_session_log.sh` — one-time IAM/SSM setup for session-log sync (creates `cw-session-log-sync` IAM user + key, writes `~/.ppp/session_log.json`). Run once after deploying `SessionLogStack`
- `bootstrap_trails.sh` — one-time setup for the trails sink (reads `/trails/bearer-token` from SSM, derives `base_url` from the `infra` secret's `delegated_subdomain`, writes `~/.ppp/trails.json`). Run once after deploying `TrailsServerStack`
- `claude_commit_footer.py` — prints the per-commit token-accounting footer (path-invoked, not a console script; runs through `base.args`, so the project venv must be active — the editable install puts `base` on the path even when it is invoked by file path). Two `>`-quoted lines — `> created with Claude Code <versions> | <model>: <delta>[, …]` then `> session(s): <id>[, …]` — with `'` as the thousands separator so it never collides with the `, ` joining entries.

  The per-model number is a per-commit *delta*: the session's cumulative transcript usage now minus the baseline already attributed to its earlier commits. So deltas — not cumulatives — are what sum across a range. Baselines live in the gitignored `<repo>/.token_accounting_state.json` (`committed` marks plus a `staged` proposal). Default mode emits the delta and stages the new cumulative; `--record` (run by the `post-commit` hook) promotes staged→committed once a commit lands, so the mark only advances on success; `--squash <range>` (run by `/land`) emits an aggregated footer over a branch — the union of its commits' deltas / sessions / versions plus the land session's uncommitted remainder — so squash merges keep their discarded children's tokens. The session ids link each commit back to its source trail.
- `git_hooks/post-commit` — git hook installed into `.git/hooks` by `provision_repo.sh` (on every surface); runs `claude_commit_footer.py --record` to promote the staged token-accounting baseline after a commit lands. Surfaces failures (it imports `base.args`) rather than swallowing them, so commit with the venv active
- `git_golc.py` — `git golc` alias backend (repo-local). Renders `git gol`-style oneline-graph log with a per-commit credits column (per-model, rounded — e.g. `O:18K S:1.2M`).

  Two-pass: collects per-sha deltas from commit-message footers (same format as `claude_commit_footer.py`), then substitutes a sentinel in a `--graph --color=always` render. Legacy single-line footers carry a session cumulative, not a delta, so their value is prefixed with `~` and dimmed — visibly not a real per-commit number. Pages through `less -R` (with `LESS=FRX` in the environment) on a tty. The `git golc` alias is registered by `provision_repo.sh` (see the `provision_repo.sh` bullet above).
- `docker_smoke_test.sh` — sourceable helper for service `verify_deps.sh` scripts (`smoke_build`, `smoke_start`, `smoke_await`, `smoke_curl`, `smoke_assert_status`); picks docker or podman via `$OCI_CMD` from `infra/deploy_lib.sh`
- `print_anthropic_key.sh` — prints the `anthropic` secret's `api_key` via `credentials get anthropic --field api_key`. Wired as `apiKeyHelper` by `cw --bro` so claude reads the key without the "Detected a custom API key" confirmation that `ANTHROPIC_API_KEY` would trigger every session
- `container/` — Dockerfile + entrypoint for the `cw -c` container image; `bump-claude-code.sh` rebuilds with the pinned `claude-code-version`. Image bundles the docker CLI; `cw.py` bind-mounts the host docker socket so deploy scripts inside the container can build + push against the host daemon.

  The Dockerfile bakes the project venv at `/opt/cw-venv` (deps + editable project + the `_entrypoints.py` console-script bridge, the editable module finder pointing at `/workspace`) so the entrypoint can symlink it in and skip both `uv sync` (~3.4s) and `sync-scripts --entrypoints` (~1s) on every launch — correct for the image's life because the tag pins `pyproject.toml` + `uv.lock` (and the bridge is a pure function of the tag-pinned `[project.scripts]`). The entrypoint signals the skip to `provision_repo.sh` via `CW_VENV_BAKED=1`. `test_smoke.sh` validates the image + entrypoint postconditions (run on the host by `run_tests.py`, skipped with `--no-docker` or inside a container)
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
- `claude_code_oauth_token` — the long-lived OAuth token from `claude setup-token` (scalar, not json), minted once on the host. Registered as the `claude_code` secret; exported as `CLAUDE_CODE_OAUTH_TOKEN` for every interactive Claude Code session (host: subprocess env; container: registry install hook), so each session presents the same stable subscription bearer instead of the rotating `~/.claude/.credentials.json` OAuth whose cross-session refresh-token rotation forced periodic `/login`. Bills against the Pro/Max subscription, not API credits.

  **Required for container claude code sessions** (`cw ss -c`, `dive-in`): the token is the container session's sole credential (no OAuth file is mounted), so a missing secret fails loudly at scoped-store hydration on the host. Populate it once: `claude setup-token` on the host, then store the printed token in `~/.ppp/claude_code_oauth_token`. Host-mode sessions use it best-effort (they fall back to the host's own `~/.claude/.credentials.json` when it's absent). `claude --bare` (the bro LLM hop) ignores the var — it authenticates with the `anthropic` key.
- `tmdb.json` — `{ "api_key": "<v3-key>" }` The Movie Database v3 API key (get one at themoviedb.org → Settings → API). Read lazily by the Librorian bro's TMDb data source
- `brave.json` — `{ "api_key": "<subscription-token>" }` Brave Search API key (api.search.brave.com → Subscriptions; free tier gives 2,000 queries/month at 1 qps). Read lazily by the `WebSearch` data source
- `cw_github_token_bro` — the GitHub token (scalar, not json) container sessions push with, under the `Bro` git identity that the pre-push hook fences from `master`. Must carry the `repo` and `read:org` scopes — `read:org` is required by `gh pr edit` / `gh pr view`, whose PR-metadata GraphQL reads Team `login`/`name`/`slug` fields (a regenerated token that drops it reintroduces "requires read:org" failures). Registered as the `github` secret; declared by `ppp-dev`'s `extra_secrets`. Its install hook wires `git credential.helper` + `GH_TOKEN` in the container.
- `aws_credentials` — the AWS shared-credentials INI (`[default]\naws_access_key_id=…`) the deploy tooling authenticates with. Registered as the `aws` secret (a `cp`/symlink of `~/.aws/credentials`). Its install hook materializes it back to `~/.aws/credentials` (the AWS CLI's default path) in the container; declared by `devoops` (and `infra.MCPServer`), or opted in per session with `cw ss --grant aws`.

**Scoped per-bro hydration (containers).** `cw`/`do` give each container a scoped credential store rather than the whole `~/.ppp`. The host resolves only the secrets the session uses — the bro's manifest plus per-surface extras (LLM key for `ask`/`do-task`, session baselines for claude code) — packs them (one file per secret plus a scoped `credentials.json` carrying each secret's install hook) into an in-memory tar, and `docker cp`s that into the container's `~/.ppp` before it starts (no host-side store); any non-declared secret then resolves to a clean `SecretNotFound`. Hydration is strict: a missing secret fails the launch. Secrets a tool reads from outside the resolver (git, aws CLI) are wired in by registry-declared install hooks, applied generically by the entrypoint. Full mechanics: `reference/cw.md` ("Scoped credential hydration").
