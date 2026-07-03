# cw

`cw` launches Claude Code in an isolated per-task workspace. It owns the workspace lifecycle (create / provision / list / clean), wires up MCP and bro personas, and chooses between two execution modes — a host-side git worktree or an isolated Docker container — without changing what the user sees on the inside.

This document explains *how it actually works*: the launch stack, where things live on disk, what each flag does, how credentials and settings move between host and container, and which env vars are produced and consumed. For the source of truth on flags, run `cw --help` / `cw ss --help`; the implementation lives in the `cw/` package (`cw/CLAUDE.md` maps the modules).

## Subcommands

`cw` is a subcommand dispatcher:

- **`cw ss <name>`** — start a session in the workspace `<name>`. Creates the workspace on the fly if it doesn't exist. This is the workhorse; everything below describes its behaviour.
- **`cw list`** — list every workspace under this project. Each entry shows a state badge (`[.]` live host session, `[o]` live container session, `[x]` abandoned), the workspace name (container names are prefixed `c:`), an age (last filesystem touch), and the first user prompt of the latest session.
- **`cw clean [--force] [--dry-run] [<ref> ...]`** — remove workspaces with no uncommitted or unpushed work. Without args, scans both namespaces. With explicit `<ref>`s, restricts to those (`name` for host, `c:name` for container). `--force` removes despite dirty state; `--dry-run` only prints. Safety is shared with `check-clean`. Container workspaces may hold files owned by an in-container uid (root, or `cw` ≠ the host user) that a host-side `rmtree` can't unlink; removal first tries `rmtree`, then escalates to deleting from inside a throwaway root container (any local `ppp-cw` image). A workspace whose removal fails is logged and skipped rather than aborting the sweep; a non-empty failure count makes the command exit non-zero.
- **`cw check-clean [<ref>]`** — probe a single workspace (or the cwd if omitted). Exit 0 if it's safe to drop; exit 1 with reasons on stderr otherwise. (Host mode applies the same `Workspace.is_clean` check inline at session exit to drive its keep-or-drop offer.)
- **`cw exec <name> [<cmd>…]`** — exec into the container backing workspace `<name>`. With no command, opens an interactive bash with `/workspace/.venv` sourced; otherwise runs `<cmd>` in the same activated env. Runs as the `cw` user (`docker exec -u cw`), matching the entrypoint's `gosu` drop — without it `docker exec` would default to the image's root and write root-owned files into the bind-mounted `/workspace` that the host user can't later remove. Container workspaces only; the `c:` prefix is accepted but optional.
- **`cw banner [--llm]`** — print the banner: workspace name (with the `c:` prefix from `cw list` for container workspaces), the canonical `cw ss …` invocation (`CW_COMMAND`), `/workspace` plus its host bind-mount path on separate lines, the `cw exec <name>` hint (labelled `docker shell:` because it drops into a shell *inside* the container), the outer launching command (`PPP_SHELL_COMMAND`), and the user-typed prompt extracted out of it (split at the last `--new`/`-p`/`--prompt`/`--` marker). For `--bro` sessions, prepends the ASCII Bro logo with a dim `// <bro>` signature on its bottom line. The container image's `~/.bashrc` runs `cw banner` once per interactive shell so users see it on `cw exec` entry. The visual mode renders the `@prompt@` placeholder and the `prompt:` label in bright-white bold for emphasis. The `--llm` flag emits the same facts as plain `key: value` lines (no ANSI, no logo) for Claude itself to read at session start via the Bash tool (see `prompts/environment.md`); the prompt body is deliberately omitted there since the LLM already has it as its first message — `launch_command` retains its trailing marker (`dive-in --new `) as the signal that a seed prompt exists.

A workspace is "clean" when:

- (a) `git status --porcelain` is empty,
- (b) `HEAD` is an ancestor of `origin/master`, and
- (c) every submodule's pinned commit is reachable from its remote default ref.

For container workspaces the ancestry checks run against the host project's `.git` (the container clone's own `origin` is an HTTPS URL the host can't reach without credentials). The check reads the clone's `HEAD` sha and runs `merge-base`/`rev-list` in the host repo with the clone's object store exposed as a read-only alternate, rather than fetching the clone's `HEAD` into the host repo — `cw clean` checks every workspace concurrently, and a per-check fetch raced on the shared `FETCH_HEAD`/ref locks (yielding wrong commit counts and a flapping clean/dirty verdict). `origin/master` is fetched once up front for the same reason; `check-clean` (single workspace, no concurrency) still fetches inline.

## The launch stack

Every `cw ss` session launches through the same three-layer stack. `-c` changes only the outer machinery; `--bro` changes only the argv flavor (and the credential policy the outer computes):

- **the outer `cw ss`** (`cw/session.py:start_session`) — mode-specific by nature: policy validation, workspace preparation, session supervision, post-exit UX. See "The outer layer".
- **the in-place session runner** (`cw ss --in-place` → `cw/runner.py`) — one code path for every flag combination, spawned by the outer from the workspace's own venv so a session always runs its workspace's code, not the launching repo's. It runs where claude runs and owns everything next to it. See "The in-place session runner".
- **the flavor** — the native/bro fork, confined to the claude argv builder plus (host-side) the secret manifest and docker-socket policy. See "The claude argv".

