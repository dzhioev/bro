#!/usr/bin/env -S bash -e
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../prelude.sh"
# build the general-purpose bro-base image from the Dockerfile next to this
# script. extra args (e.g. --platform) are forwarded to the build. respects a
# caller-exported $OCI_CMD (docker default).

DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
OCI_CMD="${OCI_CMD:-docker}"

if [ "$OCI_CMD" = "docker" ]; then
  docker buildx build --provenance=false --load -t bro-base "$@" "$DIR"
else
  podman build -t bro-base "$@" "$DIR"
fi
