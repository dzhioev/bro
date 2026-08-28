#!/usr/bin/env -S bash -e
source "$(bro-shell-dir)/prelude.sh"

DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
REPO_ROOT="$DIR/.."
source "$(bro-oops-dir)/deploy_lib.sh"

if [ "$#" -ne 1 ] || [ "$1" != 'trails' ]; then
  echo 'usage: image_build.sh trails' >&2
  exit 2
fi
: "${IMAGE_REPOSITORY:?is set by trigger_image_build}"
: "${IMAGE_REGION:?is set by trigger_image_build}"

cd "$REPO_ROOT"
commit="$(git rev-parse HEAD)"
image_repository="$(ecr_uri "$IMAGE_REPOSITORY" "$IMAGE_REGION")"
latest_image="${image_repository}:latest"
commit_image="${image_repository}:${commit}"

log INFO 'logging into ECR'
ecr_login "$IMAGE_REGION"

build_and_push \
  "$latest_image" \
  oops/trails/server/Dockerfile \
  -t "$commit_image" \
  --build-arg "AWS_REGION=$IMAGE_REGION" \
  .
