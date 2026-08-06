#!/usr/bin/env -S bash -e
DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
cd "$DIR"

if [ "${CW_VENV_BAKED:-}" != "1" ]; then
  unset VIRTUAL_ENV
  uv sync --all-packages --all-groups --all-extras
fi

source "$DIR/.venv/bin/activate"
bro-dev.install
