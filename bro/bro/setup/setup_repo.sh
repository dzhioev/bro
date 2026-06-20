#!/usr/bin/env -S bash -e

DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
cd "$DIR/.."

unset VIRTUAL_ENV

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found; run setup/setup_env.sh first or install manually" >&2
  exit 1
fi

echo "syncing python dependencies"
uv sync --all-groups

# pyproject's [project.scripts] is committed; provisioning only materializes the
# gitignored _entrypoints.py shim the launchers import (see sync_scripts.py).
echo "generating console-script entrypoints"
uv run python -m sync_scripts --entrypoints

echo "registering local git aliases"
git config --local alias.golc '!./setup/git_golc.py'

echo "installing git hooks"
hooks_dir="$(git rev-parse --git-dir)/hooks"
mkdir -p "$hooks_dir"
cp setup/git_hooks/post-commit "$hooks_dir/post-commit"
chmod +x "$hooks_dir/post-commit"

if [ -d "$HOME/.ppp" ]; then
  echo "secret store ~/.ppp OK"
else
  echo "warning: ~/.ppp not found; credentials will not be available (stow dot-ppp)" >&2
fi

echo "repo setup complete"
