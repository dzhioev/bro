# cw

`cw` launches Claude Code in an isolated per-task workspace. It owns the workspace lifecycle (create / provision / list / clean), wires up MCP and bro personas, and chooses between two execution modes — a host-side git worktree or an isolated Docker container — without changing what the user sees on the inside.

This document explains *how it actually works*: where things live on disk, what each flag does, how credentials and settings move between host and container, and which env vars are produced and consumed. For the source of truth on flags, run `cw --help` / `cw ss --help`; the implementation lives in `cw.py`.

## Subcommands

`cw` is a subcommand dispatcher:

- **`cw ss <name>`** — start a session in the workspace `<name>`. Creates the workspace on the fly if it doesn't exist. This is the workhorse; everything below describes its behaviour.
- **`cw list`** — list every workspace under this project. Each entry shows a state badge (`[.]` live host session, `[o]` live container session, `[x]` abandoned), the workspace name (container names are prefixed `c:`), an age (last filesystem touch), and the first user prompt of the latest session.
- **`cw clean [--force] [--dry-run] [<ref> ...]`** — remove workspaces with no uncommitted or unpushed work. Without args, scans both namespaces. With explicit `<ref>`s, restricts to those (`name` for host, `c:name` for container). `--force` removes despite dirty state; `--dry-run` only prints. Safety is shared with `check-clean`. Container workspaces may hold files owned by an in-container uid (root, or `cw` ≠ the host user) that a host-side `rmtree` can't unlink; removal first tries `rmtree`, then escalates to deleting from inside a throwaway root container (any local `ppp-cw` image). A workspace whose removal fails is logged and skipped rather than aborting the sweep; a non-empty failure count makes the command exit non-zero.
- **`cw check-clean [<ref>]`** — probe a single workspace (or the cwd if omitted). Exit 0 if it's safe to drop; exit 1 with reasons on stderr otherwise. The hook `.claude/hooks/check-worktree-landed.sh` calls this to populate the keep-or-drop prompt at session end.
- **`cw exec <name> [<cmd>…]`** — exec into the container backing workspace `<name>`. With no command, opens an interactive bash with `/workspace/.venv` sourced; otherwise runs `<cmd>` in the same activated env. Runs as the `cw` user (`docker exec -u cw`), matching the entrypoint's `gosu` drop — without it `docker exec` would default to the image's root and write root-owned files into the bind-mounted `/workspace` that the host user can't later remove. Container workspaces only; the `c:` prefix is accepted but optional.
- **`cw banner [--llm]`** — print the banner: workspace name (with the `c:` prefix from `cw list` for container workspaces), the canonical `cw ss …` invocation (`CW_COMMAND`), `/workspace` plus its host bind-mount path on separate lines, the `cw exec <name>` hint (labelled `docker shell:` because it drops into a shell *inside* the container), the outer launching command (`PPP_SHELL_COMMAND`), and the user-typed prompt extracted out of it (split at the last `--new`/`-p`/`--prompt`/`--` marker). For `--bro` sessions, prepends the ASCII Bro logo with a dim `// <bro>` signature on its bottom line. The container image's `~/.bashrc` runs `cw banner` once per interactive shell so users see it on `cw exec` entry. The visual mode renders the `@prompt@` placeholder and the `prompt:` label in bright-white bold for emphasis. The `--llm` flag emits the same facts as plain `key: value` lines (no ANSI, no logo) for Claude itself to read at session start via the Bash tool (see `prompts/environment.md`); the prompt body is deliberately omitted there since the LLM already has it as its first message — `launch_command` retains its trailing marker (`dive-in --new `) as the signal that a seed prompt exists.

A workspace is "clean" when:

- (a) `git status --porcelain` is empty,
- (b) `HEAD` is an ancestor of `origin/master`, and
- (c) every submodule's pinned commit is reachable from its remote default ref.

For container workspaces the ancestry checks run against the host project's `.git` (the container clone's own `origin` is an HTTPS URL the host can't reach without credentials). The check reads the clone's `HEAD` sha and runs `merge-base`/`rev-list` in the host repo with the clone's object store exposed as a read-only alternate, rather than fetching the clone's `HEAD` into the host repo — `cw clean` checks every workspace concurrently, and a per-check fetch raced on the shared `FETCH_HEAD`/ref locks (yielding wrong commit counts and a flapping clean/dirty verdict). `origin/master` is fetched once up front for the same reason; `check-clean` (single workspace, no concurrency) still fetches inline.

