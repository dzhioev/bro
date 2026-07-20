#!/usr/bin/env -S bash -e

source /usr/local/lib/ppp-shell/prelude.sh
source /usr/local/lib/ppp-shell/container-git.sh

# root phase: align container user with host uid/gid, then re-exec as cw
if [ "$(id -u)" = "0" ] && [ -z "${CW_ENTRYPOINT_REEXEC:-}" ]; then
  TARGET_UID="$(stat -c '%u' /workspace)"
  TARGET_GID="$(stat -c '%g' /workspace)"
  # skip remapping when detected uid is 0 — on Docker for Mac, virtiofs reports
  # bind mounts as root-owned but handles permissions transparently; remapping to
  # uid 0 would make claude refuse --dangerously-skip-permissions
  if [ "$TARGET_UID" != "0" ]; then
    if [ "$(id -u cw)" != "$TARGET_UID" ] || [ "$(id -g cw)" != "$TARGET_GID" ]; then
      groupmod -o -g "$TARGET_GID" cw
      usermod -o -u "$TARGET_UID" -g "$TARGET_GID" cw
      chown cw:cw /home/cw /home/cw/.claude
    fi
  fi
  # align the in-container `docker` group's gid with the bind-mounted host
  # socket's gid so cw can talk to the host daemon without sudo
  if [ -S /var/run/docker.sock ]; then
    SOCK_GID="$(stat -c '%g' /var/run/docker.sock)"
    if [ "$(getent group docker | cut -d: -f3)" != "$SOCK_GID" ]; then
      groupmod -o -g "$SOCK_GID" docker
    fi
  fi
  # the scoped credential store is `docker cp`'d into /home/cw/.ppp before start
  # (cw/containers.py), landing owned by the uid baked into the tar. re-own it to cw after the
  # remap above so the resolver and install hooks (run as cw) can read the 0600
  # files — on Linux (cw remapped to the host uid) and on Docker for Mac (remap
  # skipped, cw keeps its image uid).
  if [ -d /home/cw/.ppp ]; then
    chown -R cw:cw /home/cw/.ppp
  fi
  export CW_ENTRYPOINT_REEXEC=1
  exec gosu cw "$0" "$@"
fi

# --- running as cw from here ---

# ~/.claude is not seeded from the host: cw constructs ~/.claude.json and
# settings.json and syncs credentials; host machine state stays on the host.

# seed host git config into a writable copy (the host file is bind-mounted
# read-only at /host-gitconfig; git config --global needs atomic rename).
# done before the clone so init.defaultBranch suppresses the git hint
if [ -f /host-gitconfig ] && [ ! -f "$HOME/.gitconfig" ]; then
  cp /host-gitconfig "$HOME/.gitconfig"
fi

# mark /workspace safe for git. on Docker for Mac, virtiofs reports the bind
# mount as root-owned even though cw can read/write it (see uid-remap skip in
# the root phase); without this, git refuses with "dubious ownership"
git config --global --add safe.directory /workspace

# first-run clone: /workspace starts empty; host repo is bind-mounted at /host-repo
# read-only. clone --shared reuses /host-repo/.git/objects via alternates so there's
# no disk duplication. origin is retargeted to the host's upstream (so `git push`
# goes to GitHub, matching host-mode worktrees), and we add `host` as a local
# remote for fetching commits that haven't been pushed upstream yet.
if [ ! -d /workspace/.git ]; then
  log INFO 'cloning host repo into /workspace'
  quiet=(-q)
  if log_enabled VERBOSE; then quiet=(); fi
  git config --global --add safe.directory /host-repo
  cd /
  git -c protocol.file.allow=always clone --shared "${quiet[@]}" /host-repo /workspace >&2
  cd /workspace
  host_origin="$(git -C /host-repo config --get remote.origin.url)"
  host_origin="$(container_git_url "$host_origin")"
  git remote set-url origin "$host_origin"
  git remote add host /host-repo
  # refresh refs/remotes/origin/master (the clone copied /host-repo's possibly-stale
  # local copy) so later ancestry/clean checks and rebases compare against the real
  # upstream. ref-only — objects are already shared via alternates, no token needed.
  git fetch "${quiet[@]}" host '+refs/remotes/origin/master:refs/remotes/origin/master' >&2
  # branch worktree-<CW_NAME> from CW_BASE_REF — a sha the host resolved for an
  # explicit base (--into <ref>) or a summoned child's inherited summoner HEAD.
  # the HEAD fallback (the clone's checkout, i.e. the host checkout's current
  # commit) is the default: a workspace bases on what its launcher has checked
  # out. -B resets if a stale worktree-<CW_NAME> branch came through with the
  # clone. either base's objects are shared from /host-repo via the clone's
  # alternates (the host resolution transfers foreign objects into the host repo
  # first), so no extra fetch is needed.
  git checkout "${quiet[@]}" -B "worktree-$CW_NAME" "${CW_BASE_REF:-HEAD}" >&2
  # initialize from host-local clones because the container has no ssh keys
  initialize_container_submodules /workspace /host-repo
fi

# pre-create the /workspace transcript directory (trust is granted in the
# constructed ~/.claude.json, not here)
mkdir -p "$HOME/.claude/projects/-workspace"

