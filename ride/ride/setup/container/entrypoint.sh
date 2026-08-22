#!/usr/bin/env -S bash -e

source /usr/local/lib/bro-shell/prelude.sh
source /usr/local/lib/bro-shell/container-git.sh

# root phase: align container user with host uid/gid, then re-exec as ride
if [ "$(id -u)" = "0" ] && [ -z "${RIDE_ENTRYPOINT_REEXEC:-}" ]; then
  TARGET_UID="$(stat -c '%u' /workspace)"
  TARGET_GID="$(stat -c '%g' /workspace)"
  # skip remapping when detected uid is 0 — on Docker for Mac, virtiofs reports
  # bind mounts as root-owned but handles permissions transparently; remapping to
  # uid 0 would make claude refuse --dangerously-skip-permissions
  if [ "$TARGET_UID" != "0" ]; then
    if [ "$(id -u ride)" != "$TARGET_UID" ] || [ "$(id -g ride)" != "$TARGET_GID" ]; then
      groupmod -o -g "$TARGET_GID" ride
      usermod -o -u "$TARGET_UID" -g "$TARGET_GID" ride
      chown ride:ride /home/ride /home/ride/.claude
    fi
  fi
  # the scoped credential store is `docker cp`'d into /home/ride/.bro before start
  # (ride/ride/workspace/docker.py), landing owned by the uid baked into the tar. re-own it to ride after the
  # remap above so the resolver and install hooks (run as ride) can read the 0600
  # files — on Linux (ride remapped to the host uid) and on Docker for Mac (remap
  # skipped, ride keeps its image uid).
  if [ -d /home/ride/.bro ]; then
    chown -R ride:ride /home/ride/.bro
  fi
  export RIDE_ENTRYPOINT_REEXEC=1
  exec gosu ride "$0" "$@"
fi

# --- running as ride from here ---

# ~/.claude is not seeded from the host: ride constructs the .claude.json and
# settings.json inside it and syncs credentials; host machine state stays on the
# host.

# the credential install hooks write their wiring into a directory of the
# container's own layer, so it dies with the container the way the scoped store
# does. captured before the eval, so failing hooks abort the launch instead of
# becoming a successful empty eval.
session_environment="$(credentials install-hooks "$HOME/.bro-environment")"
eval "$session_environment"

# Repository setup runs only for an explicitly attached launch.
if [ -n "${RIDE_REPO:-}" ]; then
# mark /workspace safe for git. on Docker for Mac, virtiofs reports the bind
# mount as root-owned even though ride can read/write it (see uid-remap skip in
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
  # refresh the mounted repository's origin-tracking refs: a checkout may carry
  # local-only commits while a managed mirror carries the launch's fresh fetch.
  # ref-only — objects are already shared via alternates, no token needed.
  git fetch "${quiet[@]}" host '+refs/remotes/origin/*:refs/remotes/origin/*' >&2
  # branch RIDE_BRANCH (the workspace's recorded branch) from RIDE_BASE_REF — a sha
  # the host resolved for an explicit base (--into <ref>) or a summoned child's
  # inherited summoner HEAD. the HEAD fallback (the clone's checkout, i.e. the
  # host checkout's current commit) is the default: a workspace bases on what its
  # launcher has checked out. -B resets if a stale branch of that name came
  # through with the clone. either base's objects are shared from /host-repo via
  # the clone's alternates (the host resolution transfers foreign objects into
  # the host repo first), so no extra fetch is needed.
  git checkout "${quiet[@]}" -B "$RIDE_BRANCH" "${RIDE_BASE_REF:-HEAD}" >&2
  # initialize from host-local clones because the container has no ssh keys
  initialize_container_submodules /workspace /host-repo
fi
fi

# pre-create the /workspace transcript directory (trust is granted in the
# constructed ~/.claude.json, not here)
mkdir -p "$HOME/.claude/projects/-workspace"

# seed the pre-installed plugins baked into the image (pyright-lsp). ~/.claude is
# bind-mounted from a fresh per-session dir, so the build-time install staged at
# /opt is copied in on first run. settings.json enables the plugin (ride/ride/claude/claude_config.py); this
# provides the matching install records so claude doesn't prompt to install it on
# .py files.
if [ -d /opt/claude-plugins-seed ] && [ ! -f "$HOME/.claude/plugins/installed_plugins.json" ]; then
  mkdir -p "$HOME/.claude/plugins"
  cp -r /opt/claude-plugins-seed/. "$HOME/.claude/plugins/"
fi

# Link and provision the operated repository only when one is attached.
if [ -n "${RIDE_REPO:-}" ]; then
# Link the optional dependency bake into the clone. A symlink whose old image
# target disappeared is replaced; any real workspace environment is preserved.
if [ -L /workspace/.venv ] && [ ! -e /workspace/.venv ]; then
  rm /workspace/.venv
fi
if [ -d /opt/project-venv ]; then
  if [ ! -e /workspace/.venv ]; then
    log VERBOSE 'linking the project venv baked into the image'
    ln -s /opt/project-venv /workspace/.venv
    export RIDE_VENV_MANIFEST=/opt/project-venv-manifest
  fi
fi

if [ -f /workspace/setup.sh ]; then
  /workspace/setup.sh >&2
else
  log INFO 'setup.sh not found; skipping project provisioning'
fi
fi

# Capture before eval so a failed command substitution aborts the launch rather
# than becoming a successful empty eval.
# One local broker proxy serves the in-container client swarm. A proxy launch
# failure is expected to degrade the optional broker channel, not the session.
if [ -n "${BROKER_CHANNEL:-}" ]; then
  if broxy_launch="$(
    broxy launch --upstream "$BROKER_CHANNEL" --log-file /tmp/broxy.log
  )"; then
    IFS=$'\t' read -r BROKER_CHANNEL _ <<< "$broxy_launch"
    export BROKER_CHANNEL
    log VERBOSE 'broker channel ready'  # the address carries its token
  else
    log WARNING 'broxy launch failed (log: /tmp/broxy.log); the session gets no broker channel'
    unset BROKER_CHANNEL
  fi
fi

exec "$@"