## Host mode vs container mode

`cw ss` has two execution modes. Host is the default; `-c` / `--container` selects the container.

### Host mode (`cw ss <name>`)

Runs `claude -w <name>` from the project root, with the env extended to activate the worktree's `.venv`. Claude Code itself owns the worktree lifecycle: it triggers the `.claude/hooks/worktree_create.sh` hook on first use (which is what actually does `git worktree add` plus the project-specific tweaks — `worktree-<name>` branch, `CLAUDE_BASE` marker file, `submodule.alternateLocation=superproject`), and on exit it shows the keep-or-drop prompt. `cw` itself does not directly create or remove worktrees in host mode unless `--drop` is passed (in which case it `git worktree remove --force` and `git branch -D worktree-<name>` after `claude` exits).

Layout on disk:

- `<project>/.claude/worktrees/<name>/` — the worktree (regular working tree with a `.git` gitfile that points at `<project>/.git/worktrees/<name>/`).
- `<project>/.git/worktrees/<name>/CLAUDE_BASE` — marker recording the `HEAD` at worktree creation (set by `worktree_create.sh`).
- `<project>/.claude/worktrees/<name>/.venv` — per-worktree virtualenv created on first run by `.claude/hooks/session_start.sh`.
- `~/.claude/projects/<encoded-worktree-path>/` — Claude Code's own per-project state, including the session JSONL files. The encoded path is the worktree path with `/` and `.` replaced by `-`.

### Container mode (`cw ss -c <name>`)

`/workspace` inside the container is a **fresh clone**, not a worktree. The gitfile-based worktree layout doesn't survive the container boundary (the gitfile points at a host absolute path), and a clone keeps the container's git state genuinely isolated. Layout:

- `<project>/var/cw/containers/<name>/` (host) → `/workspace` rw. Empty on first run; the entrypoint clones into it.
- `<project>` (host) → `/host-repo` ro. The clone uses `--shared`, so the container reuses the host's `.git/objects` via alternates instead of duplicating them.
- a per-launch **scoped credential store** (host) → `/home/cw/.ppp` ro. Before the container exists, the host resolves only the secrets the session actually uses into an ephemeral dir under `~/.cache/ppp-cw/secrets/<name>` and mounts that as the container's `~/.ppp`. It carries one file per resolved secret (its raw text) plus a `credentials.json` registry covering exactly those, so the in-container resolver is bounded to the scoped set — any other secret resolves to a clean `SecretNotFound`. Hydration is **strict** — a missing secret raises on the host before the container starts. The dir is hydrated fresh each launch and removed on exit (secrets never linger on disk). The store lives outside `<project>` precisely because `/host-repo` is bind-mounted into every container — a secrets dir under the project would re-leak across containers. See "Scoped credential hydration" below.
- **github** and **aws** are ordinary scoped secrets — no out-of-band `/run/secrets/github_token` mount, no `~/.aws` mount. Each carries an **install hook** in the registry (a shell template) that the entrypoint applies generically via `eval "$(credentials install-hooks)"` after venv activation: `github` → `git credential.helper` + `GH_TOKEN`; `aws` → `AWS_SHARED_CREDENTIALS_FILE` pointing at the hydrated `~/.ppp/aws_credentials`. No per-secret logic lives in the entrypoint.
- `/var/run/docker.sock` (host) → same path in the container, **only where docker work happens**. Lets deploy scripts run `docker build`/`docker push` against the host daemon — no nested runtime — at the cost of giving in-container processes API-level control over host docker. The entrypoint reconciles the in-container `docker` group's GID with the bind-mounted socket's GID so `cw` can use it without sudo. Claude code sessions (`cw ss`/dive-in) always get it; a `--bro`/`ask` container gets it only when the bro declares `needs_docker` (just `Devoops`, the deployer), so other bros (ppp-dev, Librorian / PM / assistant) don't — the scoped boundary then holds against prompt-injection exfiltration via docker.

Inside the container, the entrypoint (running as root first):

