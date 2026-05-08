#!/usr/bin/env -S bash -e

DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
cd "$DIR/.."

unset VIRTUAL_ENV

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found; run setup/setup_env.sh first or install manually" >&2
  exit 1
fi

echo "initializing submodules"
git submodule update --init --recursive

echo "syncing python dependencies"
uv sync --all-groups

echo "syncing console scripts"
uv run sync-scripts

echo "re-syncing after script registration"
uv sync --all-groups

if [ -L .configs ] && [ -d .configs ]; then
  echo ".configs symlink OK"
elif [ -L .configs ]; then
  echo "warning: .configs symlink is broken (target missing); check setup/dotfiles submodule" >&2
else
  echo "warning: .configs symlink not found; credentials will not be available" >&2
fi

echo "repo setup complete"
