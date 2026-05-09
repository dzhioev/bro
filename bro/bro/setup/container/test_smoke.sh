#!/usr/bin/env -S bash -e
# smoke-test the container entrypoint: builds the image, runs the entrypoint
# with the same mount layout as `cw -c`, and verifies key postconditions.
# uses CW_SKIP_VENV=1 to skip the slow `uv sync` step.

DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
PROJ="$(cd "$DIR" && realpath "$(git rev-parse --git-common-dir)/..")"

TAG="ppp-cw:smoke-test"
echo "building image" >&2
docker build -t "$TAG" -f "$DIR/Dockerfile" --build-context "proj=$PROJ" "$DIR" >&2

# colima only shares /Users; mktemp uses /var/folders which is invisible
# inside the container. create temp dir under the project tree instead.
SMOKE_TMP="$(mktemp -d "$PROJ/.smoke-XXXXXX")"
trap 'rm -rf "$SMOKE_TMP"' EXIT
mkdir -p "$SMOKE_TMP/workspace" "$SMOKE_TMP/claude"

cat > "$SMOKE_TMP/gitconfig" << 'GC'
[user]
    name = Smoke Test
    email = test@test.com
GC

echo "ghp_fake_token" > "$SMOKE_TMP/github_token"

echo "running entrypoint" >&2
docker run --rm \
  -v "$SMOKE_TMP/workspace:/workspace" \
  -v "$PROJ:/host-repo:ro" \
  -v "$SMOKE_TMP/gitconfig:/host-gitconfig:ro" \
  -v "$SMOKE_TMP/claude:/home/cw/.claude" \
  -v "$SMOKE_TMP/github_token:/run/secrets/github_token:ro" \
  -e "HOME=/home/cw" \
  -e "CW_NAME=smoke-test" \
  -e "CW_SKIP_VENV=1" \
  "$TAG" bash -c '
    set -e
    # gitconfig should be writable (the bug this test guards against)
    git config --global --list > /dev/null
    # credential helper should be configured
    git config --global credential.helper | grep -q github_token
    # workspace should have a cloned repo
    test -d /workspace/.git
    # pre-push hook should be installed
    test -x /workspace/.git/hooks/pre-push
    # uv cache should be pre-warmed and writable by cw
    test -d /opt/uv-cache
    test -n "$(ls -A /opt/uv-cache)"
    test -w /opt/uv-cache
  ' >&2

echo "smoke test passed" >&2