# seed the pre-installed plugins baked into the image (pyright-lsp). ~/.claude is
# bind-mounted from a fresh per-session dir, so the build-time install staged at
# /opt is copied in on first run. settings.json enables the plugin (cw/docker.py); this
# provides the matching install records so claude doesn't prompt to install it on
# .py files.
if [ -d /opt/claude-plugins-seed ] && [ ! -f "$HOME/.claude/plugins/installed_plugins.json" ]; then
  mkdir -p "$HOME/.claude/plugins"
  cp -r /opt/claude-plugins-seed/. "$HOME/.claude/plugins/"
fi

# reuse the venv baked into the image (deps + editable project already installed,
# its module finder pointing at /workspace — see the Dockerfile) instead of a fresh
# `uv sync` (~3.4s). symlink it in and stamp provision_repo.sh's skip marker newer
# than the just-cloned uv.lock/pyproject so the sync is skipped. valid only when
# the clone's dependency manifests equal the ones the image was built from —
# CW_BASE_REF can base the clone on any ref, so equality is checked against the
# staged /opt/cw-venv-manifest copies, not assumed. a mismatch (or an older image
# without the staged manifests, or a pre-existing /workspace/.venv from a reused
# workspace) falls through to a normal sync from the clone's own manifests.
# the third cmp covers a project vendoring ppp as a submodule: the baked
# console-script bridge derives from ppp/pyproject.toml's [project.scripts], so
# a clone whose submodule manifest diverges from the staged copy must re-sync
# (both sides absent — the ppp project itself — passes).
if [ "${CW_SKIP_VENV:-}" != "1" ] && [ -d /opt/cw-venv ] && [ ! -e /workspace/.venv ] \
    && cmp -s /workspace/pyproject.toml /opt/cw-venv-manifest/pyproject.toml \
    && cmp -s /workspace/uv.lock /opt/cw-venv-manifest/uv.lock \
    && { { [ ! -f /workspace/ppp/pyproject.toml ] \
        && [ ! -f /opt/cw-venv-manifest/ppp-pyproject.toml ]; } \
      || cmp -s /workspace/ppp/pyproject.toml /opt/cw-venv-manifest/ppp-pyproject.toml; }; then
  log VERBOSE 'reusing the venv baked into the image'
  ln -s /opt/cw-venv /workspace/.venv
  touch /workspace/.venv/.provision-stamp
  # the baked venv also carries a `_entrypoints.py` bridge generated from the
  # [project.scripts] of the same manifests the gate above just matched. tell
  # provision_repo.sh to skip the regen (the only other thing it does is the
  # console-script bridge + git hooks).
  export CW_VENV_BAKED=1
fi

# provision the cloned repo through its root setup.sh — the uniform provisioning
# entry point every repo cw operates on carries (venv sync if stale, console-script
# bridge, post-commit hook, git alias; a superproject setup.sh's own submodule init
# is a no-op — the clone's submodules were already initialized above from
# host-local clones). then activate the venv so child processes (hooks, MCP
# servers, Bash tool) inherit it. CW_SKIP_VENV (smoke test only) skips the whole
# venv-dependent block.
if [ "${CW_SKIP_VENV:-}" != "1" ]; then
  /workspace/setup.sh >&2
  source /workspace/.venv/bin/activate
fi

# secrets resolve from the scoped credential store bind-mounted at ~/.ppp (see
# cw/containers.py). wire each into the tool that consumes it from outside the resolver (git
# credential helper, the aws CLI's ~/.aws/credentials, ...) via its registry-declared
# install hook — one generic step, no per-secret logic here. env exports must
# persist into `exec`, so this is eval'd in the entrypoint shell.
#
# capture before eval: `eval "$(credentials install-hooks)"` would mask a generator
# crash — the failed command substitution yields empty stdout, `eval ""` exits 0, and
# set -e never fires, so claude would launch with credentials unwired. a plain
# assignment propagates the substitution's exit status to set -e, aborting the launch.
if [ "${CW_SKIP_VENV:-}" != "1" ]; then
  log VERBOSE 'installing credential hooks'
  install_hooks="$(credentials install-hooks)"
  eval "$install_hooks"
fi

# broxy: one long-lived upstream connection and a local socket for the
# in-container client swarm. `broxy launch` owns spawn, readiness, and failure
# cleanup; this entrypoint owns the fail-open launch policy.
if [ -n "${BROKER_CHANNEL:-}" ]; then
  if [ "${CW_SKIP_VENV:-}" != "1" ] && command -v broxy > /dev/null; then
    if broxy_launch="$(
      broxy launch /tmp/broxy.sock --upstream "$BROKER_CHANNEL" --log-file /tmp/broxy.log
    )"; then
      IFS=$'\t' read -r BROKER_CHANNEL _ <<< "$broxy_launch"
      export BROKER_CHANNEL
      log VERBOSE "broker channel at $BROKER_CHANNEL"
    else
      log WARNING 'broxy launch failed (log: /tmp/broxy.log); the session gets no broker channel'
      unset BROKER_CHANNEL
    fi
  else
    log WARNING 'broxy unavailable in this workspace; the session gets no broker channel'
    unset BROKER_CHANNEL
  fi
fi

# the tree is prepared; the command (for a `cw ss` session: the same
# `cw ss --in-place` runner host mode spawns, resolved from the venv activated
# above) owns everything else — MCP servers, skills, claude itself.
exec "$@"