1. Aligns the `cw` user's UID/GID with whoever owns `/workspace` on the host, then re-execs as `cw` (skipped on Docker for Mac when the bind mount reports root-owned via virtiofs — remapping to UID 0 would make claude refuse `--dangerously-skip-permissions`).
2. Does **not** seed `~/.claude/` from the host — `cw` provisions everything the container needs explicitly (constructed `~/.claude.json` and `~/.claude/settings.json`, synced credentials; see "Container credential isolation" below). Host machine state (caches, plugins, daemon/session state) deliberately stays on the host.
3. Copies the host's `~/.gitconfig` into the writable container `$HOME` and marks `/workspace` as a safe git directory.
4. On first run, clones `/host-repo` into `/workspace` with `--shared`, retargets `origin` to the host's upstream (converting `git@github.com:` to `https://github.com/` so token auth works), and adds `host` as a local remote (pointing at `/host-repo`) for fetching commits that haven't been pushed yet. Branches `worktree-<CW_NAME>` from current upstream `origin/master`.
5. Initialises submodules from the matching host-local paths in `/host-repo` (since `.gitmodules` uses SSH URLs the container can't auth to), skipping any submodule the host hasn't initialised.
6. Installs a `pre-push` hook that blocks non-fast-forward pushes, and blocks direct pushes to `master`/`main` when running as the bro identity (`GIT_AUTHOR_EMAIL=dzhioev+bro@gmail.com`).
7. On first run, provisions a Linux `.venv` with `uv sync --all-groups` (the wheel cache is pre-warmed in the image; see `setup/container/Dockerfile`).
8. Activates the venv so child processes (hooks, MCP servers, Bash tool) inherit it.

The container image is built lazily — `cw.py:_image_tag()` hashes `setup/container/` plus `pyproject.toml` and `uv.lock`, and `_ensure_image` rebuilds when the tag is missing. Tag format: `ppp-cw:<12-char-sha>`.

Network is not restricted by design.

When `cw ss -c` exits, the workspace directory and the per-session host-side state stay on disk for the next session, unless `--drop` was passed (in which case both `var/cw/containers/<name>` and `~/.claude/cw-sessions/<name>` are removed).

#### Container credential isolation

The container does **not** bind-mount `~/.claude.json` or `~/.claude/.credentials.json` directly from the host. Instead, `cw.py:_docker_run_argv` seeds a container-private copy under `~/.claude/cw-sessions/<name>/` and bind-mounts that:

- `~/.claude/cw-sessions/<name>/.claude.json` — constructed once per workspace from an explicit config (`installMethod: global` to match the image's `npm i -g` install, `hasCompletedOnboarding`, `autoUpdates: false`, and `projects["/workspace"].hasTrustDialogAccepted: true` so non-`--auto` sessions skip the folder-trust prompt) plus the host's account-identity fields (`oauthAccount`, `userID`) so the session starts logged in. Host machine state (project paths, trust history, usage counters, feature caches) is **not** copied. Missing identity is fatal — `cw` aborts asking you to log in on the host first. Subsequent sessions keep whatever the container last wrote. Stops per-project mutations (mcpServers, allowedTools, hasTrustDialogAccepted) from being usable to escalate into the next host session.
- `~/.claude/cw-sessions/<name>/.credentials.json` — synced from host pre-launch and back to host post-exit, keyed on `claudeAiOauth.expiresAt`. The fresher copy wins. On macOS, the keychain is also consulted: if the keychain's expiry is newer than the host file's, the host file is rewritten from the keychain first. This preserves OAuth refresh without leaving the runtime token swap exposed.
- `~/.claude/settings.json` — constructed fresh each launch (not mounted from the host) into `cw-sessions/<name>/`, holding only UX prefs (spinner verbs, reduced motion, feedback-survey opt-out), a `statusLine` (`session-log-statusline`) that pins a red warning when session-log sync is failing, silent otherwise, and an explicit `enabledPlugins` opt-in for the `pyright-lsp` Python language server (the host plugin set no longer leaks in, so the container enables it itself; Claude Code installs the enabled plugin from the official marketplace on startup). Host permissions, hooks, plugins, and model/effort pins do not leak in; the repo's `/workspace/.claude/settings.json` layers on top, and per-launch flags (`--fast`, `--effort`) own session config.
- `~/.claude/cw-sessions/<name>/` (host) → `/home/cw/.claude` (container). Per-workspace overlay of everything else.
- `~/.claude/projects/-workspace/` (inside `cw-sessions/<name>/`) — where Claude Code stores the session JSONL for `/workspace`. The encoded path `-workspace` is fixed (just `/` replaced with `-`).

This means each container session has its own private `~/.claude.json` (so MCP server allow-lists are per-workspace), its own credentials (so a refresh in one container doesn't blow away another), and its own session log directory.

#### Scoped credential hydration

`cw.run_in_container` hydrates only the secrets the session's bro declares into the container's `~/.ppp`, scoping each container to a minimal credential set.

- **The manifest.** A bro's `needed_secrets()` (`bro/bro.py`) is the union of each declared MCP server's and data source's `needed_secrets` (read per-instance) and the bro's MRO-collected `extra_secrets`. It deliberately omits the LLM key — that is added only by surfaces that run the bro as an LLM process. `bro show <name>` lists it. Components declare what they read: `flow.MCPServer` → `notion` (+ `focus` when it holds the focus tools, derived from the held tools), `infra.MCPServer` → `infra, focus, aws`, `TMDb` → `tmdb`, `WebSearch` → `brave`, `chat_gpt.LLMSpec.needed_secrets()` → `openai`. `extra_secrets` is the escape hatch for environment needs no component expresses: ppp-dev → `github`, devoops → `aws`.
- **Which bro.** Scope keys on the manifest, not the launching CLI — `do` (ask / do-task) and `cw ss` both containerise through `cw.run_in_container`. `cw ss --bro <name>` and `ask <name>` use that bro; a no-`--bro` `cw ss` (including dive-in, which sets `CW_BRO=ppp-dev`) themes as ppp-dev.
- **Per-surface sets (strict → request only what's used).** The three container surfaces use the bro differently, so each requests a precise set:
  - native claude code session (dive-in / plain `cw ss`): baseline `{session_log, trails}` + the bro's `extra_secrets` + `flow_mcp` (when `--mcp http`). It drives the bro's *skills* (bash → `extra_secrets`) and its flow via `--mcp`, not the bro's in-process MCP/data-source toolset — so a dive-in needs neither `notion` nor `openai`.
  - `--bro` (`claude --bare` serving the bro's own in-process MCP servers): the bro's full `needed_secrets()` + baseline + `anthropic` (apiKeyHelper).
  - `ask` / `do-task` (bro runs as a chat_gpt process): `needed_secrets()` + `llm_spec.needed_secrets()` + `trails` (recording is mandatory).
  - `--aws` adds `aws` to any session's set.
- **Hydration.** `credentials.write_scoped_store(dest, names)` writes one file per resolved secret (its raw text) plus a scoped `credentials.json` (carrying each secret's `install` hook). Strict: a name not in the host registry raises (a manifest typo), and a declared name with no value also raises (`SecretNotFound`) — both fail loudly on the host, before the container exists.
- **`aws` is an ordinary secret.** `aws` → `aws_credentials`, the host's AWS shared-credentials INI at `~/.ppp/aws_credentials`. Its install hook sets `AWS_SHARED_CREDENTIALS_FILE`; no `~/.aws` mount, no `AWS_*` forwarding.
- **Install hooks.** A secret can declare an `install` shell template (`{path}` → resolved file path) in the registry; `credentials install-hooks` emits the hooks for the present secrets and the entrypoint `eval`s them, so wiring a secret into its consumer (git, the aws CLI) is declarative, not entrypoint-special-cased.
- **The container side.** `base.credentials._load_registry()` searches both `<project>/.configs` and `~/.ppp` for `credentials.json`, so the scoped registry mounted at `~/.ppp` takes effect; the built-in registry (and the deployed services that synthesize `<project>/.configs`) are unchanged.

#### "Already in a container" fallback

If `cw ss -c` is invoked from inside an already-containerised session (`CW_IN_CONTAINER=1` is set by the Dockerfile), `cw.py:cw` falls back to host mode rather than trying to nest containers.

## Flags that shape the session

These flags apply to `cw ss` and (with the exception of `-c` / `--drop` / `--mcp` / `--bro` / `-p`) are also exported via `cw.add_forwarded_flags` so wrappers like `dive-in` and `start-session` can pass them straight through without per-flag plumbing.

- **`-c`, `--container`** — container mode (see above). Defaults off; host mode is the default.
- **`--drop`** — remove the workspace on exit without prompting.

  In host mode this means `git worktree remove --force` and deleting the `worktree-<name>` branch; in container mode it means `rm -rf var/cw/containers/<name>` and `~/.claude/cw-sessions/<name>`.

  In host mode it also sets `CW_DROP=1` so `.claude/hooks/check-worktree-landed.sh` skips the warn-and-exit-2 dance. In container mode no `CW_DROP` is set and the var is not in `_DOCKER_FORWARD_ENV` — the hook's path guard already short-circuits inside the container (cwd is `/workspace`, not under `.claude/worktrees/`), so the dance is skipped anyway.
- **`--mcp [http|local]`** — wire up the flow MCP server.

  `http` (default when the flag is bare) connects to the deployed server at the `flow_mcp` secret's `url` with a bearer token; `local` spawns a stdio process from `flow/mcp/mcp_local.json`.

  Without `--mcp`, no flow MCP is connected — Claude doesn't see task/project tools.
- **`--bro <name>`** — launch a clean session under a chosen bro persona (system prompt, MCP servers, tools) using `claude --bare`, `--strict-mcp-config`, and only the bro's MCP tools. Wires the bro's MCP servers and data sources through `mcp-server bro:<name>`. The bro's skills (`bro/bros/<bro>/skills/*.md`, MRO-merged) are symlinked into `<workspace>/.claude/skills/<name>/SKILL.md` by the container entrypoint (`cw populate-bro-skills $CW_BRO`, triggered by the forwarded `CW_BRO` env var) so claude's `--bare` slash-command discovery picks them up — type `/<name>` in chat to invoke.

  **Requires `-c`** (the bro flow uses an Anthropic Console API key, not the user OAuth, and is fenced to the container). **Requires the `anthropic` secret**. Mutually exclusive with `--mcp`, `--auto`, and `--resume`.

  `cw --bro` reads its api key from that secret via `setup/print_anthropic_key.sh` (wired as `apiKeyHelper`); using `ANTHROPIC_API_KEY` instead would trigger Claude's "Detected a custom API key" prompt every session.
- **`--auto`** — autonomous mode: passes `--dangerously-skip-permissions` to claude and switches the git identity to bro (`Bro <dzhioev+bro@gmail.com>`). Implies `--rc`.

  **Requires `-c`** (a sandbox is mandatory for skip-permissions). Adds a `Land mode: PR` line to the system prompt. Cannot be combined with `--bro`.
- **`--fast`** — enables fast mode for the session (injected via `--settings '{"fastMode": true}'`). Off by default regardless of host settings, so individual `cw ss` invocations are predictable.
- **`--aws`** — give the session AWS access by adding the `aws` secret to its scoped credential store; its install hook points the AWS CLI/SDK at the hydrated `~/.ppp/aws_credentials` via `AWS_SHARED_CREDENTIALS_FILE`. Strict hydration fails the launch if the secret is absent on the host. Ignored in host mode.
- **`--effort {low|medium|high|xhigh|max}`** — forwarded as `claude --effort` (thinking effort).
- **`--rc`** — enables claude remote control (`--remote-control`). Off by default because it breaks Ctrl+V image paste; implied by `--auto`.
- **`--resume`** — resume the latest Claude session in this workspace.

  Looks up the newest `.jsonl` in the right projects directory (`~/.claude/projects/<encoded>/` for host, `~/.claude/cw-sessions/<name>/projects/-workspace/` for container), extracts the session id from the filename stem, and adds `--resume <id>` to the claude argv. Skips the initial prompt. Cannot be combined with `--drop` or `-p/--prompt`.

  After a container session exits, `cw` overwrites Claude's printed resume hint (which suggests `claude --resume <id>` — only valid inside the container) with the host-side one: `cw ss -c --mcp --resume <name>`.
- **`-p / --prompt <text>`** — the initial prompt for the session.

  `cw ss` prepends the auto-injected base prompts (via `cw.py:_load_base_prompts`) using `--append-system-prompt`, and forwards the text via `--`.

Trailing positional args after `<name>` are forwarded to `claude` verbatim (`argparse.REMAINDER`).

## Auto-injected system prompt

For every non-bro `cw ss` session (regardless of mode), `cw.py:_load_base_prompts` builds a base prompt from `prompts/shared/*` and the top-level reference docs the loader registers (see `prompts/CLAUDE.md` for the inventory). The result is appended to claude's system prompt via `--append-system-prompt`. `shared/` is also injected into every bro; the top-level reference docs are Claude-Code-specific and are **not** injected when `--bro` is used (the bro flow runs `--bare` with its own `--system-prompt`).

## Forwarded env vars

Wrappers and hooks rely on a small set of env vars:

- `CW_NAME` — workspace name. Set by `start_session` in both modes (host and container), and additionally passed into the container via `-e CW_NAME=<name>` for the entrypoint. The entrypoint uses it to pick the branch name (`worktree-$CW_NAME`); `cw banner` reads it to render the session header.
- `CW_HOST_WORKSPACE` — host-side absolute path to the container workspace dir (`<project>/var/cw/containers/<name>`). Set by `cw` in container mode only (`-e CW_HOST_WORKSPACE=<path>`) so `cw banner`, running inside the container, can tell the user where their `/workspace` mount actually lives on the host.
- `CW_COMMAND` — the user-visible `cw ss …` invocation, reconstructed by `start_session` for telemetry and resume hints. Defaulted into `PPP_SHELL_COMMAND` if that's not already set.
- `CW_BRO` — names the bro whose skills should be surfaced to Claude Code's slash-command discovery. Set by `start_session` when `--bro <name>` is passed, and unconditionally by `dive-in` (`ppp-dev`). Container mode: forwarded into the container; the entrypoint runs `cw populate-bro-skills "$CW_BRO"` after venv activation to symlink the skills into the workspace's `.claude/skills/`. Host mode: `start_session` populates a per-session `tempfile.mkdtemp` directory and passes it to claude via `--add-dir <tmp>`, so concurrent host sessions on the same repo don't share the project's `.claude/skills/`. Also drives `cw banner`'s ASCII Bro logo + bro-name header.
- `CW_TASK_ID` — set by `dive-in` when it has resolved a task; consumed by `setup/claude_commit_footer.py` to add a `Task: <url>` line to commit messages.
- `CW_IN_CONTAINER=1` — set by the Dockerfile. Detected by `cw.py:cw` to fall back to host mode when nesting would be requested, and by `.claude/hooks/session_start.sh` to skip the host-only worktree provisioning.
- `CW_DROP=1` — set by `cw` (host mode only) when `--drop` was passed; used by `.claude/hooks/check-worktree-landed.sh` to skip the keep-or-drop prompt. Container mode doesn't set or forward it (the hook short-circuits on its path guard in the container anyway).
- Plus the standard `GITHUB_TOKEN` and `GIT_AUTHOR_*` / `GIT_COMMITTER_*` — explicitly forwarded into the container via `_DOCKER_FORWARD_ENV`. (AWS reaches the container as the scoped `aws` secret, not as forwarded env.)

## Hooks

`cw` itself doesn't run hooks — Claude Code does, based on `.claude/settings.json`. The ones that interact with `cw`'s lifecycle:

- `WorktreeCreate` → `.claude/hooks/worktree_create.sh` — creates the worktree with the project's conventions (branch naming, `CLAUDE_BASE`, submodule alternate location).
- `SessionStart` → `.claude/hooks/session_start.sh` — first-time provisioning per host worktree (`.venv` via `uv sync`). Bails fast on subsequent sessions and inside containers (where the entrypoint owns first-run setup).
- `SessionEnd` → `.claude/hooks/check-worktree-landed.sh` — wraps `cw check-clean` and always exits 2 so the keep-or-drop prompt appears every time. Silent if `CW_DROP=1`.
- `SessionStart` / `SessionEnd` → `.claude/hooks/sync-session-log-start.sh` / `sync-session-log-stop.sh` — bracket the session with calls to `sync-session-log` (S3 + DynamoDB transcript upload). The hooks discard `sync-session-log`'s stderr, so its only durable failure signal is the health file it writes (`session_log_health.py`), which the statusLine and `cw banner` surface.
