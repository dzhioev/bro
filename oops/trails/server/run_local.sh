#!/usr/bin/env -S bash -e
source "$(bro-shell-dir)/prelude.sh"

DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
token="$(credentials get trails --field token)"
runtime_directory="$(mktemp -d)"
trap 'rm -rf "$runtime_directory"' EXIT

sed 's/__BRO_TRAILS_REGION__/us-east-1/g' \
  "$DIR/runtime_credentials.json" >"$runtime_directory/credentials.json"
printf '%s\n' '{"backend": "local"}' >"$runtime_directory/trails_store.json"
jq -n --arg token "$token" \
  '{tokens: {local: {token: $token, permissions: ["read", "write", "admin"]}}}' \
  >"$runtime_directory/trails_tokens.json"
export CREDENTIALS_REGISTRY="$runtime_directory/credentials.json"

trails-server --allow-env "$@"
