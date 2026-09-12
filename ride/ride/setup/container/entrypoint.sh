#!/usr/bin/env -S bash -e

source /usr/local/lib/bro-shell/prelude.sh

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

# Repository setup runs only for an explicitly attached launch.
if [ -n "${RIDE_REPO:-}" ]; then
# mark /workspace safe for git. on Docker for Mac, virtiofs reports the bind
# mount as root-owned even though ride can read/write it (see uid-remap skip in
# the root phase); without this, git refuses with "dubious ownership"
git config --global --add safe.directory /workspace
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

exec "$@"
