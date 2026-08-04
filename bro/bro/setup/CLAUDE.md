# bro/bro/setup/CLAUDE.md

How to bring up a fresh checkout, plus the credential schemas the project reads (secrets live in `~/.bro`). Run any script with `--help` for flags.

## Setup

A repository operated by `cw` provides a root `setup.sh` with one postcondition: `.venv/bin/cw` works when it exits. The script runs `uv sync`, activates that environment long enough to install repository hooks, and skips the sync when the container entrypoint exports `CW_VENV_BAKED=1` for its matching baked environment.

PPP is a three-member uv workspace: the root `ppp` distribution depends on workspace member `bro` at runtime and on `bro-dev` in its dev group. `uv sync --all-packages --all-groups --all-extras` creates the shared root `.venv`, installs all three members editably, and registers each distribution's committed console-script bridge. Each member owns its own pyproject tool configuration and development gate.

Prerequisites are documented in `bro/README.md`. `setup_env.sh` remains an optional macOS/Ubuntu reference installer and is not invoked by repository provisioning.

### Worktrees

`cw` creates a fresh `.venv` in each host worktree by running that worktree's `setup.sh`. Container workspaces normally receive the image's matching baked environment and the same setup entry point installs only the repository hooks. Never run `uv sync` against the main checkout from inside another worktree: editable installs record absolute source paths.

## Files

- `setup_env.sh` — reference host-prerequisite installer for macOS and Ubuntu; invoked by nothing
- `versions.sh` and `ubuntu/` — pinned host-tool versions and Ubuntu installers used only by `setup_env.sh`
- `prelude.sh` — shell-script prelude every executable framework script sources; consumers resolve the packaged directory with `bro-shell-dir`
- `log.sh` — leveled shell logging thresholded by `BRO_LOG_LEVEL`
- `strict.sh` — fail-fast shell guards, including command-not-found inside test positions
- `docker_smoke_test.sh` — packaged sourceable helper for service `verify_deps.sh` scripts
- `base_image/` — Dockerfile and builder for the local general-purpose base image
- `container/` — the `cw` image, entrypoint, clone helper, and host-only smoke test. The image bakes a workspace venv in two stages: dependency resolution from the root and member manifests, then editable installation from the full project context. On launch the entrypoint reuses it only when its staged dependency manifests match the clone; otherwise the repository's `setup.sh` performs a fresh sync
- `bro-dev/bro_dev/hooks/post-commit` — packaged hook installed by `bro-dev.install`; it advances token-accounting state after each commit

## Configuration

Credentials live in the standalone `~/.bro` store; the repo no longer carries them. Readers resolve them through `bro.base.credentials` — `credentials.default_store().get_json(name)` — which searches `BRO_CONFIGS_DIR` when a deployed service sets one, then `~/.bro`; the host leaves the variable unset and searches the store alone. The effective registry merges framework entries from `bro/bro/base/registry.json` with installed `bro.credentials` entry points; PPP contributes its names from `extra/credentials.py`. The `credentials get <name> [--field <key>]` CLI exposes the same resolver to non-Python callers (e.g. the Anthropic apiKeyHelper), while `credentials list` prints the names that resolve in the current store; host scripts that write new secrets write directly to `~/.bro`.

A host-local `registry.json` — searched along the same path as the secret files — merges per-name over that effective registry: entries that stay out of the repo — credential variants of a checked-in kind, or a kind's own sources when its checked-in entry declares none (the `github` kind). An addition that doesn't declare `install` inherits the built-in entry's hook, so a sources-only kind override keeps the checked-in wiring:

```json
{"github+pavel": {"sources": [{"file": "github_token_pavel"}]}}
```

