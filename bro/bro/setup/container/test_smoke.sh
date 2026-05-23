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
[init]
    defaultBranch = master
GC

echo "ghp_fake_token" > "$SMOKE_TMP/github_token"

# pre-seed the container-private .claude.json (cw.py does this on first run).
# also drop a "host" .claude.json next to it as a tripwire: it must not exist
# on any container mount, so any write the container makes to /home/cw/.claude.json
# must land in claude/.claude.json and leave host_claude.json untouched.
echo '{"projects":{"/workspace":{"smoke_seed":true}}}' > "$SMOKE_TMP/claude/.claude.json"
echo '{"host_marker":"untouched"}' > "$SMOKE_TMP/host_claude.json"
HOST_CLAUDE_SHA="$(sha256sum "$SMOKE_TMP/host_claude.json" | cut -d' ' -f1)"

echo "running entrypoint" >&2
docker run --rm \
  -v "$SMOKE_TMP/workspace:/workspace" \
  -v "$PROJ:/host-repo:ro" \
  -v "$SMOKE_TMP/gitconfig:/host-gitconfig:ro" \
  -v "$SMOKE_TMP/claude:/home/cw/.claude" \
  -v "$SMOKE_TMP/claude/.claude.json:/home/cw/.claude.json" \
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
    # aws cli should be installed
    aws --version
    # uv cache should be pre-warmed and writable by cw
    test -d /opt/uv-cache
    test -n "$(ls -A /opt/uv-cache)"
    test -w /opt/uv-cache
    # /home/cw/.claude.json reflects the container-private seed and is writable
    grep -q smoke_seed /home/cw/.claude.json
    echo "{\"modified_by_container\":true}" > /home/cw/.claude.json
  ' >&2

# container-private .claude.json reflects the in-container write
grep -q modified_by_container "$SMOKE_TMP/claude/.claude.json"
# the parallel host-shadow file is unmounted, so it must be byte-identical to before
test "$(sha256sum "$SMOKE_TMP/host_claude.json" | cut -d' ' -f1)" = "$HOST_CLAUDE_SHA"

echo "smoke test passed" >&2