A new session-shaping flag lands once — in the runner or the builder — and applies to both execution modes and both flavors by construction.

## The outer layer

Host mode is the default; `-c` / `--container` selects the container. Whatever the mode, the outer:

- validates policy once — the `--auto` × `-c` and `--bro` × `-c` gates and the anthropic-key probe run only here, never in the runner (the inner argv carries `--auto` but never `-c`, so re-running them would reject it);
- sets `CW_COMMAND` / `CW_RESUME_COMMAND` and resolves `--into` against the host repo to a sha;
- refuses to start when a session is already active on the target workspace — a live `cw-session.pid` (host) or a running bound container (container). One session per workspace: a second concurrent claude would mutate the same files and share the gitignored token-accounting state. The lock releases on exit, so re-entry / `--resume` after a session ends is unaffected;
- with `--resume`, fails fast when the workspace has no recorded claude session — a cheap existence check, run before the workspace auto-create could manufacture an empty workspace for a mistyped name (the runner resolves the actual session id later, from its cwd);
- prepares the workspace (the two mode sections below), then spawns the runner from the workspace's own venv with the machinery flags it consumed stripped from the inner argv (`-c --drop --grant --revoke --into`);
- owns the post-exit UX — keep-or-drop offer (host), resume-hint replacement (container), `--drop` removal in both.

### Host mode (`cw ss <name>`)

`cw` owns the worktree lifecycle directly: it prepares the worktree, then spawns the worktree's own `cw ss --in-place` as a subprocess inside it, which runs plain `claude` (not `claude -w`, so no Claude Code worktree/provisioning hooks are involved). On launch:

1. creates the worktree if new — a `worktree-<name>` branch (based on `--into <ref>` when given, else the current `HEAD`) plus `submodule.alternateLocation=superproject` so submodule updates reuse the superproject's modules;
2. runs `setup/provision_repo.sh` against the worktree (the same provisioner the container entrypoint uses — venv sync if stale, console-script bridge, git hooks, `git golc` alias);
3. writes its own pid to `<project>/.git/worktrees/<name>/cw-session.pid` for the session's duration — the session lock, and how `cw list` / `cw clean` tell the session is live;
4. spawns `<worktree>/.venv/bin/cw ss --in-place …` with the env extended to activate the worktree's `.venv`.

On exit: `--drop` removes the worktree (`git worktree remove --force` + `git branch -D worktree-<name>`); otherwise `cw` warns if the worktree isn't landed on `origin/master` and, in an interactive session, offers to drop it (non-interactive sessions keep it — run `cw clean` later).

Layout on disk:

- `<project>/var/cw/worktrees/<name>/` — the worktree (regular working tree with a `.git` gitfile that points at `<project>/.git/worktrees/<name>/`), a sibling of the container workspaces under `var/cw/`.
- `<project>/.git/worktrees/<name>/cw-session.pid` — the launching `cw` process's pid, present while a host session is live (drives `cw list`/`clean` active-session detection).
- `<project>/var/cw/worktrees/<name>/.venv` — per-worktree virtualenv created on first launch by `cw` (via `setup/provision_repo.sh`).
- `~/.claude/projects/<encoded-worktree-path>/` — Claude Code's own per-project state, including the session JSONL files. The encoded path is the worktree path with `/` and `.` replaced by `-`.

### Container mode (`cw ss -c <name>`)

`/workspace` inside the container is a **fresh clone**, not a worktree. The gitfile-based worktree layout doesn't survive the container boundary (the gitfile points at a host absolute path), and a clone keeps the container's git state genuinely isolated. Layout:

- `<project>/var/cw/containers/<name>/` (host) → `/workspace` rw. Empty on first run; the entrypoint clones into it.
- `<project>` (host) → `/host-repo` ro. The clone uses `--shared`, so the container reuses the host's `.git/objects` via alternates instead of duplicating them.
- a per-launch **scoped credential store** injected into `/home/cw/.ppp`. Before the container starts, the host resolves only the secrets the session actually uses into an **in-memory** tar and `docker cp`s it into the created-but-unstarted container — there is no host-side store and no bind mount. It carries one file per resolved secret (its raw text) plus a `credentials.json` registry covering exactly those, so the in-container resolver is bounded to the scoped set — any other secret resolves to a clean `SecretNotFound`. Hydration is **strict** — a missing secret raises on the host before the container is created. Living in the container's own writable layer, the store dies with the container: `--rm` removes it on normal exit, and an orphaned container (a killed `cw`) is reclaimed by `cw clean`'s container GC — secret cleanup piggybacks on the container lifecycle, so no host directory ever holds plaintext and no exit-sweep or signal handlers are needed. See "Scoped credential hydration" below.
- **github** and **aws** are ordinary scoped secrets — no out-of-band `/run/secrets/github_token` mount, no `~/.aws` mount. Each carries a static **install hook** in the registry that the entrypoint applies generically via `eval "$(credentials install-hooks)"` after venv activation: `github` → `GH_TOKEN` + `git credential.helper`; `aws` → materializes the value to `$HOME/.aws/credentials`, the path the CLI reads by default (so writing it there is the whole install — no env var). Each hook pulls its value via `credentials get <name>` at eval time, so it carries no interpolated file path. No per-secret logic lives in the entrypoint.
- `/var/run/docker.sock` (host) → same path in the container, **only where docker work happens**. Lets deploy scripts run `docker build`/`docker push` against the host daemon — no nested runtime — at the cost of giving in-container processes API-level control over host docker. The entrypoint reconciles the in-container `docker` group's GID with the bind-mounted socket's GID so `cw` can use it without sudo. Claude code sessions (`cw ss`/dive-in) always get it; a `--bro`/`ask` container gets it only when the bro declares `needs_docker` (just `Devoops`, the deployer), so other bros (ppp-dev, Librorian / PM / assistant) don't — the scoped boundary then holds against prompt-injection exfiltration via docker.
- the session's **broker channel** socket, bind-mounted at the fixed `/run/broker.sock` (see "The broker channel").

