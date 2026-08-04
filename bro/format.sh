#!/usr/bin/env -S bash -e
source "$(bro-shell-dir)/prelude.sh"
DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"

cd "$DIR"
ruff format
ruff check --fix
