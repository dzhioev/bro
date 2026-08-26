#!/usr/bin/env -S bash -e
source "$(bro-shell-dir)/prelude.sh"

DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
REPO_ROOT="$DIR/.."
source "$(bro-oops-dir)/deploy_lib.sh"
source "$DIR/trails/server/deployment_config.sh"
load_trails_deployment_config

if [ "$#" -ne 1 ] || [ "$1" != 'trails' ]; then
  echo 'usage: image_build.sh trails' >&2
  exit 2
fi

cd "$REPO_ROOT"
commit="$(git rev-parse HEAD)"
image_repository="$(ecr_uri "$TRAILS_REPOSITORY" "$TRAILS_REGION")"
latest_image="${image_repository}:latest"
commit_image="${image_repository}:${commit}"

log INFO 'logging into ECR'
ecr_login "$TRAILS_REGION"

build_and_push \
  "$latest_image" \
  oops/trails/server/Dockerfile \
  -t "$commit_image" \
  --build-arg "AWS_REGION=$TRAILS_REGION" \
  .
