#!/usr/bin/env -S bash -e

# shellcheck source=bro/setup/prelude.sh
source /usr/local/lib/bro-shell/prelude.sh

# root phase: align container user with host uid/gid, then re-exec as ride
if [ "$(id -u)" = "0" ] && [ -z "${RIDE_ENTRYPOINT_REEXEC:-}" ]; then
  TARGET_UID="$(stat -c '%u' /tree)"
  TARGET_GID="$(stat -c '%g' /tree)"
  # Docker for Mac reports root-owned mounts but handles permissions transparently.
  if [ "$TARGET_UID" != "0" ]; then
    if [ "$(id -u ride)" != "$TARGET_UID" ] || [ "$(id -g ride)" != "$TARGET_GID" ]; then
      groupmod -o -g "$TARGET_GID" ride
      usermod -o -u "$TARGET_UID" -g "$TARGET_GID" ride
      chown -R ride:ride /home/ride
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

git config --global --add safe.directory '*'

exec "$@"
