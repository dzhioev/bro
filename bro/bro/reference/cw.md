# cw

`cw` launches Claude Code in an isolated per-task workspace. It owns the workspace lifecycle (create / provision / list / clean), wires up MCP and bro personas, and chooses between two execution modes — a host-side git worktree or an isolated Docker container — without changing what the user sees on the inside.

This document explains *how it actually works*: where things live on disk, what each flag does, how credentials and settings move between host and container, and which env vars are produced and consumed. For the source of truth on flags, run `cw --help` / `cw ss --help`; the implementation lives in `cw.py`.

## Subcommands

`cw` is a subcommand dispatcher:

- **`cw ss <name>`** — start a session in the workspace `<name>`. Creates the workspace on the fly if it doesn't exist. This is the workhorse; everything below describes its behaviour.
- **`cw list`** — list every workspace under this project. Each entry shows a state badge (`[.]` live host session, `[o]` live container session, `[x]` abandoned), the workspace name (container names are prefixed `c:`), an age (last filesystem touch), and the first user prompt of the latest session.
- **`cw clean [--force] [--dry-run] [<ref> ...]`** — remove workspaces with no uncommitted or unpushed work. Without args, scans both namespaces. With explicit `<ref>`s, restricts to those (`name` for host, `c:name` for container). `--force` removes despite dirty state; `--dry-run` only prints. Safety is shared with `check-clean`.
- **`cw check-clean [<ref>]`** — probe a single workspace (or the cwd if omitted). Exit 0 if it's safe to drop; exit 1 with reasons on stderr otherwise. The hook `.claude/hooks/check-worktree-landed.sh` calls this to populate the keep-or-drop prompt at session end.
- **`cw exec <name> [<cmd>…]`** — exec into the container backing workspace `<name>`. With no command, opens an interactive bash with `/workspace/.venv` sourced; otherwise runs `<cmd>` in the same activated env. Container workspaces only; the `c:` prefix is accepted but optional.
- **`cw banner [--llm]`** — print the banner: workspace name (with the `c:` prefix from `cw list` for container workspaces), the canonical `cw ss …` invocation (`CW_COMMAND`), `/workspace` plus its host bind-mount path on separate lines, the `cw exec <name>` hint (labelled `docker shell:` because it drops into a shell *inside* the container), the outer launching command (`PPP_SHELL_COMMAND`), and the user-typed prompt extracted out of it (split at the last `--new`/`-p`/`--prompt`/`--` marker). For `--bro` sessions, prepends the ASCII Bro logo with a dim `// <bro>` signature on its bottom line. The container image's `~/.bashrc` runs `cw banner` once per interactive shell so users see it on `cw exec` entry. The visual mode renders the `@prompt@` placeholder and the `prompt:` label in bright-white bold for emphasis. The `--llm` flag emits the same facts as plain `key: value` lines (no ANSI, no logo) for Claude itself to read at session start via the Bash tool (see `prompts/environment.md`); the prompt body is deliberately omitted there since the LLM already has it as its first message — `launch_command` retains its trailing marker (`dive-in --new `) as the signal that a seed prompt exists.

A workspace is "clean" when:

- (a) `git status --porcelain` is empty,
- (b) `HEAD` is an ancestor of `origin/master`, and
- (c) every submodule's pinned commit is reachable from its remote default ref.

For container workspaces the ancestry checks run against the host project's `.git`, with the container's `HEAD` fetched in first, because the container clone's `origin` is an HTTPS URL that the host can't reach without credentials.

## Host mode vs container mode

`cw ss` has two execution modes. Host is the default; `-c` / `--container` selects the container.

### Host mode (`cw ss <name>`)

