#!/usr/bin/env -S bash -e

# prints .configs/anthropic.json's `api_key` to stdout. used as claude code's
# `apiKeyHelper` for `cw --bro` sessions so claude reads the api key from the
# configs file instead of from ANTHROPIC_API_KEY (which triggers a one-time
# "Detected a custom API key" confirmation per ~/.claude.json — and that file
# is per-workspace in cw containers, so the prompt would fire every session).

DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
exec python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['api_key'])" \
  "$DIR/../.configs/anthropic.json"