Inside the container, the entrypoint (running as root first):

1. Aligns the `cw` user's UID/GID with whoever owns `/workspace` on the host, then re-execs as `cw` (skipped on Docker for Mac when the bind mount reports root-owned via virtiofs — remapping to UID 0 would make claude refuse `--dangerously-skip-permissions`).
2. Does **not** seed `~/.claude/` from the host — `cw` provisions everything the container needs explicitly (constructed `~/.claude.json` and `~/.claude/settings.json`, synced credentials; see "Container credential isolation" below). Host machine state (caches, plugins, daemon/session state) deliberately stays on the host.
3. Copies the host's `~/.gitconfig` into the writable container `$HOME` and marks `/workspace` as a safe git directory.
4. On first run, clones `/host-repo` into `/workspace` with `--shared`, retargets `origin` to the host's upstream (converting `git@github.com:` to `https://github.com/` so token auth works), and adds `host` as a local remote (pointing at `/host-repo`) for fetching commits that haven't been pushed yet. Branches `worktree-<CW_NAME>` from the host repo's current `HEAD` (the clone's checkout, matching host mode), or from `CW_BASE_REF` when `--into <ref>` was passed. `refs/remotes/origin/master` is still ref-refreshed from the host for later clean/rebase checks.
5. Initialises submodules from the matching host-local paths in `/host-repo` (since `.gitmodules` uses SSH URLs the container can't auth to), skipping any submodule the host hasn't initialised.
6. Installs a `pre-push` hook that blocks non-fast-forward pushes, and blocks direct pushes to `master`/`main` when running as the bro identity (`GIT_AUTHOR_EMAIL=dzhioev+bro@gmail.com`).
7. Reuses the venv baked into the image: symlinks `/workspace/.venv` to `/opt/cw-venv` (deps + editable project + the `_entrypoints.py` console-script bridge, all installed at build time, the module finder pointing at `/workspace`) and stamps `provision_repo.sh`'s skip marker plus `CW_VENV_BAKED=1`, so both the slow `uv sync --all-groups` and the `sync-scripts --entrypoints` regen are avoided on every launch. The image tag pins `pyproject.toml` + `uv.lock`, so the baked env (and the bridge, a pure function of `[project.scripts]`) always matches a session's clone. `provision_repo.sh` then only installs git hooks + the `git golc` alias. Falls back to a fresh `uv sync` + regen when `/opt/cw-venv` is absent (older image) or `/workspace/.venv` already exists (reused workspace). See `setup/container/Dockerfile`.
8. Activates the venv so child processes (hooks, MCP servers, Bash tool) inherit it.
9. Execs the container command — for a `cw ss -c` session, `cw ss --in-place …`, the same in-place session runner host mode spawns, resolved from the venv activated above; everything from here is the runner's, identically to host mode. The entrypoint itself is flavor-blind — no MCP or skills logic.

The container image is built lazily — `cw/docker.py:_image_tag()` hashes `setup/container/` plus `pyproject.toml` and `uv.lock`, and `_ensure_image` rebuilds when the tag is missing. Tag format: `ppp-cw:<12-char-sha>`.

Network is not restricted by design.

When `cw ss -c` exits, the workspace directory and the per-session host-side state stay on disk for the next session, unless `--drop` was passed (in which case both `var/cw/containers/<name>` and `~/.claude/cw-sessions/<name>` are removed).

#### Container credential isolation

The container does **not** bind-mount `~/.claude.json` from the host, nor does the host's OAuth credentials file ever enter the container. Instead, `cw/docker.py:_docker_create_argv` seeds a container-private `~/.claude.json` under `~/.claude/cw-sessions/<name>/` and bind-mounts that, while session auth comes from the `claude_code` token (below):

- `~/.claude/cw-sessions/<name>/.claude.json` — constructed once per workspace from an explicit config (`installMethod: global` to match the image's `npm i -g` install, `hasCompletedOnboarding`, `autoUpdates: false`, `projects["/workspace"].hasTrustDialogAccepted: true` so non-`--auto` sessions skip the folder-trust prompt, and `officialMarketplaceAutoInstall{Attempted,ed}: true` so claude doesn't re-fetch the official plugin marketplace at startup — it's baked into the image) plus the host's account-identity fields (`oauthAccount`, `userID`) so the session starts logged in. Host machine state (project paths, trust history, usage counters, feature caches) is **not** copied. Missing identity is fatal — `cw` aborts asking you to log in on the host first. Subsequent sessions keep whatever the container last wrote. Stops per-project mutations (mcpServers, allowedTools, hasTrustDialogAccepted) from being usable to escalate into the next host session.
- **Session auth (`CLAUDE_CODE_OAUTH_TOKEN`)** — native claude code sessions authenticate with this env var, which the **required** `claude_code` secret (a `claude setup-token` long-lived token) exports via its registry install hook. Claude Code reads it above any credentials file, and one stable bearer is shared by every session — so no OAuth credentials file is mounted or synced, and none of the cross-session refresh-token rotation that forced the periodic `/login`. Being required, a missing token fails loudly on the host at scoped-store hydration, before the container starts (not as a turn-1 401 inside it). `--bro`/`do` containers run `claude --bare` against the `anthropic` api key and request the token only on the native-session path. Host-mode sessions get the same var injected into the claude subprocess env directly (best-effort there — host mode falls back to the host's own `~/.claude/.credentials.json` when the secret is absent).
- `~/.claude/settings.json` — constructed fresh each launch (not mounted from the host) into `cw-sessions/<name>/`, holding only UX prefs (spinner verbs, reduced motion, feedback-survey opt-out), a `statusLine` (`session-log-statusline`) that pins a red warning when session-log sync is failing, silent otherwise, and an explicit `enabledPlugins` opt-in for the `pyright-lsp` Python language server (the host plugin set no longer leaks in, so the container enables it itself). The plugin is *installed* at image-build time (`setup/container/Dockerfile`) and staged at `/opt/claude-plugins-seed`, which the entrypoint copies into the bind-mounted `~/.claude/plugins` on first run — enabling alone isn't enough, claude would otherwise prompt the "LSP Plugin Recommendation" on `.py` files. Host permissions, hooks, plugins, and model/effort pins do not leak in; the repo's `/workspace/.claude/settings.json` layers on top, and per-launch flags (`--fast`, `--effort`) own session config.
- `~/.claude/cw-sessions/<name>/` (host) → `/home/cw/.claude` (container). Per-workspace overlay of everything else.
- `~/.claude/projects/-workspace/` (inside `cw-sessions/<name>/`) — where Claude Code stores the session JSONL for `/workspace`. The encoded path `-workspace` is fixed (just `/` replaced with `-`).

This means each container session has its own private `~/.claude.json` (so MCP server allow-lists are per-workspace) and its own session log directory, while authenticating with the shared, non-rotating `claude_code` token (so no session's refresh can blow away another's).

#### Scoped credential hydration

`cw.run_in_container` hydrates only the secrets the session's bro declares into the container's `~/.ppp`, scoping each container to a minimal credential set.

- **The manifest.** A bro's `needed_secrets()` (`bro/bro.py`) is the union of each declared MCP server's and data source's `needed_secrets` (read per-instance) and the bro's MRO-collected `extra_secrets`. It deliberately omits the LLM key — that is added only by surfaces that run the bro as an LLM process. `bro show <name>` lists it. Components declare what they read: `flow.MCPServer` → `notion` (+ `focus` when it holds the focus tools, derived from the held tools), `infra.MCPServer` → `infra, focus, aws`, `TMDb` → `tmdb`, `WebSearch` → `brave`, `chat_gpt.LLMSpec.needed_secrets()` → `openai`. `extra_secrets` is the escape hatch for environment needs no component expresses: ppp-dev → `github`, devoops → `aws`.
- **Which bro.** Scope keys on the manifest, not the launching CLI — `do` (ask / do-task) and `cw ss` both containerise through `cw.run_in_container`. `cw ss --bro <name>` and `ask <name>` use that bro; a no-`--bro` `cw ss` (including dive-in, which sets `CW_BRO=ppp-dev`) themes as ppp-dev.
- **Per-surface sets (strict → request only what's used).** The three container surfaces use the bro differently, so each requests a precise set:
  - native claude code session (dive-in / plain `cw ss`): baseline `{session_log, trails}` + the bro's `extra_secrets` + `flow_mcp` (when `--mcp http`). It drives the bro's *skills* (bash → `extra_secrets`) and its flow via `--mcp`, not the bro's in-process MCP/data-source toolset — so a dive-in needs neither `notion` nor `openai`.
  - `--bro` (`claude --bare` serving the bro's own in-process MCP servers): the bro's full `needed_secrets()` + baseline + `anthropic` (apiKeyHelper), plus the bro's `optional_secrets()` hydrated best-effort (e.g. the LLM key behind a data source's query-focused fetch summary — present → wired, absent → skipped, not a launch failure). The native surface above gets no optional tier (it doesn't mount that toolset).
  - `ask` / `do-task` (bro runs as a chat_gpt process): `needed_secrets()` + `llm_spec.needed_secrets()` + `optional_secrets()` (best-effort) + `trails` (recording is mandatory).
  - `--grant` / `--revoke` (repeatable, container only) then layer a per-session override on top via `credentials.apply_grant_revoke`: final set = `(computed | granted) - revoked`. Strict — a grant of a secret already in the set, or a revoke of one absent from it, raises and stops (a no-op flag is a mistake to surface). `do` (ask / call / do-task) takes the same two flags and applies them to the bro's manifest set before the hop. Add AWS access to a session with `--grant aws`.
- **Hydration.** `credentials.build_scoped_store(names, optional=…)` returns an in-memory map — one entry per resolved secret (its raw text) plus a scoped `credentials.json` (carrying each secret's `install` hook); `cw` packs it into a tar and `docker cp`s it into the pre-start container's `~/.ppp`, so no plaintext ever lands on a host file. The required tier (`names`) is strict: a name not in the host registry raises (a manifest typo), and a declared name with no value also raises (`SecretNotFound`) — both fail loudly on the host, before the container is created. The `optional` tier is best-effort: each name (minus those already required) is materialised when resolvable and silently skipped otherwise, so an absent optional secret degrades a component instead of failing launch.
- **`aws` is an ordinary secret.** `aws` → `aws_credentials`, the host's AWS shared-credentials INI at `~/.ppp/aws_credentials`. Its install hook materializes the value to `$HOME/.aws/credentials` — the path the aws CLI/SDK reads by default, so writing it there is the whole install (no `AWS_SHARED_CREDENTIALS_FILE` needed). Deliberately not the `~/.ppp` resolver source: `credentials get aws > ~/.ppp/aws_credentials` would truncate it via `> samefile`. No `~/.aws` mount, no `AWS_*` forwarding.
- **Install hooks.** A secret can declare a static `install` shell hook in the registry that pulls its value via `credentials get <name>` at eval time (no interpolated path, so no quoting/injection surface); `credentials install-hooks` emits the hooks for every secret that declares one (in a scoped container the registry is exactly the hydrated set) and the entrypoint `eval`s them, so wiring a secret into its consumer (git, the aws CLI) is declarative, not entrypoint-special-cased.
- **The container side.** `base.credentials._load_registry()` searches both `<project>/.configs` and `~/.ppp` for `credentials.json`, so the scoped registry injected at `~/.ppp` takes effect; the built-in registry (and the deployed services that synthesize `<project>/.configs`) are unchanged. The `docker cp`'d files land owned by the tar's uid, so the entrypoint `chown -R`s `~/.ppp` to `cw` after its uid remap — keeping the 0600 secret files readable by the resolver and the install hooks on both Linux (cw remapped to the host uid) and Docker for Mac (remap skipped, cw keeps its image uid).

#### "Already in a container" fallback

If `cw ss -c` is invoked from inside an already-containerised session (`CW_IN_CONTAINER=1` is set by the Dockerfile), `cw/session.py:cw` falls back to host mode rather than trying to nest containers. `--bro` sessions are the exception: they are fenced to the container (the scoped `anthropic` secret is their auth model), so nesting one errors out instead of degrading.

### The broker channel

Every session runs as the root peer of a **broker** (see `broker/CLAUDE.md`): the outer provisions a unix socket at `var/cw/broker/<ulid>.sock` (gitignored under `var/`; the control dir is deliberately shallow so the bind path fits the ~108-byte `sun_path` limit; dir `0700`, socket `0600`), points `BROKER_CHANNEL` at it, and supervises the session from the broker's event loop until it exits. The modes differ only in the spawner (`cw/spawn.py`):

- container — the attached container launch (`cw/containers.py:_run_root_via_broker` + `DockerSpawner`), with the socket bind-mounted at the fixed `/run/broker.sock` and `BROKER_CHANNEL` pointing there;
- host — the runner as a plain subprocess (`cw/session.py:_run_host_root_via_broker` + `ProcessSpawner`), `BROKER_CHANNEL` pointing straight at the socket path — no bind-mount hop.

The live broker registers the substrate's built-in `ping` handler, so a session can verify its channel with `broker request ping '{}'`; further request types arrive with their consumers (summon). Because the channel exists unconditionally for every session, a broker defect would sit on every launch — `BROKER_DISABLED` (presence-checked, parallel to `TRAILS_DISABLED`) is the kill-switch that skips broker provisioning/dispatch entirely, and a venv that can't import broker degrades the same way with a warning; both fall back to the direct launch (`docker start -a -i` in container mode, a plain runner spawn on host). The post-exit finish (keep-or-drop, resume hint, `--drop`) runs after `Broker.run()` returns, so it is identical on both paths.

### The outer↔inner contract

The outer spawns the workspace's own cw, so the workspace's code must understand `cw ss --in-place`. A workspace based on a ref that predates the contract fails loudly at launch — a missing `<worktree>/.venv/bin/cw` on host, or the inner parser rejecting `--in-place` — rather than silently running old code. The remedy splits by mode: a host worktree can be rebased (`git -C <worktree-path> rebase origin/master`; provisioning refreshes the venv on the next launch) or recreated, but a container clone can only be recreated — its shared object store records the in-container `/host-repo` alternates path, which host-side git cannot resolve, so a host-side rebase fails there. `cw clean` sweeps abandoned workspaces.

## The in-place session runner

`cw ss --in-place` (`cw/runner.py`) is the inner layer: it assumes its cwd is a prepared workspace with the workspace venv active, and owns everything that runs next to claude. `--in-place` is help-suppressed — an internal seam, not a user surface — and skips the outer-only policy gates (see "The outer layer").

In order, the runner: resolves `--resume` from its cwd's projects dir — claude's own path encoding maps the workspace path to `~/.claude/projects/<encoded>` (host `<encoded-worktree-path>`, container `-workspace`), one derivation for both modes; exports the bro git identity when `--auto`; starts the session-local MCP server when one is needed and surfaces bro skills (both below); builds the claude argv (below) and captures `CW_SESSION_CONTEXT` (see "Forwarded env vars"); gates on the server's `/health` for `--bro`; then runs `claude`, forwarding SIGTERM to it (claude's raw-mode TTY already absorbs Ctrl-C, but a SIGTERM aimed at the runner — `docker stop`, kill — would otherwise strand claude) and waiting. After claude exits it stops the server and, for `--bare` sessions, does the one-shot transcript sync (see "Hooks").

### The claude argv

One builder for both flavors (`cw/claude_argv.py:build_claude_launch`): model, the merged `--settings` (fastMode plus, under `--bro`, the apiKeyHelper), `--effort`, the forwarded claude args, the skills `--add-dir`, and prompt seeding are handled once; only the flavor forks:

- **native** — the full harness plus the cw-injected `--append-system-prompt` (see "Auto-injected system prompt"), `--dangerously-skip-permissions` under `--auto`, and the flow `--mcp-config` when `--mcp` is passed (`http` → the deployed server from the `flow_mcp` secret; `local` → the session-local server below).
- **bro** — `--bare --strict-mcp-config --tools ''`: no project/user CLAUDE.md, no host MCP servers, no built-in tools, and only the bro's MCP namespaces allowed (`--allowed-tools mcp__<ns>__*`), with the bro's `claude_system_prompt` as `--system-prompt`. Auth is the `anthropic` secret read through the workspace's own `setup/print_anthropic_key.sh`, wired as `apiKeyHelper` in the merged `--settings` — a helper avoids the "Detected a custom API key" prompt that `ANTHROPIC_API_KEY` would trigger every session, and flag-level `--settings` (flagSettings, not project/local) means claude executes it without a workspace trust gate.

### Session-local MCP serving

`--mcp local` and `--bro` sessions get their MCP tools from a session-local HTTP server the runner owns — one mechanism for both execution modes and both flavors, dying with the session. The runner starts `mcp-server <spec> --http` (`flow` for `--mcp local`, `bro:<name>` for `--bro`) from the workspace's venv on an OS-assigned port (a fixed port would collide between concurrent sessions sharing a netns) with a per-session bearer token. `mcp-server --http --port 0 --port-file <path>` binds the socket *before* its heavy imports and publishes the real port through the port file, which the runner polls (milliseconds) before building the `--mcp-config` and launching claude; a claude connect that lands mid-import sits in the TCP backlog until uvicorn accepts on the pre-bound socket. The server is terminated when claude exits; a SIGKILLed runner orphans it. Its output lands in a `cw-mcp-*` temp dir alongside the port file.

For `--bro` the server serves the bro's MCP servers and data sources (`claude_bro_mcp_servers()` — declared servers plus the `skill` service tool, no `raise`), one streamable-HTTP endpoint per tool namespace (`/flow`, `/bro`, `/<name>-source`, …); the argv builder mounts each endpoint under its namespace as the claude server key, so tools surface as `mcp__<namespace>__<tool>` (`mcp__flow__get_projects`, `mcp__bro__skill`), matching the `prompts/tool_names.md` convention — which is why the session's `--system-prompt` is the bro's `claude_system_prompt`, the composition that carries that file as its tool-name rule rather than the bro-native `ns__tool` block. The runner also polls the server's `/health` until ready *before* launching claude, so the heavy bro import (bro graph → flow → notion → boto3, seconds) is paid off claude's critical path and the first turn — which a seeded `-p` prompt fires the moment the REPL is up — already has every tool connected; the runner's own argv build overlaps the server's import, so much of the wait is already paid when the gate is reached.

In container mode the server runs inside the container, so the scoped credential store carries the served tools' own secrets (for `--mcp local`, `flow.MCPServer`'s notion + focus) instead of `flow_mcp`.

### Bro skills

A bro's skills (`bro/bros/<bro>/skills/*.md`, MRO-merged) reach the session two ways, keyed on the flavor:

- a **native themed session** (`CW_BRO` in the environment — `dive-in` sets `ppp-dev`) gets them as `/<name>` slash commands: the runner populates a per-session `tempfile.mkdtemp` directory with `.claude/skills/<name>/SKILL.md` symlinks (`cw/bro.py:_populate_bro_skills`) and passes it to claude via `--add-dir <tmp>`, so concurrent sessions on the same repo don't share the project's `.claude/skills/`;
- a **`--bro` session** reaches them through the `bro::skill` MCP tool instead — `--bare` exposes no typed slash commands, so the symlinks would be inert and the runner skips them; the agent calls `skill(<name>)` to load a skill's body and follow it.

## Flags that shape the session

These flags apply to `cw ss` and (with the exception of `-c` / `--drop` / `--mcp` / `--bro` / `-p`) are also exported via `cw.add_forwarded_flags` so the `dive-in` wrapper can pass them straight through without per-flag plumbing.

- **`-c`, `--container`** — container mode (see above). Defaults off; host mode is the default.
- **`--drop`** — remove the workspace on exit without prompting.

  In host mode this means `git worktree remove --force` and deleting the `worktree-<name>` branch (skipping the keep-or-drop offer `cw` makes otherwise); in container mode it means `rm -rf var/cw/containers/<name>` and `~/.claude/cw-sessions/<name>`.
- **`--mcp [http|local]`** — wire up the flow MCP server.

  `http` (default when the flag is bare) connects to the deployed server at the `flow_mcp` secret's `url` with a bearer token.

  `local` serves the workspace's flow code from a session-local HTTP MCP server that dies with the session (see "Session-local MCP serving").

  Without `--mcp`, no flow MCP is connected — Claude doesn't see task/project tools.
- **`--bro <name>`** — launch a clean session under a chosen bro persona (system prompt, MCP servers, tools) using `claude --bare` and only the bro's MCP tools: the bro flavor of "The claude argv", served by "Session-local MCP serving", with skills via the `bro::skill` tool ("Bro skills").

  **Requires `-c`** (the bro flow uses an Anthropic Console API key, not the user OAuth, and is fenced to the container). **Requires the `anthropic` secret**. Mutually exclusive with `--mcp` and `--auto`.
- **`--auto`** — autonomous mode: passes `--dangerously-skip-permissions` to claude and switches the git identity to bro (`Bro <dzhioev+bro@gmail.com>`).

  **Requires `-c`** (a sandbox is mandatory for skip-permissions). Adds a `Land mode: PR` line to the system prompt. Cannot be combined with `--bro`.
- **`--fast`** — enables fast mode for the session, carried in the session's one merged `--settings` (fastMode plus, under `--bro`, the apiKeyHelper). Off by default regardless of host settings, so individual `cw ss` invocations are predictable.
- **`--grant <secret>` / `--revoke <secret>`** (repeatable) — per-session override of the computed scoped set: the final set is `(computed | granted) - revoked`, applied by `credentials.apply_grant_revoke`.

  `--grant` elevates a session with a secret the computed set doesn't carry (e.g. `--grant aws` for AWS access, whose install hook materializes the credentials to `$HOME/.aws/credentials`; or `--grant gmail_creds --grant google_api` to run the emails e2e in a scoped container instead of host-native); an unknown name still fails loudly at hydration. `--revoke` tightens a session ad-hoc.

  Strict, and the launch stops on misuse: granting a secret already in the set, or revoking one not in it (including granting/revoking the same name twice, or naming it in both lists), is an error — a no-op flag is a mistake to surface, not swallow.

  **Requires `-c`**: host mode is unscoped (the process reads `~/.ppp` directly), so a revoke there couldn't actually restrict the session — passing them without `-c` errors rather than silently no-op.
- **`--effort {low|medium|high|xhigh|max}`** — forwarded as `claude --effort` (thinking effort). Defaults to `xhigh`; pass an explicit level to override.
- **`--resume`** — resume the latest Claude session in this workspace.

  The outer fails fast when the workspace has no recorded session (see "The outer layer"); the runner then resolves the newest `.jsonl` in its cwd's projects dir to a session id and adds `--resume <id>` to the claude argv. Skips the initial prompt. Cannot be combined with `--drop` or `-p/--prompt`.

  After a container session exits, `cw` overwrites Claude's printed resume hint (which suggests `claude --resume <id>` — only valid inside the container) with the host-side one. The replacement is this session's own `cw ss …` invocation with `--resume` added and the create-only inputs dropped (`--drop`, `--into`, the prompt, forwarded claude args), so it carries the session's actual flags (`--auto`, `--grant`, `--effort`, `--mcp`, `--bro`, …) rather than a fixed approximation. `start_session` builds it via `SessionSpec.resume_variant().to_command_argv()` — the same `to_command_argv` that produces `CW_COMMAND` — and stashes it in `CW_RESUME_COMMAND`.
- **`--into <ref>`** — base a *new* workspace on git `<ref>` (branch/tag/sha) instead of the default (the host repo's current `HEAD`, in both container and host mode). `cw` resolves the ref against the host repo to a sha; when it isn't resolvable there — e.g. a feature branch that only lives on origin (pushed from another container, as the `/feature` per-stage flow does) — `cw` fetches the ref from origin into the host repo first, then resolves it. The container then reaches that commit via `/host-repo`'s shared objects, and the host worktree bases its new branch on it. Ignored once the workspace exists (the clone/worktree is created only on first run); cannot be combined with `--resume`. Forwarded, so `dive-in --into <ref>` works too — handy for basing a session on a ref you don't have checked out.
- **`-p / --prompt <text>`** — the initial prompt for the session.

  `cw ss` prepends the auto-injected base prompts (via `cw/system_prompt.py:_load_base_prompts`) using `--append-system-prompt`, and forwards the text via `--`.

Trailing positional args after `<name>` are forwarded to `claude` verbatim (`argparse.REMAINDER`).

## Auto-injected system prompt

For every non-bro `cw ss` session (regardless of mode), `cw/system_prompt.py:_load_base_prompts` builds a base prompt from `prompts/shared/*` and the top-level reference docs the loader registers (see `prompts/CLAUDE.md` for the inventory). The result is appended to claude's system prompt via `--append-system-prompt`. `shared/` is also injected into every bro; the top-level reference docs are Claude-Code-specific and are **not** injected when `--bro` is used (the bro flow runs `--bare` with its own `--system-prompt`) — except `tool_names.md`, which reaches `--bro` sessions through the bro's `claude_system_prompt` composition (see `bro/CLAUDE.md`).

## Forwarded env vars

Wrappers and hooks rely on a small set of env vars:

- `CW_NAME` — workspace name. Set by `start_session` in both modes (host and container), and additionally passed into the container via `-e CW_NAME=<name>` for the entrypoint. The entrypoint uses it to pick the branch name (`worktree-$CW_NAME`); `cw banner` reads it to render the session header.
- `CW_HOST_WORKSPACE` — host-side absolute path to the container workspace dir (`<project>/var/cw/containers/<name>`). Set by `cw` in container mode only (`-e CW_HOST_WORKSPACE=<path>`) so `cw banner`, running inside the container, can tell the user where their `/workspace` mount actually lives on the host.
- `CW_COMMAND` — the user-visible `cw ss …` invocation, reconstructed by `start_session` (via `SessionSpec.to_command_argv`) for telemetry and the banner. Defaulted into `PPP_SHELL_COMMAND` if that's not already set.
- `CW_RESUME_COMMAND` — the `cw ss … --resume <name>` command that reproduces this session, built by `start_session` from the same flags as `CW_COMMAND` minus the create-only inputs. Read by `_replace_container_resume_hint` to overwrite Claude's container-only exit hint (see `--resume`).
- `CW_BRO` — names the bro the session is themed as. Set by `start_session` when `--bro <name>` is passed, and unconditionally by `dive-in` (`ppp-dev`); forwarded into the container via `_DOCKER_FORWARD_ENV`. For a native themed session the runner surfaces that bro's skills as slash commands (see "Bro skills"). Also drives `cw banner`'s ASCII Bro logo + bro-name header.
- `CW_TASK_ID` — set by `dive-in` when it has resolved a task; consumed by `setup/claude_commit_footer.py` to add a `Task: <url>` line to commit messages.
- `CW_SESSION_CONTEXT` — the session's launch context as a JSON list of typed records (system prompt, git state, MCP servers, root CLAUDE.md), built via `cw/session_context.py` by the in-place session runner, next to claude, in both modes. `sync-session-log` stores it on the DynamoDB item's `context` attribute; `rewind` renders it as a `SESSION CONTEXT` preamble. It captures what the model was told but the transcript omits — Claude Code's base harness prompt stays in-process and is not included.
- `CW_BASE_REF` — the sha `--into <ref>` resolved to. Container mode only, and only when `--into` is passed (`-e CW_BASE_REF=<sha>`). The entrypoint checks it out as the new clone's `worktree-<name>` branch instead of the default (the clone's `HEAD`, i.e. the host's current checkout); the sha's objects are already reachable through the clone's `/host-repo` alternates. (Host mode applies the base inline via `git worktree add … <ref>`, no env var.)
- `CW_IN_CONTAINER=1` — set by the Dockerfile. Detected by `cw/session.py:cw` to fall back to host mode when nesting would be requested.
- `BROKER_CHANNEL` — the address of the session's broker channel, set at launch in both modes: `unix:/run/broker.sock` in a container (the bind-mounted socket), `unix:<project>/var/cw/broker/<ulid>.sock` on host (the socket itself). Read by `broker.client.Client.from_env` — the `broker` CLI and bro's `BroChannel` ride it — and everything on it is inert when unset (`BROKER_DISABLED`, or a workspace provisioned before broker existed).
- `BROKER_DISABLED` — launcher-side presence-checked kill-switch: the session gets no channel socket and no `BROKER_CHANNEL` (see "The broker channel"). Checked before any broker import (`cw/containers.py:_broker_enabled`).
- Plus the standard `GIT_AUTHOR_*` / `GIT_COMMITTER_*` — explicitly forwarded into the container via `_DOCKER_FORWARD_ENV`. (github and AWS reach the container as the scoped `github` / `aws` secrets via their install hooks, not as forwarded env; an ambient host `GITHUB_TOKEN` is deliberately not forwarded.)

## Hooks

`cw` itself doesn't run hooks — Claude Code does, based on `.claude/settings.json`. The ones that interact with `cw`'s lifecycle:

- `SessionStart` / `SessionEnd` → `.claude/hooks/sync-session-log-start.sh` / `sync-session-log-stop.sh` — bracket the session with calls to `sync-session-log` (S3 + DynamoDB transcript upload). The hooks discard `sync-session-log`'s stderr, so its only durable failure signal is the health file it writes (`session_log_health.py`), which the statusLine and `cw banner` surface. `--bro` sessions run `claude --bare` (minimal mode), which runs no hooks at all, so this pair never fires there — instead the in-place session runner does a one-shot `sync-session-log` after claude exits (the `session_log` secret is in every claude code session's scoped baseline, so this works inside the container too). Without it `--bro` sessions would stay invisible to `sessions` / `rewind`.

Worktree creation, provisioning, and the keep-or-drop offer are **not** hooks — `cw` (host mode) owns them directly (see "Host mode" above), so the only Claude Code hooks left are the session-log pair.
