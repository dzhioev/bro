#!/usr/bin/env -S bash -e
source "$(bro-shell-dir)/prelude.sh"

DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
REPO_ROOT="$DIR/../../.."
CDK_DIRECTORY="$REPO_ROOT/oops/deployment"
source "$(bro-oops-dir)/deploy_lib.sh"
source "$DIR/deployment_config.sh"
load_trails_deployment_config

log INFO 'deploying the trails repository and image-build stacks'
cdk_deploy "$CDK_DIRECTORY" "$TRAILS_REPOSITORY_STACK" "$TRAILS_IMAGE_BUILD_STACK"

log INFO 'building and pushing the trails image through CodeBuild'
trigger_image_build trails "$TRAILS_REPOSITORY" "$TRAILS_IMAGE_BUILD_PROJECT" "$TRAILS_REGION"

log INFO 'deploying the trails service stack'
cdk_deploy "$CDK_DIRECTORY" "$TRAILS_STACK"