Runs `claude -w <name>` from the project root, with the env extended to activate the worktree's `.venv`. Claude Code itself owns the worktree lifecycle: it triggers the `.claude/hooks/worktree_create.sh` hook on first use (which is what actually does `git worktree add` plus the project-specific tweaks — `worktree-<name>` branch, `CLAUDE_BASE` marker file, `submodule.alternateLocation=superproject`), and on exit it shows the keep-or-drop prompt. `cw` itself does not directly create or remove worktrees in host mode unless `--drop` is passed (in which case it `git worktree remove --force` and `git branch -D worktree-<name>` after `claude` exits).

Layout on disk:

- `<project>/.claude/worktrees/<name>/` — the worktree (regular working tree with a `.git` gitfile that points at `<project>/.git/worktrees/<name>/`).
- `<project>/.git/worktrees/<name>/CLAUDE_BASE` — marker recording the `HEAD` at worktree creation (set by `worktree_create.sh`).
- `<project>/.claude/worktrees/<name>/.venv` — per-worktree virtualenv created on first run by `.claude/hooks/session_start.sh` (which also reflinks submodule git-dirs from the main repo to avoid re-fetching ~200M of objects).
- `~/.claude/projects/<encoded-worktree-path>/` — Claude Code's own per-project state, including the session JSONL files. The encoded path is the worktree path with `/` and `.` replaced by `-`.

### Container mode (`cw ss -c <name>`)

`/workspace` inside the container is a **fresh clone**, not a worktree. The gitfile-based worktree layout doesn't survive the container boundary (the gitfile points at a host absolute path), and a clone keeps the container's git state genuinely isolated. Layout:

- `<project>/var/cw/containers/<name>/` (host) → `/workspace` rw. Empty on first run; the entrypoint clones into it.
- `<project>` (host) → `/host-repo` ro. The clone uses `--shared`, so the container reuses the host's `.git/objects` via alternates instead of duplicating them, and submodules can clone from local paths (avoiding the need for SSH keys in the container).
- `<project>/.configs/cw_github_token` (or `cw_github_token_bro` when `--auto`) → `/run/secrets/github_token` ro. The entrypoint wires this into `git credential.helper` and exports it as `GH_TOKEN` so `git push` over HTTPS and the `gh` CLI both work.
- `/var/run/docker.sock` (host) → same path in the container. Lets deploy scripts run `docker build`/`docker push` against the host daemon — no nested runtime — at the cost of giving in-container processes API-level control over host docker. The entrypoint reconciles the in-container `docker` group's GID with the bind-mounted socket's GID so `cw` can use it without sudo.

Inside the container, the entrypoint (running as root first):

