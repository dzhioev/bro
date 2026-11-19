#!/usr/bin/env -S bash -e
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/bro/setup/prelude.sh"
cd "$HERE"

unset VIRTUAL_ENV
uv sync --all-packages --all-groups --all-extras

printf 'bro\n' > .venv/bro-installation-home

# shellcheck source=/dev/null
source "$HERE/.venv/bin/activate"
python -m ride.provisioning
bro.dev.install
