#!/usr/bin/env -S bash -e
source "$(bro-shell-dir)/prelude.sh"

token="$(credentials get trails --field token)"
runtime_directory="$(mktemp -d)"
trap 'rm -rf "$runtime_directory"' EXIT

mkdir "$runtime_directory/creds"
printf '%s\n' '{"backend": "local"}' >"$runtime_directory/creds/trails.cred"
jq -n --arg token "$token" \
  '{tokens: {local: {token: $token, permissions: ["read", "write", "admin"]}}}' \
  >"$runtime_directory/creds/trails_tokens.cred"
export BRO_STORE="$runtime_directory"

trails-server --allow-env "$@"
