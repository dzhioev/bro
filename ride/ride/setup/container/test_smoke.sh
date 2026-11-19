#!/usr/bin/env -S bash -e
source "$(bro-shell-dir)/prelude.sh"

PROJECT="$(realpath "$HERE/../../../..")"

# Colima shares the project tree but not the host's default temporary directory.
SMOKE_TMP="$(mktemp -d "$PROJECT/.smoke-XXXXXX")"
cleanup() {
  rm -rf "$SMOKE_TMP"
}
trap cleanup EXIT
mkdir -p "$SMOKE_TMP/claude" "$SMOKE_TMP/bro/creds" "$SMOKE_TMP/detached-workspace"
echo '{}' > "$SMOKE_TMP/bro/creds.json"

log INFO 'building the runtime image'
python - "$SMOKE_TMP/bundle-hash" >&2 <<'PY'
import sys
from pathlib import Path
from ride.runtime_bundle import resolve_runtime_bundle
from ride.workspace.docker import ensure_runtime_image, _preflight_daemon, runtime_image_tag

output = Path(sys.argv[1])
with resolve_runtime_bundle() as bundle:
  runtime = runtime_image_tag(bundle.python_version)
  ensure_runtime_image(runtime, bundle.python_version)
  bundle.materialize_container(runtime, preflight=lambda: _preflight_daemon(runtime))
  output.write_text(bundle.hash)
  output.with_name('runtime-tag').write_text(runtime)
PY
BUNDLE_HASH="$(cat "$SMOKE_TMP/bundle-hash")"
RUNTIME_TAG="$(cat "$SMOKE_TMP/runtime-tag")"

HOST_REPO="$SMOKE_TMP/host-repo"
git clone --quiet "$PROJECT" "$HOST_REPO"
git -C "$HOST_REPO" remote set-url origin "$(git -C "$PROJECT" remote get-url origin)"
git -C "$HOST_REPO" update-ref refs/remotes/origin/master HEAD
python - "$HOST_REPO" "$SMOKE_TMP/workspace" <<'PY'
import sys
from pathlib import Path
from ride.repository import Repository
from ride.workspace.clones import ensure_clone

source = Path(sys.argv[1])
ensure_clone(Repository(str(source), source), Path(sys.argv[2]), 'workspace-smoke-test')
PY
EXPECTED_HEAD="$(git -C "$HOST_REPO" rev-parse HEAD)"
EXPECTED_ORIGIN_MASTER="$(git -C "$HOST_REPO" rev-parse refs/remotes/origin/master)"
printf '#!/bin/sh\nexit 99\n' > "$SMOKE_TMP/workspace/setup.sh"
chmod +x "$SMOKE_TMP/workspace/setup.sh"
ln -s /missing-environment "$SMOKE_TMP/workspace/.venv"

echo '{"projects":{"/workspace":{"smoke_seed":true}}}' > "$SMOKE_TMP/claude/.claude.json"
echo '{"host_marker":"untouched"}' > "$SMOKE_TMP/host_claude.json"
HOST_CLAUDE_SHA="$(sha256sum "$SMOKE_TMP/host_claude.json" | cut -d' ' -f1)"

log INFO 'running the attached entrypoint'
docker run --rm -i \
  -v "$SMOKE_TMP/workspace:/workspace" \
  -v "$SMOKE_TMP/claude:/home/ride/.claude" \
  -v "$SMOKE_TMP/bro:/home/ride/.bro" \
  -v "ride-runtime-$BUNDLE_HASH:/var/ride/runtime:ro" \
  -e 'HOME=/home/ride' \
  -e 'CLAUDE_CONFIG_DIR=/home/ride/.claude' \
  -e 'RIDE_WORKSPACE=smoke-test' \
  -e "RIDE_REPO=$HOST_REPO" \
  -e "EXPECTED_HEAD=$EXPECTED_HEAD" \
  -e "EXPECTED_ORIGIN_MASTER=$EXPECTED_ORIGIN_MASTER" \
  "$RUNTIME_TAG" bash -s >&2 <<'SMOKE'
    set -e
    cd /workspace
    test -d .git
    test "$(git rev-parse HEAD)" = "$EXPECTED_HEAD"
    test "$(git rev-parse refs/remotes/origin/master)" = "$EXPECTED_ORIGIN_MASTER"
    test ! -e .git/objects/info/alternates
    test "$(readlink .venv)" = /missing-environment
    test "$(command -v ride)" = /var/ride/runtime/bin/ride
    test "$(command -v broxy)" = /var/ride/runtime/bin/broxy
    test -z "${VIRTUAL_ENV:-}"
    test "$UV_LINK_MODE" = copy
    aws --version
    test -r /home/ride/.bro/creds.json
    grep -q smoke_seed "$CLAUDE_CONFIG_DIR/.claude.json"
    echo '{"modified_by_container":true}' > "$CLAUDE_CONFIG_DIR/.claude.json"
SMOKE

grep -q modified_by_container "$SMOKE_TMP/claude/.claude.json"
test "$(sha256sum "$SMOKE_TMP/host_claude.json" | cut -d' ' -f1)" = "$HOST_CLAUDE_SHA"

log INFO 'running the detached entrypoint'
docker run --rm \
  -v "$SMOKE_TMP/detached-workspace:/workspace" \
  -v "ride-runtime-$BUNDLE_HASH:/var/ride/runtime:ro" \
  -e 'HOME=/home/ride' \
  -e 'RIDE_WORKSPACE=detached-smoke-test' \
  "$RUNTIME_TAG" bash -c 'test -z "$(find /workspace -mindepth 1 -print -quit)"' >&2

log INFO 'smoke test passed'
