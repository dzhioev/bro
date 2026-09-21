#!/usr/bin/env -S bash -e
source "$(bro-shell-dir)/prelude.sh"

cd "$HERE"
ruff format
ruff check --fix
