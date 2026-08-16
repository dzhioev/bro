#!/usr/bin/env -S bash -e
#
# shared docker/podman smoke-test helper for server images.
# source this with the OCI command and a build-preparation function, then call:
#
#   source "$(bro-shell-dir)/docker_smoke_test.sh" "$OCI_CMD" <build-preparer>
#   smoke_build <dockerfile>
#   smoke_copy <source> <container-path>
#   smoke_start <internal-port> [-e KEY=VAL ...]
#   smoke_await <path> [-H <header>]
#   smoke_curl <path> [-H <header>]
#   smoke_assert_status <path> <expected-status> [-H <header>]
#
# container is cleaned up on EXIT. $SMOKE_PORT holds the mapped host port.

if [ "$#" -ne 2 ]; then
  echo "usage: source docker_smoke_test.sh <oci-command> <build-preparer>" >&2
  return 2
fi
_SMOKE_OCI_COMMAND="$1"
_SMOKE_BUILD_PREPARER="$2"
if ! command -v "$_SMOKE_OCI_COMMAND" >/dev/null; then
  echo "OCI command not found: $_SMOKE_OCI_COMMAND" >&2
  return 2
fi
if ! command -v "$_SMOKE_BUILD_PREPARER" >/dev/null; then
  echo "build preparer not found: $_SMOKE_BUILD_PREPARER" >&2
  return 2
fi

SMOKE_PORT=
_SMOKE_CID=
_SMOKE_IMAGE=
_SMOKE_INTERNAL_PORT=
_SMOKE_COPIES=()

smoke_build() {
  local dockerfile=$1
  _SMOKE_IMAGE="smoke-test-$$"

  "$_SMOKE_BUILD_PREPARER" "$dockerfile"
  echo "=== $_SMOKE_OCI_COMMAND build ==="
  "$_SMOKE_OCI_COMMAND" build -f "$dockerfile" -t "$_SMOKE_IMAGE" .
}

# stage a file or directory for the next smoke_start to place in the container.
# the copy streams from this client, while a `-v` source is resolved by the
# daemon against its own filesystem: a caller running with the host docker
# socket bind-mounted can name no path the daemon shares, and docker answers an
# unresolvable mount source by creating an empty directory there.
smoke_copy() {
  local source=$1 container_path=$2
  if [ ! -e "$source" ]; then
    echo "copy source does not exist: $source" >&2
    exit 1
  fi
  _SMOKE_COPIES+=("$source" "$container_path")
}

smoke_start() {
  local internal_port=$1; shift
  _SMOKE_INTERNAL_PORT=$internal_port
  SMOKE_PORT=$((internal_port + 10000 + RANDOM % 10000))

  echo "=== starting container ==="
  _SMOKE_CID=$("$_SMOKE_OCI_COMMAND" create -p "${SMOKE_PORT}:${internal_port}" "$@" "$_SMOKE_IMAGE")
  # armed before the copies, which can fail with the container already created
  trap '_smoke_cleanup' EXIT
  local index
  for ((index = 0; index < ${#_SMOKE_COPIES[@]}; index += 2)); do
    "$_SMOKE_OCI_COMMAND" cp "${_SMOKE_COPIES[index]}" "$_SMOKE_CID:${_SMOKE_COPIES[index + 1]}"
  done
  "$_SMOKE_OCI_COMMAND" start "$_SMOKE_CID" >/dev/null
}

_smoke_request() {
  local output_mode=$1 path=$2; shift 2
  "$_SMOKE_OCI_COMMAND" exec "$_SMOKE_CID" python -c '
import http.client
import sys
from contextlib import closing

output_mode, port, path, *arguments = sys.argv[1:]
headers = {}
while len(arguments) > 0:
  if len(arguments) < 2 or arguments[0] != "-H":
    raise RuntimeError(f"unsupported smoke request arguments: {arguments}")
  name, value = arguments[1].split(":", 1)
  headers[name.strip()] = value.strip()
  arguments = arguments[2:]

with closing(http.client.HTTPConnection("127.0.0.1", int(port), timeout=2)) as connection:
  connection.request("GET", path, headers=headers)
  response = connection.getresponse()
  body = response.read()

if output_mode == "body":
  sys.stdout.buffer.write(body)
elif output_mode == "status":
  print(response.status, end="")
elif output_mode != "ready":
  raise RuntimeError(f"unknown output mode: {output_mode}")
' "$output_mode" "$_SMOKE_INTERNAL_PORT" "$path" "$@"
}

smoke_await() {
  local path=$1; shift
  for _ in {1..30}; do
    if _smoke_request ready "$path" "$@" >/dev/null 2>&1; then
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
  _smoke_request body "$path" "$@"
}

smoke_assert_status() {
  local path=$1 expected=$2; shift 2
  local actual
  actual=$(_smoke_request status "$path" "$@")
  if [ "$actual" != "$expected" ]; then
    echo "expected $expected on $path, got $actual" >&2
    exit 1
  fi
}

_smoke_cleanup() {
  "$_SMOKE_OCI_COMMAND" rm -f "$_SMOKE_CID" >/dev/null 2>&1 || true
}
