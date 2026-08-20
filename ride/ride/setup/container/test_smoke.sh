#!/usr/bin/env -S bash -e
source "$(bro-shell-dir)/prelude.sh"

DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
PROJECT="$(realpath "$DIR/../../../..")"
TAG="bro/framework:smoke-test"

# Colima shares the project tree but not the host's default temporary directory.
SMOKE_TMP="$(mktemp -d "$PROJECT/.smoke-XXXXXX")"
trap 'rm -rf "$SMOKE_TMP"' EXIT
mkdir -p \
  "$SMOKE_TMP/workspace" "$SMOKE_TMP/claude" "$SMOKE_TMP/bro" \
  "$SMOKE_TMP/detached-workspace" "$SMOKE_TMP/detached-claude" "$SMOKE_TMP/detached-bro"
echo '{}' > "$SMOKE_TMP/bro/credentials.json"
echo '{}' > "$SMOKE_TMP/detached-bro/credentials.json"

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

HOST_REPO="$SMOKE_TMP/host-repo"
git clone --quiet "$PROJECT" "$HOST_REPO"
git -C "$HOST_REPO" remote set-url origin "$(git -C "$PROJECT" remote get-url origin)"
git -C "$HOST_REPO" update-ref refs/remotes/origin/master HEAD

cat > "$SMOKE_TMP/gitconfig" <<'GC'
[user]
    name = Smoke Test
    email = test@test.com
[init]
    defaultBranch = master
GC

echo '{"projects":{"/workspace":{"smoke_seed":true}}}' > "$SMOKE_TMP/claude/.claude.json"
echo '{"host_marker":"untouched"}' > "$SMOKE_TMP/host_claude.json"
HOST_CLAUDE_SHA="$(sha256sum "$SMOKE_TMP/host_claude.json" | cut -d' ' -f1)"

echo "running entrypoint" >&2
docker run --rm -i \
  -v "$SMOKE_TMP/workspace:/workspace" \
  -v "$HOST_REPO:/host-repo:ro" \
  -v "$SMOKE_TMP/gitconfig:/host-gitconfig:ro" \
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
    git config --global --list > /dev/null
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
  -v "$SMOKE_TMP/gitconfig:/host-gitconfig:ro" \
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
