#!/usr/bin/env -S bash -e
source "$(bro-shell-dir)/prelude.sh"

DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
PROJECT="$(realpath "$DIR/../../../..")"
TAG="bro/framework:smoke-test"

# Colima shares the project tree but not the host's default temporary directory.
SMOKE_TMP="$(mktemp -d "$PROJECT/.smoke-XXXXXX")"
cleanup() {
  if [ -n "${CONSUMER_UV_TAG:-}" ]; then
    docker image rm -f "$CONSUMER_UV_TAG" >/dev/null 2>&1 || true
  fi
  rm -rf "$SMOKE_TMP"
}
trap cleanup EXIT
mkdir -p \
  "$SMOKE_TMP/workspace" "$SMOKE_TMP/claude" "$SMOKE_TMP/bro" \
  "$SMOKE_TMP/detached-workspace" "$SMOKE_TMP/detached-claude" "$SMOKE_TMP/detached-bro" \
  "$SMOKE_TMP/consumer-uv-workspace" "$SMOKE_TMP/consumer-uv-claude" "$SMOKE_TMP/consumer-uv-bro" \
  "$SMOKE_TMP/consumer-plain-workspace" "$SMOKE_TMP/consumer-plain-claude" \
  "$SMOKE_TMP/consumer-plain-bro"
for store in "$SMOKE_TMP/bro" "$SMOKE_TMP/detached-bro" \
  "$SMOKE_TMP/consumer-uv-bro" "$SMOKE_TMP/consumer-plain-bro"; do
  echo '{}' > "$store/credentials.json"
done

echo "building runtime and project images" >&2
python - "$TAG" "$PROJECT" "$SMOKE_TMP/bundle-hash" >&2 <<'PY'
import sys
from pathlib import Path
from ride.runtime_bundle import resolve_runtime_bundle
from ride.workspace.docker import (
  _ensure_runtime_image,
  build_project_image,
  runtime_image_tag,
)

tag, project, output = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
with resolve_runtime_bundle() as bundle:
  runtime = runtime_image_tag(bundle.python_version)
  _ensure_runtime_image(runtime, bundle.python_version)
  build_project_image(tag, runtime, project)
  bundle.materialize_container(runtime)
  output.write_text(bundle.hash)
  output.with_name('runtime-tag').write_text(runtime)
PY
BUNDLE_HASH="$(cat "$SMOKE_TMP/bundle-hash")"
RUNTIME_TAG="$(cat "$SMOKE_TMP/runtime-tag")"

make_consumer_repo() {
  local directory="$1"
  local image_repository="$2"
  mkdir -p "$directory"
  cat > "$directory/pyproject.toml" <<EOF
[project]
name = "consumer-smoke"
version = "0.1.0"
requires-python = ">=3.12"

[tool.uv]
package = false

[tool.bro]
default = "bro"
image-repository = "$image_repository"
EOF
  git -C "$directory" init --quiet --initial-branch master
  git -C "$directory" config user.name 'Smoke Test'
  git -C "$directory" config user.email test@test.com
  git -C "$directory" remote add origin https://example.invalid/consumer-smoke.git
}

CONSUMER_UV_REPO="$SMOKE_TMP/consumer-uv-repo"
CONSUMER_UV_TAG="bro/consumer-uv-smoke:test"
make_consumer_repo "$CONSUMER_UV_REPO" bro/consumer-uv-smoke
uv lock --directory "$CONSUMER_UV_REPO"
! grep -Eq '^name = "bro(-native|-ride|-dev)?"$' "$CONSUMER_UV_REPO/uv.lock"
git -C "$CONSUMER_UV_REPO" add .
git -C "$CONSUMER_UV_REPO" commit --quiet -m initial

CONSUMER_PLAIN_REPO="$SMOKE_TMP/consumer-plain-repo"
make_consumer_repo "$CONSUMER_PLAIN_REPO" bro/consumer-plain-smoke
git -C "$CONSUMER_PLAIN_REPO" add .
git -C "$CONSUMER_PLAIN_REPO" commit --quiet -m initial

python - "$CONSUMER_UV_TAG" "$RUNTIME_TAG" "$CONSUMER_UV_REPO" <<'PY'
import sys
from pathlib import Path
from ride.workspace.docker import build_project_image

