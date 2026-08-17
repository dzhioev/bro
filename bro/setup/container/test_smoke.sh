#!/usr/bin/env -S bash -e
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../prelude.sh"
# smoke-test the container entrypoint: builds the image, runs the entrypoint
# with the same mount layout as a ride container session, and verifies key postconditions.
# uses RIDE_SKIP_VENV=1 to skip the slow `uv sync` step.

DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
PROJECT="$(realpath "$DIR/../../..")"

TAG="bro/framework:smoke-test"
echo "building image" >&2
# through the framework's own builder, so the assembled context this test runs
# against is the one a session launch builds from. the project is passed
# explicitly: in a linked worktree project_root() resolves to the main checkout,
# and this test has to build the tree it ships in
python -c 'import sys
from pathlib import Path
from bro.workspace.docker import build_image
build_image(sys.argv[1], Path(sys.argv[2]))' "$TAG" "$PROJECT" >&2

# colima only shares /Users; mktemp uses /var/folders which is invisible
# inside the container. create temp dir under the project tree instead.
SMOKE_TMP="$(mktemp -d "$PROJECT/.smoke-XXXXXX")"
trap 'rm -rf "$SMOKE_TMP"' EXIT
mkdir -p "$SMOKE_TMP/workspace" "$SMOKE_TMP/claude"

# /host-repo has to be a self-contained repo — the entrypoint clones it with
# --shared, and a linked worktree carries only a pointer to the main checkout's
# .git. objects hardlink, so the clone costs nothing.
HOST_REPO="$SMOKE_TMP/host-repo"
git clone --quiet "$PROJECT" "$HOST_REPO"
git -C "$HOST_REPO" remote set-url origin "$(git -C "$PROJECT" remote get-url origin)"
# a clone mirrors the source's local branches only, so seed the ref the entrypoint refreshes
git -C "$HOST_REPO" update-ref refs/remotes/origin/master HEAD

cat > "$SMOKE_TMP/gitconfig" << 'GC'
[user]
    name = Smoke Test
    email = test@test.com
[init]
    defaultBranch = master
GC

# credential wiring (git helper, AWS, ...) is applied by `eval "$(credentials
# install-hooks)"` after venv activation, which this smoke test skips
# (RIDE_SKIP_VENV=1) — so that path is covered by base/credentials_test.py, not here.

# pre-seed the container-private .claude.json (bro/workspace/docker.py does this on first run).
# also drop a "host" .claude.json next to it as a tripwire: it must not exist
# on any container mount, so any write the container makes to /home/ride/.claude.json
# must land in claude/.claude.json and leave host_claude.json untouched.
echo '{"projects":{"/workspace":{"smoke_seed":true}}}' > "$SMOKE_TMP/claude/.claude.json"
echo '{"host_marker":"untouched"}' > "$SMOKE_TMP/host_claude.json"
HOST_CLAUDE_SHA="$(sha256sum "$SMOKE_TMP/host_claude.json" | cut -d' ' -f1)"

echo "running entrypoint" >&2
# the assertion body is piped to the container via a quoted heredoc + `bash -s`
# (needs `-i` so docker forwards stdin): everything between the markers reaches
# bash verbatim, so apostrophes, quotes, and `$(...)` need no escaping. don't
# switch back to `bash -c '...'` — a single apostrophe in a comment silently
# breaks out of the quote and corrupts the script.
docker run --rm -i \
  -v "$SMOKE_TMP/workspace:/workspace" \
  -v "$HOST_REPO:/host-repo:ro" \
  -v "$SMOKE_TMP/gitconfig:/host-gitconfig:ro" \
  -v "$SMOKE_TMP/claude:/home/ride/.claude" \
  -v "$SMOKE_TMP/claude/.claude.json:/home/ride/.claude.json" \
  -e "HOME=/home/ride" \
  -e "RIDE_WORKSPACE=smoke-test" \
  -e "RIDE_BRANCH=worktree-smoke-test" \
  -e "RIDE_SKIP_VENV=1" \
  "$TAG" bash -s >&2 << 'SMOKE'
    set -ex
    # gitconfig should be writable (the bug this test guards against)
    git config --global --list > /dev/null
    # workspace should have a cloned repo
    test -d /workspace/.git
    # worktree branch is based on the host repo's current HEAD (the default base,
    # matching host-mode worktrees)
    cd /workspace
    test "$(git rev-parse HEAD)" = "$(git -C /host-repo rev-parse HEAD)"
    # origin/master is still ref-refreshed from /host-repo for later clean/rebase checks
    test "$(git rev-parse refs/remotes/origin/master)" = "$(git -C /host-repo rev-parse refs/remotes/origin/master)"
    # aws cli should be installed
    aws --version
    # docker CLI should be installed for deploys via host socket
    docker --version
    # uv cache should be pre-warmed and writable by ride
    test -d /opt/uv-cache
    test -n "$(ls -A /opt/uv-cache)"
    test -w /opt/uv-cache
    # the workspace venv is baked in: console-script launchers are present, from
    # the published members and the repository-local one alike, and every editable
    # path entry points at a directory in the clone
    test -x /opt/ride-venv/bin/ask
    test -x /opt/ride-venv/bin/run-tests
    EDITABLE_PATHS="$(grep -h '^/workspace' /opt/ride-venv/lib/python*/site-packages/*.pth)"
    test -n "$EDITABLE_PATHS"
    while IFS= read -r EDITABLE_PATH; do
      test -d "$EDITABLE_PATH"
    done <<< "$EDITABLE_PATHS"
    # the framework packages ship their argv bridges as ordinary editable modules
    test -f /workspace/bro/_entrypoints.py
    test -f /workspace/dev/bro/dev/_entrypoints.py
    test -f /workspace/local/bro/local/_entrypoints.py
    # every manifest the bake ran from is staged for setup.sh's reuse gate at its
    # project-relative path, and matches this clone (based on the same tree the
    # image was built from). the staged set is walked rather than listed, so the
    # check covers whatever members the workspace declares
    STAGED_MANIFESTS="$(cd /opt/ride-venv-manifest && find . -type f)"
    test -n "$STAGED_MANIFESTS"
    while IFS= read -r MANIFEST; do
      cmp -s "/opt/ride-venv-manifest/$MANIFEST" "/workspace/$MANIFEST"
    done <<< "$STAGED_MANIFESTS"
    # /home/ride/.claude.json reflects the container-private seed and is writable
    grep -q smoke_seed /home/ride/.claude.json
    echo '{"modified_by_container":true}' > /home/ride/.claude.json
SMOKE

# container-private .claude.json reflects the in-container write
grep -q modified_by_container "$SMOKE_TMP/claude/.claude.json"
# the parallel host-shadow file is unmounted, so it must be byte-identical to before
test "$(sha256sum "$SMOKE_TMP/host_claude.json" | cut -d' ' -f1)" = "$HOST_CLAUDE_SHA"

echo "smoke test passed" >&2
