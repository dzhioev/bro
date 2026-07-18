#!/usr/bin/env -S bash -e
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/prelude.sh"

# prints the `anthropic` secret's `api_key` to stdout. used as claude code's
# `apiKeyHelper` for `cw --bro` sessions so claude reads the api key from the
# credential resolver instead of from ANTHROPIC_API_KEY (which triggers a
# one-time "Detected a custom API key" confirmation per ~/.claude.json — and
# that file is per-workspace in cw containers, so the prompt would fire every
# session). runs inside the container, where the venv (hence `credentials`) is
# on PATH.

exec credentials get anthropic --field api_key