build_project_image(sys.argv[1], sys.argv[2], Path(sys.argv[3]))
PY

HOST_REPO="$SMOKE_TMP/host-repo"
git clone --quiet "$PROJECT" "$HOST_REPO"
git -C "$HOST_REPO" remote set-url origin "$(git -C "$PROJECT" remote get-url origin)"
git -C "$HOST_REPO" update-ref refs/remotes/origin/master HEAD

echo '{"projects":{"/workspace":{"smoke_seed":true}}}' > "$SMOKE_TMP/claude/.claude.json"
echo '{"host_marker":"untouched"}' > "$SMOKE_TMP/host_claude.json"
HOST_CLAUDE_SHA="$(sha256sum "$SMOKE_TMP/host_claude.json" | cut -d' ' -f1)"

echo "running entrypoint" >&2
docker run --rm -i \
  -v "$SMOKE_TMP/workspace:/workspace" \
  -v "$HOST_REPO:/host-repo:ro" \
  -v "$SMOKE_TMP/claude:/home/ride/.claude" \
  -v "$SMOKE_TMP/bro:/home/ride/.bro" \
  -v "ride-runtime-$BUNDLE_HASH:/var/ride/runtime:ro" \
  -e "HOME=/home/ride" \
  -e "CLAUDE_CONFIG_DIR=/home/ride/.claude" \
  -e "RIDE_WORKSPACE=smoke-test" \
  -e "RIDE_REPO=$HOST_REPO" \
  -e "RIDE_BRANCH=worktree-smoke-test" \
  "$TAG" bash -s >&2 <<'SMOKE'
    set -e
    # the install-hook pass ran: its session directory is there, with no hook to
    # apply from the empty scoped registry the run mounts
    test -d "$HOME/.bro-environment"
    test -d /workspace/.git
    cd /workspace
    test "$(git rev-parse HEAD)" = "$(git -C /host-repo rev-parse HEAD)"
    test "$(git rev-parse refs/remotes/origin/master)" = "$(git -C /host-repo rev-parse refs/remotes/origin/master)"

    aws --version
    docker --version
    test -d /opt/uv-cache
    test -n "$(ls -A /opt/uv-cache)"
    test -w /opt/uv-cache

    test -x /opt/project-venv/bin/run-tests
    test "$(readlink /workspace/.venv)" = /opt/project-venv
    test "$(command -v ride)" = /var/ride/runtime/bin/ride
    test "$(command -v broxy)" = /var/ride/runtime/bin/broxy
    test -z "${VIRTUAL_ENV:-}"
    case ":$PATH:" in *:/opt/project-venv/bin:*) exit 1;; esac

    EDITABLE_PATHS="$(grep -h '^/workspace' /opt/project-venv/lib/python*/site-packages/*.pth)"
    test -n "$EDITABLE_PATHS"
    while IFS= read -r EDITABLE_PATH; do
      test -d "$EDITABLE_PATH"
    done <<< "$EDITABLE_PATHS"

    STAGED_MANIFESTS="$(cd /opt/project-venv-manifest && find . -type f)"
    test -n "$STAGED_MANIFESTS"
    while IFS= read -r MANIFEST; do
      cmp -s "/opt/project-venv-manifest/$MANIFEST" "/workspace/$MANIFEST"
    done <<< "$STAGED_MANIFESTS"

    grep -q smoke_seed "$CLAUDE_CONFIG_DIR/.claude.json"
    echo '{"modified_by_container":true}' > "$CLAUDE_CONFIG_DIR/.claude.json"
SMOKE

grep -q modified_by_container "$SMOKE_TMP/claude/.claude.json"
test "$(sha256sum "$SMOKE_TMP/host_claude.json" | cut -d' ' -f1)" = "$HOST_CLAUDE_SHA"

