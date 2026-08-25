#!/usr/bin/env -S bash -e
source "$(bro-shell-dir)/prelude.sh"

DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
REPO_ROOT="$DIR/../../.."
cd "$REPO_ROOT"

source "$(bro-oops-dir)/deploy_lib.sh"
source "$(bro-shell-dir)/docker_smoke_test.sh" docker prepare_image_build

smoke_directory=build/trails-smoke
mkdir -p "$smoke_directory"
printf '%s\n' '{"backend": "local"}' >"$smoke_directory/trails_store.json"
printf '%s\n' \
  '{"tokens": {"smoke": {"token": "test-token", "permissions": ["write"]}}}' \
  >"$smoke_directory/trails_tokens.json"

smoke_build oops/trails/server/Dockerfile
smoke_copy "$smoke_directory/trails_store.json" /app/.configs/trails_store.json
smoke_copy "$smoke_directory/trails_tokens.json" /app/.configs/trails_tokens.json
smoke_start 8004

smoke_await /health
smoke_assert_status /health 200
smoke_assert_status /v1/trails 401
smoke_assert_status /v1/trails/anything 401 -H 'Authorization: Bearer wrong'
smoke_assert_status /v1/trails 403 -H 'Authorization: Bearer test-token'

log INFO 'verification passed'
