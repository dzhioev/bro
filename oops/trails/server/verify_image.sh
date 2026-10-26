#!/usr/bin/env -S bash -e
source "$(bro-shell-dir)/prelude.sh"

REPO_ROOT="$HERE/../../.."
cd "$REPO_ROOT"

source "$(bro-oops-dir)/deploy_lib.sh"
source "$(bro-shell-dir)/docker_smoke_test.sh" docker prepare_image_build

smoke_directory=build/trails-smoke
mkdir -p "$smoke_directory/creds"
printf '%s\n' '{}' >"$smoke_directory/creds.json"
printf '%s\n' '{"backend": "local"}' >"$smoke_directory/creds/trails.cred"
printf '%s\n' \
  '{"tokens": {"smoke": {"token": "test-token", "permissions": ["read", "write"]}}}' \
  >"$smoke_directory/creds/trails_tokens.cred"

smoke_build oops/trails/server/Dockerfile
smoke_copy "$smoke_directory/creds.json" /app/.configs/creds.json
smoke_copy "$smoke_directory/creds" /app/.configs/creds
smoke_copy oops/trails/server/image_cases.json /app/image_cases.json
smoke_copy oops/trails/server/verify_routes.py /app/verify_routes.py
smoke_start 8004

smoke_await /health
smoke_assert_status /health 200
smoke_assert_status /v1/trails 401
smoke_assert_status /v1/trails/anything 401 -H 'Authorization: Bearer wrong'
smoke_assert_status /v1/trails 200 -H 'Authorization: Bearer test-token'
docker exec "$_SMOKE_CID" python /app/verify_routes.py \
  http://127.0.0.1:8004 test-token /app/image_cases.json

trap 'docker rm -f "$_SMOKE_IMAGE-missing" >/dev/null 2>&1 || true; _smoke_cleanup' EXIT
missing_status=0
timeout 30s docker run --rm --name "$_SMOKE_IMAGE-missing" --user root --entrypoint python "$_SMOKE_IMAGE" -c '
import importlib.metadata
import os
import subprocess

entry = next(entry for entry in importlib.metadata.entry_points(group="bro.llm.wires") if entry.name == "chat")
subprocess.run(["uv", "pip", "uninstall", "--system", entry.dist.name], check=True)
os.execvp("trails-server", ["trails-server", "--entries", "/app/entries.json"])
' >"$smoke_directory/missing-entry.log" 2>&1 || missing_status=$?
if [ "$missing_status" -ne 1 ]; then
  cat "$smoke_directory/missing-entry.log" >&2
  log ERROR "expected startup failure, got status $missing_status"
  exit 1
fi
grep -F 'cannot serve bro.llm.wires:chat' "$smoke_directory/missing-entry.log"

log INFO 'verification passed'