echo "running consumer fixture without bro in its lock" >&2
docker run --rm \
  -v "$SMOKE_TMP/consumer-uv-workspace:/workspace" \
  -v "$CONSUMER_UV_REPO:/host-repo:ro" \
  -v "$SMOKE_TMP/consumer-uv-claude:/home/ride/.claude" \
  -v "$SMOKE_TMP/consumer-uv-bro:/home/ride/.bro" \
  -v "ride-runtime-$BUNDLE_HASH:/var/ride/runtime:ro" \
  -e "HOME=/home/ride" \
  -e "CLAUDE_CONFIG_DIR=/home/ride/.claude" \
  -e "RIDE_WORKSPACE=consumer-uv-smoke" \
  -e "RIDE_REPO=$CONSUMER_UV_REPO" \
  -e "RIDE_BRANCH=worktree-consumer-uv-smoke" \
  "$CONSUMER_UV_TAG" bash -ec '
    test "$(command -v ride)" = /var/ride/runtime/bin/ride
    test "$(readlink /workspace/.venv)" = /opt/project-venv
    /opt/project-venv/bin/python -c '\''import importlib.metadata as m; names = {d.metadata["Name"].lower() for d in m.distributions()}; assert names.isdisjoint({"bro", "bro-native", "bro-ride", "bro-dev"})'\''
    test ! -e /workspace/setup.sh
  ' >&2

echo "running consumer fixture without a uv lock or setup.sh" >&2
python - "$RUNTIME_TAG" "$CONSUMER_PLAIN_REPO" <<'PY'
import sys
from pathlib import Path
from ride.workspace.docker import project_image_tag

assert project_image_tag(sys.argv[1], Path(sys.argv[2])) is None
PY
docker run --rm \
  -v "$SMOKE_TMP/consumer-plain-workspace:/workspace" \
  -v "$CONSUMER_PLAIN_REPO:/host-repo:ro" \
  -v "$SMOKE_TMP/consumer-plain-claude:/home/ride/.claude" \
  -v "$SMOKE_TMP/consumer-plain-bro:/home/ride/.bro" \
  -v "ride-runtime-$BUNDLE_HASH:/var/ride/runtime:ro" \
  -e "HOME=/home/ride" \
  -e "CLAUDE_CONFIG_DIR=/home/ride/.claude" \
  -e "RIDE_WORKSPACE=consumer-plain-smoke" \
  -e "RIDE_REPO=$CONSUMER_PLAIN_REPO" \
  -e "RIDE_BRANCH=worktree-consumer-plain-smoke" \
  "$RUNTIME_TAG" bash -ec '
    test "$(command -v ride)" = /var/ride/runtime/bin/ride
    test ! -e /workspace/.venv
    test ! -e /workspace/setup.sh
  ' >&2

echo "running detached entrypoint" >&2
docker run --rm \
  -v "$SMOKE_TMP/detached-workspace:/workspace" \
  -v "$SMOKE_TMP/detached-claude:/home/ride/.claude" \
  -v "$SMOKE_TMP/detached-bro:/home/ride/.bro" \
  -v "ride-runtime-$BUNDLE_HASH:/var/ride/runtime:ro" \
  -e "HOME=/home/ride" \
  -e "CLAUDE_CONFIG_DIR=/home/ride/.claude" \
  -e "RIDE_WORKSPACE=detached-smoke-test" \
  "$RUNTIME_TAG" bash -c 'test -z "$(find /workspace -mindepth 1 -print -quit)"' >&2

rm "$SMOKE_TMP/workspace/setup.sh" "$SMOKE_TMP/workspace/.venv"
ln -s /opt/ride-venv "$SMOKE_TMP/workspace/.venv"
docker run --rm \
  -v "$SMOKE_TMP/workspace:/workspace" \
  -v "$HOST_REPO:/host-repo:ro" \
  -v "$SMOKE_TMP/claude:/home/ride/.claude" \
  -v "$SMOKE_TMP/bro:/home/ride/.bro" \
  -v "ride-runtime-$BUNDLE_HASH:/var/ride/runtime:ro" \
  -e "HOME=/home/ride" \
  -e "CLAUDE_CONFIG_DIR=/home/ride/.claude" \
  -e "RIDE_WORKSPACE=smoke-test" \
  -e "RIDE_REPO=$HOST_REPO" \
  -e "RIDE_BRANCH=worktree-smoke-test" \
  "$TAG" bash -c 'test "$(readlink /workspace/.venv)" = /opt/project-venv' >&2

echo "smoke test passed" >&2