A `kind+instance` name declares a variant of the kind named up to the `+` (name grammar owned by `bro/bro/base/credentials.py`). The kind entry owns kind-level behavior — notably the install hook, a template (`bro/bro/reference/template.md`) rendered with `#name` bound to each instance's own name — so a variant declares only its `sources`, and one that carries its own `install` or names a kind the registry lacks fails the load. Instance names exist only on the host: a scoped store materializes a variant under its kind name (entry, cred file, and install hook all speak the kind), so in-session readers address kinds only. A session installs at most one instance of each kind: hydrating two (e.g. `github` and `github+pavel`) fails the launch, so pair a variant grant with a revoke of its sibling (`--grant github+pavel --revoke github`) — or select instances durably per repo via `[tool.bro] creds` (`bro/bro/reference/cw.md`, "Per-project defaults"). Generated registries (a scoped store's `credentials.json`, `CREDENTIALS_REGISTRY`) replace the registry wholesale, so a scoped session stays bounded to exactly its hydrated set and never sees host-local additions it wasn't granted.

A json secret may reference other secrets instead of embedding copies: `{"$cred": "<name>"}` anywhere in its tree resolves to the referenced secret's value, `{"$cred": "<name>", "field": "<key>"}` to one top-level field (exact semantics in `bro/bro/base/credentials.py`). The resolver expands references before any consumer sees the value, so a scoped store hydrates a granted secret with its references already expanded — self-contained in the container, no grant of the referenced secrets needed.

- `notion.json` — Notion token + database IDs (`tasks_db_id`, `events_db_id`, `projects_db_id`, `media_db_id`) + `root_page_id`, the fixed parent page all `create_page` pages land under
- `google_api.json` — Google OAuth client config
- `gmail_creds.json` — cached Gmail OAuth token (JSON-serialised)
- `flow_mcp.json` — `{ "url": "https://flow.<delegated_subdomain>", "token": "<bearer-token>" }` for the deployed flow MCP server (external MCP clients — Claude apps, agents on other hosts)
- `brog.json` — the brog task-tracker backend selection, self-contained: every credential the active backend needs is embedded — literally or as `$cred` references — so brog makes no assumption about other secrets being granted. One of:

  - `{ "backend": "flow", "transport": "http", "url": ..., "token": ... }` — url + token as in `flow_mcp.json` (reference it: `"url": {"$cred": "flow_mcp", "field": "url"}, "token": {"$cred": "flow_mcp", "field": "token"}`)
  - `{ "backend": "flow", "transport": "local", "notion": { ...notion.json shape... } }` — or `"notion": {"$cred": "notion"}`
  - `{ "backend": "github", "token": ..., "repo": "owner/name"? }` — token e.g. `{"$cred": "github"}` or a `github+...` variant; the token's account is the acting identity (issues and comments are created under it — for a `github_app`-backed secret, the app's bot; brog re-reads the config per operation so minted tokens stay fresh); `repo` omitted derives owner/name from the workspace's `origin` remote at server start
