#!/usr/bin/env -S bash -e
# smoke-test the container entrypoint: builds the image, runs the entrypoint
# with the same mount layout as `cw -c`, and verifies key postconditions.
# uses CW_SKIP_VENV=1 to skip the slow `uv sync` step.

DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
PROJ="$(cd "$DIR" && realpath "$(git rev-parse --git-common-dir)/..")"

TAG="ppp-cw:smoke-test"
echo "building image" >&2
docker build -t "$TAG" -f "$DIR/Dockerfile" "$DIR" >&2

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT
mkdir -p "$TMPDIR/workspace" "$TMPDIR/claude"

cat > "$TMPDIR/gitconfig" << 'GC'
[user]
    name = Smoke Test
    email = test@test.com
GC

echo "ghp_fake_token" > "$TMPDIR/github_token"

echo "running entrypoint" >&2
docker run --rm \
  -v "$TMPDIR/workspace:/workspace" \
  -v "$PROJ:/host-repo:ro" \
  -v "$TMPDIR/gitconfig:/host-gitconfig:ro" \
  -v "$TMPDIR/claude:/home/cw/.claude" \
  -v "$TMPDIR/github_token:/run/secrets/github_token:ro" \
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
  ' >&2

echo "smoke test passed" >&2
