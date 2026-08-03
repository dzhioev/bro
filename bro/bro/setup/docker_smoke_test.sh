#!/usr/bin/env -S bash -e
#
# shared docker/podman smoke-test helper for server images.
# source this with the OCI command and base-image builder, then call:
#
#   source "$(bro-shell-dir)/docker_smoke_test.sh" "$OCI_CMD" ensure_server_base
#   smoke_build <dockerfile>
#   smoke_start <internal-port> [-e KEY=VAL ...]
#   smoke_await <path> [-H <header>]
#   smoke_curl <curl-args...>          # pre-fills http://localhost:$SMOKE_PORT
#   smoke_assert_status <path> <expected-status> [-H <header>]
#
# container is cleaned up on EXIT. $SMOKE_PORT holds the mapped host port.

if [ "$#" -ne 2 ]; then
  echo "usage: source docker_smoke_test.sh <oci-command> <base-image-builder>" >&2
  return 2
fi
_SMOKE_OCI_COMMAND="$1"
_SMOKE_BASE_IMAGE_BUILDER="$2"
if ! command -v "$_SMOKE_OCI_COMMAND" >/dev/null; then
  echo "OCI command not found: $_SMOKE_OCI_COMMAND" >&2
  return 2
fi
if ! command -v "$_SMOKE_BASE_IMAGE_BUILDER" >/dev/null; then
  echo "base-image builder not found: $_SMOKE_BASE_IMAGE_BUILDER" >&2
  return 2
fi

SMOKE_PORT=
_SMOKE_CID=
_SMOKE_IMAGE=

smoke_build() {
  local dockerfile=$1
  _SMOKE_IMAGE="smoke-test-$$"

  "$_SMOKE_BASE_IMAGE_BUILDER" "$dockerfile"
  echo "=== $_SMOKE_OCI_COMMAND build ==="
  "$_SMOKE_OCI_COMMAND" build -f "$dockerfile" -t "$_SMOKE_IMAGE" .
}

smoke_start() {
  local internal_port=$1; shift
  SMOKE_PORT=$((internal_port + 10000 + RANDOM % 10000))

  echo "=== starting container ==="
  _SMOKE_CID=$("$_SMOKE_OCI_COMMAND" run -d --rm -p "${SMOKE_PORT}:${internal_port}" "$@" "$_SMOKE_IMAGE")
  trap '_smoke_cleanup' EXIT
}

smoke_await() {
  local path=$1; shift
  for _ in {1..30}; do
    if curl -s -o /dev/null -w '' "http://localhost:${SMOKE_PORT}${path}" "$@" 2>/dev/null; then
      return 0
    fi
    sleep 0.5
  done
  echo "container failed to become ready" >&2
  "$_SMOKE_OCI_COMMAND" logs "$_SMOKE_CID" >&2
  exit 1
}

smoke_curl() {
  local path=$1; shift
  curl -s "http://localhost:${SMOKE_PORT}${path}" "$@"
}

smoke_assert_status() {
  local path=$1 expected=$2; shift 2
  local actual
  actual=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${SMOKE_PORT}${path}" "$@")
  if [ "$actual" != "$expected" ]; then
    echo "expected $expected on $path, got $actual" >&2
    exit 1
  fi
}

_smoke_cleanup() {
  "$_SMOKE_OCI_COMMAND" stop "$_SMOKE_CID" >/dev/null 2>&1 || true
}