1. Aligns the `cw` user's UID/GID with whoever owns `/workspace` on the host, then re-execs as `cw` (skipped on Docker for Mac when the bind mount reports root-owned via virtiofs — remapping to UID 0 would make claude refuse `--dangerously-skip-permissions`).
2. Seeds `~/.claude/` from `/host-claude` once (skipping `sessions`/`projects`/`history.jsonl`/`cw-sessions` so transcripts from prior sessions don't leak across containers).
3. Copies the host's `~/.gitconfig` into the writable container `$HOME` and marks `/workspace` as a safe git directory.
4. On first run, clones `/host-repo` into `/workspace` with `--shared`, retargets `origin` to the host's upstream (converting `git@github.com:` to `https://github.com/` so token auth works), and adds `host` as a local remote (pointing at `/host-repo`) for fetching commits that haven't been pushed yet. Branch is `worktree-<CW_NAME>`.
5. Initialises submodules from the matching host-local paths in `/host-repo` (since `.gitmodules` uses SSH URLs the container can't auth to), skipping any submodule the host hasn't initialised.
6. Installs a `pre-push` hook that blocks non-fast-forward pushes, and blocks direct pushes to `master`/`main` when running as the bro identity (`GIT_AUTHOR_EMAIL=dzhioev+bro@gmail.com`).
7. On first run, provisions a Linux `.venv` with `uv sync --all-groups` (the wheel cache is pre-warmed in the image; see `setup/container/Dockerfile`).
8. Activates the venv so child processes (hooks, MCP servers, Bash tool) inherit it.

The container image is built lazily — `cw.py:_image_tag()` hashes `setup/container/` plus `pyproject.toml` and `uv.lock`, and `_ensure_image` rebuilds when the tag is missing. Tag format: `ppp-cw:<12-char-sha>`.

Network is not restricted by design.

When `cw ss -c` exits, the workspace directory and the per-session host-side state stay on disk for the next session, unless `--drop` was passed (in which case both `var/cw/containers/<name>` and `~/.claude/cw-sessions/<name>` are removed).

#### Container credential isolation

The container does **not** bind-mount `~/.claude.json` or `~/.claude/.credentials.json` directly from the host. Instead, `cw.py:_docker_run_argv` seeds a container-private copy under `~/.claude/cw-sessions/<name>/` and bind-mounts that:

- `~/.claude/cw-sessions/<name>/.claude.json` — seeded once per workspace from the host's `~/.claude.json` (or `{}` if missing). Subsequent sessions keep whatever the container last wrote. Stops per-project mutations (mcpServers, allowedTools, hasTrustDialogAccepted) from being usable to escalate into the next host session.
- `~/.claude/cw-sessions/<name>/.credentials.json` — synced from host pre-launch and back to host post-exit, keyed on `claudeAiOauth.expiresAt`. The fresher copy wins. On macOS, the keychain is also consulted: if the keychain's expiry is newer than the host file's, the host file is rewritten from the keychain first. This preserves OAuth refresh without leaving the runtime token swap exposed.
- `~/.claude/settings.json` (host) → same path in the container, read-only. The host file is the source of truth for settings; container can't mutate it.
- `~/.claude/cw-sessions/<name>/` (host) → `/home/cw/.claude` (container). Per-workspace overlay of everything else.
- `~/.claude/projects/-workspace/` (inside `cw-sessions/<name>/`) — where Claude Code stores the session JSONL for `/workspace`. The encoded path `-workspace` is fixed (just `/` replaced with `-`).

This means each container session has its own private `~/.claude.json` (so MCP server allow-lists are per-workspace), its own credentials (so a refresh in one container doesn't blow away another), and its own session log directory.

#### "Already in a container" fallback

If `cw ss -c` is invoked from inside an already-containerised session (`CW_IN_CONTAINER=1` is set by the Dockerfile), `cw.py:cw` falls back to host mode rather than trying to nest containers.

## Flags that shape the session

These flags apply to `cw ss` and (with the exception of `-c` / `--drop` / `--mcp` / `--bro` / `-p`) are also exported via `cw.add_forwarded_flags` so wrappers like `dive-in` and `start-session` can pass them straight through without per-flag plumbing.

- **`-c`, `--container`** — container mode (see above). Defaults off; host mode is the default.
- **`--drop`** — remove the workspace on exit without prompting.

  In host mode this means `git worktree remove --force` and deleting the `worktree-<name>` branch; in container mode it means `rm -rf var/cw/containers/<name>` and `~/.claude/cw-sessions/<name>`.

  In host mode it also sets `CW_DROP=1` so `.claude/hooks/check-worktree-landed.sh` skips the warn-and-exit-2 dance. In container mode no `CW_DROP` is set and the var is not in `_DOCKER_FORWARD_ENV` — the hook's path guard already short-circuits inside the container (cwd is `/workspace`, not under `.claude/worktrees/`), so the dance is skipped anyway.
- **`--mcp [http|local]`** — wire up the flow MCP server.

  `http` (default when the flag is bare) connects to the deployed server at `.configs/flow_mcp.json`'s `url` with a bearer token; `local` spawns a stdio process from `flow/mcp/mcp_local.json`.

  Without `--mcp`, no flow MCP is connected — Claude doesn't see task/project tools.
- **`--bro <name>`** — launch a clean session under a chosen bro persona (system prompt, MCP servers, tools) using `claude --bare`, `--strict-mcp-config`, and only the bro's MCP tools. Wires the bro's MCP servers and data sources through `mcp-server bro:<name>`. The bro's skills (`bro/bros/<bro>/skills/*.md`, MRO-merged) are symlinked into `<workspace>/.claude/skills/<name>/SKILL.md` by the container entrypoint (`cw populate-bro-skills $CW_BRO`, triggered by the forwarded `CW_BRO` env var) so claude's `--bare` slash-command discovery picks them up — type `/<name>` in chat to invoke.

  **Requires `-c`** (the bro flow uses an Anthropic Console API key, not the user OAuth, and is fenced to the container). **Requires `.configs/anthropic.json`**. Mutually exclusive with `--mcp`, `--auto`, and `--resume`.

  `cw --bro` reads its api key from that file via `setup/print_anthropic_key.sh` (wired as `apiKeyHelper`); using `ANTHROPIC_API_KEY` instead would trigger Claude's "Detected a custom API key" prompt every session.
- **`--auto`** — autonomous mode: passes `--dangerously-skip-permissions` to claude, switches the git identity to bro (`Bro <dzhioev+bro@gmail.com>`), and uses `.configs/cw_github_token_bro` instead of the user token. Implies `--rc`.

  **Requires `-c`** (a sandbox is mandatory for skip-permissions). Adds a `Land mode: PR` line to the system prompt. Cannot be combined with `--bro`.
- **`--fast`** — enables fast mode for the session (injected via `--settings '{"fastMode": true}'`). Off by default regardless of host settings, so individual `cw ss` invocations are predictable.
- **`--aws`** — expose host AWS credentials to the container: bind-mounts `~/.aws` read-only and forwards the `AWS_*` env vars. Pre-flight check rejects the flag if neither source is present. Ignored in host mode.
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
- `CW_TOKEN_FILE` — selects which `.configs/cw_github_token*` to bind-mount as `/run/secrets/github_token`. Defaults to `cw_github_token`; `--auto` switches it to `cw_github_token_bro`.
- `CW_IN_CONTAINER=1` — set by the Dockerfile. Detected by `cw.py:cw` to fall back to host mode when nesting would be requested, and by `.claude/hooks/session_start.sh` to skip the host-only worktree provisioning.
- `CW_DROP=1` — set by `cw` (host mode only) when `--drop` was passed; used by `.claude/hooks/check-worktree-landed.sh` to skip the keep-or-drop prompt. Container mode doesn't set or forward it (the hook short-circuits on its path guard in the container anyway).
- Plus the standard `GITHUB_TOKEN`, `GIT_AUTHOR_*` / `GIT_COMMITTER_*`, and (with `--aws`) `AWS_*` — all explicitly forwarded into the container via `_DOCKER_FORWARD_ENV` / `_DOCKER_AWS_ENV`.

## Hooks

`cw` itself doesn't run hooks — Claude Code does, based on `.claude/settings.json`. The ones that interact with `cw`'s lifecycle:

- `WorktreeCreate` → `.claude/hooks/worktree_create.sh` — creates the worktree with the project's conventions (branch naming, `CLAUDE_BASE`, submodule alternate location).
- `SessionStart` → `.claude/hooks/session_start.sh` — first-time provisioning per host worktree (submodule git-dir reflinks, `.venv` via `uv sync`). Bails fast on subsequent sessions and inside containers (where the entrypoint owns first-run setup).
- `SessionEnd` → `.claude/hooks/check-worktree-landed.sh` — wraps `cw check-clean` and always exits 2 so the keep-or-drop prompt appears every time. Silent if `CW_DROP=1`.
- `SessionStart` / `SessionEnd` → `.claude/hooks/sync-session-log-start.sh` / `sync-session-log-stop.sh` — bracket the session with calls to `sync-session-log` (S3 + DynamoDB transcript upload).
