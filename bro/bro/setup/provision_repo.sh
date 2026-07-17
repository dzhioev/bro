#!/usr/bin/env -S bash -e

# provision a checked-out PPP repo for use. idempotent and safe to re-run every
# session — the three surfaces that need a provisioned repo all call it:
#   - setup_repo.sh        host main repo
#   - cw (host mode)       host worktrees, every launch
#   - container entrypoint the cloned /workspace
# tree creation (clone / worktree / pre-existing) and surface-specific wiring
# (credentials) stay with the callers; only the steps below are shared.

DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
source "$DIR/log.sh"
cd "$DIR/.."

unset VIRTUAL_ENV

if ! command -v uv >/dev/null 2>&1; then
  log ERROR "uv not found; run setup/setup_env.sh first or install manually"
  exit 1
fi

# uv sync is the slow step; skip it when nothing it reads has changed since the
# last successful sync. the stamp lives in the gitignored .venv, so a fresh venv
# (no stamp) always syncs. regen below is cheap and unconditional — source files
# can add or remove CLIs without touching uv.lock / pyproject.
stamp=".venv/.provision-stamp"
if [ ! -f "$stamp" ] || [ uv.lock -nt "$stamp" ] || [ pyproject.toml -nt "$stamp" ]; then
  log INFO "syncing python dependencies"
  if ppp_verbose; then
    uv sync --all-groups >&2
  else
    uv sync -q --all-groups >&2
  fi
  touch "$stamp"
fi

# regenerate the gitignored _entrypoints.py console-script bridge in .venv. the
# committed [project.scripts] table points at it; it must track the source on
# every provision. run with the venv's python directly (cwd-independent, and
# avoids `uv run` re-syncing the env). see sync_scripts.py.
# CW_VENV_BAKED: the container entrypoint sets this when it reuses the venv baked
# into the image, whose bridge was generated from the tag-pinned [project.scripts]
# and so already matches this clone — skip the regen.
if [ "${CW_VENV_BAKED:-}" = "1" ]; then
  log VERBOSE "console-script entrypoints baked into image; skipping regen"
else
  log VERBOSE "generating console-script entrypoints"
  .venv/bin/python -m sync_scripts --entrypoints >&2
fi

# promote the staged token-accounting baseline after each commit lands. --git-path
# hooks (not --git-dir/hooks) resolves to the shared common hooks dir from inside a
# worktree, which is where git actually runs hooks from.
log VERBOSE "installing git hooks"
hooks_dir="$(git rev-parse --git-path hooks)"
mkdir -p "$hooks_dir"
cp setup/git_hooks/post-commit "$hooks_dir/post-commit"
chmod +x "$hooks_dir/post-commit"

log VERBOSE "registering local git aliases"
git config --local alias.golc '!./setup/git_golc.py'
