#!/usr/bin/env -S bash -e
#
# Shared docker smoke-test helper for server images.
# Source this, then call:
#
#   smoke_build <dockerfile>
#   smoke_start <internal-port> [-e KEY=VAL ...]
#   smoke_await <path> [-H <header>]
#   smoke_curl <curl-args...>          # pre-fills http://localhost:$SMOKE_PORT
#   smoke_assert_status <path> <expected-status> [-H <header>]
#
# Container is cleaned up on EXIT. $SMOKE_PORT holds the mapped host port.

SMOKE_PORT=
_SMOKE_CID=
_SMOKE_IMAGE=

smoke_build() {
  local dockerfile=$1
  _SMOKE_IMAGE="smoke-test-$$"

  echo "=== docker build ==="
  docker build -f "$dockerfile" -t "$_SMOKE_IMAGE" .
}

smoke_start() {
  local internal_port=$1; shift
  SMOKE_PORT=$((internal_port + 10000 + RANDOM % 10000))

  echo "=== starting container ==="
  _SMOKE_CID=$(docker run -d --rm -p "${SMOKE_PORT}:${internal_port}" "$@" "$_SMOKE_IMAGE")
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
  docker logs "$_SMOKE_CID" >&2
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
  docker stop "$_SMOKE_CID" >/dev/null 2>&1 || true
}
