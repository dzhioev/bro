#!/usr/bin/env -S bash -e

# provision a checked-out repo for use — the body behind <repo>/setup.sh, the
# uniform provisioning entry point of every repo cw operates on: idempotent,
# safe to run on every launch; postcondition: <repo>/.venv/bin/cw exists.
# two modes, selected by where this ppp tree sits:
#   - ppp is the repo: provision ppp's own root (ppp's setup.sh calls this)
#   - ppp is a submodule: provision the superproject's root — its manifests
#     drive the sync, ppp's tree supplies the git hook and dev tooling. the
#     superproject's entire provisioning boilerplate is a root setup.sh of:
#         #!/usr/bin/env -S bash -e
#         DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
#         git -C "$DIR" submodule update --init ppp
#         "$DIR/ppp/setup/setup_env.sh"
#         exec "$DIR/ppp/setup/provision_repo.sh"
# tree creation (clone / worktree / pre-existing) and surface-specific wiring
# (credentials) stay with the callers; only the steps below are shared.

DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
source "$DIR/prelude.sh"

ppp_root="$(readlink -f "$DIR/..")"
superproject="$(git -C "$ppp_root" rev-parse --show-superproject-working-tree)"
if [ -n "$superproject" ]; then
  cd "$(readlink -f "$superproject")"
else
  cd "$ppp_root"
fi

unset VIRTUAL_ENV

if ! command -v uv >/dev/null 2>&1; then
  log ERROR "uv not found; run setup/setup_env.sh first or install manually"
  exit 1
fi

# uv sync is the slow step; skip it when nothing it reads has changed since the
# last successful sync. the stamp lives in the gitignored .venv, so a fresh venv
# (no stamp) always syncs. a superproject's sync also reads the vendored ppp
# manifest (its path dependency), so that joins the staleness check — in self
# mode it repeats pyproject.toml. regen below is cheap and unconditional —
# source files can add or remove CLIs without touching uv.lock / pyproject.
stamp=".venv/.provision-stamp"
if [ ! -f "$stamp" ] || [ uv.lock -nt "$stamp" ] || [ pyproject.toml -nt "$stamp" ] \
    || [ "$ppp_root/pyproject.toml" -nt "$stamp" ]; then
  log INFO "syncing python dependencies"
  if log_enabled VERBOSE; then
    uv sync --all-groups >&2
  else
    uv sync -q --all-groups >&2
  fi
  touch "$stamp"
fi

# regenerate the gitignored _entrypoints.py console-script bridge in .venv. the
# committed [project.scripts] table points at it; it must track the source on
# every provision. run with the venv's python directly (cwd-independent — the
# venv's editable install resolves the dev package in either mode — and avoids
# `uv run` re-syncing the env). see dev/sync_scripts.py.
# CW_VENV_BAKED: the container entrypoint sets this when it reuses the venv baked
# into the image, whose bridge was generated from the tag-pinned [project.scripts]
# and so already matches this clone — skip the regen.
if [ "${CW_VENV_BAKED:-}" = "1" ]; then
  log VERBOSE "console-script entrypoints baked into image; skipping regen"
else
  log VERBOSE "generating console-script entrypoints"
  .venv/bin/python -m dev.sync_scripts --entrypoints >&2
fi

# promote the staged token-accounting baseline after each commit lands. --git-path
# hooks (not --git-dir/hooks) resolves to the shared common hooks dir from inside a
# worktree, which is where git actually runs hooks from.
log VERBOSE "installing git hooks"
hooks_dir="$(git rev-parse --git-path hooks)"
mkdir -p "$hooks_dir"
cp "$DIR/git_hooks/post-commit" "$hooks_dir/post-commit"
chmod +x "$hooks_dir/post-commit"

log VERBOSE "registering local git aliases"
if [ -n "$superproject" ]; then
  git config --local alias.golc "!./${ppp_root#"$PWD/"}/dev/git_golc.py"
else
  git config --local alias.golc '!./dev/git_golc.py'
fi