- `focus.json` — `{ "url": ..., "token": ... }` for the focus HTTP client
- `infra.json` — `{ "apex": ..., "delegated_subdomain": ... }` consumed by `infra/cdk/config.py`
- `trails.json` — `{ "base_url": "https://trails.<apex>", "token": "<bearer>" }` for the deployed trails server. Required for production bro runs — `BaseBro`'s default tracker factory raises when the file is missing rather than silently falling back — and for the claude session recorder, whose persistent failures surface via the health file `session-recorder-health.json` it writes into the session's claude config dir (read by the cw statusLine and `cw banner`; the daemon's stderr goes to `session-recorder.log` next to it). Created by `bro/bro/trails/bootstrap.sh`
- `anthropic.json` — `{ "api_key": "sk-ant-..." }` shared Anthropic Console API key for any in-repo Anthropic API usage
- `claude_code_oauth_token` — the long-lived OAuth token from `claude setup-token` (scalar, not json), minted once on the host. Registered as the `claude_code` secret; exported as `CLAUDE_CODE_OAUTH_TOKEN` for every interactive Claude Code session (host: subprocess env; container: registry install hook), so each session presents the same stable subscription bearer instead of the rotating `~/.claude/.credentials.json` OAuth whose cross-session refresh-token rotation forced periodic `/login`. Bills against the Pro/Max subscription, not API credits.

  **Required for native claude code sessions in both modes** (`cw ss` / `cw ss --host` / `dive-in`): the token is the session's sole credential — a container mounts no OAuth file, and a host session's private claude state dir carries none either (`bro/bro/reference/cw.md`, "Host claude-state isolation") — so a missing secret fails the launch loudly (container: at scoped-store hydration; host: before the worktree is touched). Populate it once: `claude setup-token` on the host, then store the printed token in `~/.bro/claude_code_oauth_token`. `claude --bare` (the bro LLM hop) ignores the var — it authenticates with the `anthropic` key.
- `tmdb.json` — `{ "api_key": "<v3-key>" }` The Movie Database v3 API key (get one at themoviedb.org → Settings → API). Read lazily by the Librorian bro's TMDb data source
- `brave.json` — `{ "api_key": "<subscription-token>" }` Brave Search API key (api.search.brave.com → Subscriptions; free tier gives 2,000 queries/month at 1 qps). Read lazily by the `WebSearch` data source
- `github_app_<app>.json` (name free, referenced by the host registry) — the GitHub App minting config backing the `github` kind: `{ "app_id": ..., "installation_id": ..., "private_key": "<PEM>" }`, ids as strings or numbers, the key embedded, installation id from the installation page URL (or `GET /app/installations` under an app JWT). The framework's checked-in `github` entry declares no sources, only the kind's install hook; `github_app` resolves lazily through the framework's `bro.credential_sources` entry point; the host-local `registry.json` points the kind at the config: `{"github": {"sources": [{"type": "github_app", "file": "github_app_<app>.json"}]}}`. Declared (as the `github` kind) by `ppp-dev`'s `extra_secrets`.

  - Resolution mints a 1-hour `ghs_…` installation token per read, so the acting identity on pushes, PRs, and issues is the app's bot. The minting-source machinery (TTL hold, store-cache bypass, config-not-token scoped hydration) is `bro/bro/base/credentials.py`'s `MintingSource`; the GitHub half is `bro/bro/extra/github/app.py`'s `Source`. App tokens pass the `gh pr edit` / `gh pr view` team-field GraphQL reads that required the `read:org` scope on the predecessor PAT.
  - The kind's install hook wires the container's git credential helper and a PATH-front `gh` wrapper, each resolving the token via `credentials get` at use time, so consumers always read a fresh mint.
  - Another app can back a host-local `github+<instance>` variant — `{"github+other": {"sources": [{"type": "github_app", "file": "github_app_other.json"}]}}` — selected per repo via `[tool.bro] creds`.
- `aws_credentials` — the AWS shared-credentials INI (`[default]\naws_access_key_id=…`) the deploy tooling authenticates with. Registered as the `aws` secret (a `cp`/symlink of `~/.aws/credentials`). Its install hook materializes it back to `~/.aws/credentials` (the AWS CLI's default path) in the container; declared by `devoops` (and `infra.MCPServer`), or opted in per session with `cw ss --grant aws`.

**Scoped per-bro hydration.** `cw` sessions and native bro launches receive a scoped credential store rather than the whole `~/.bro`. The host resolves only the secrets the session uses — the bro's manifest plus per-surface extras (LLM key for native `bro run` / `bro chat` launches, session baselines for claude code). Container launches pack them (one file per secret plus a scoped `credentials.json` carrying each secret's install hook) into an in-memory tar and `docker cp` that tar into the container's `~/.bro` before it starts, with no host-side store. Host cw-sessions materialize the same store into the session claude dir's `.bro` and point `CREDENTIALS_REGISTRY` at it; this is a convenience scope rather than a security boundary because the session runs as the user. In either mode a non-declared secret resolves to a clean `SecretNotFound`, and hydration is strict: a missing required secret fails the launch. Secrets a tool reads from outside the resolver (git, aws CLI) are wired in containers by registry-declared install hooks, applied generically by the entrypoint. Full mechanics: `bro/bro/reference/cw.md` ("Scoped credential hydration").
