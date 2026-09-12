#!/usr/bin/env -S bash -e
source "$(bro-shell-dir)/prelude.sh"

SCRIPT_DIRECTORY="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
REPO_ROOT="$SCRIPT_DIRECTORY/../../.."
CDK_DIRECTORY="$REPO_ROOT/oops/deployment"
source "$(bro-oops-dir)/deploy_lib.sh"
source "$SCRIPT_DIRECTORY/deployment_config.sh"
load_trails_deployment_config

log INFO 'deploying the trails repository and image-build stacks'
cdk_deploy "$CDK_DIRECTORY" "${TRAILS_IMAGE_STACKS[@]}"

log INFO 'building and pushing the trails image through CodeBuild'
trigger_image_build trails "$TRAILS_REPOSITORY" "$TRAILS_IMAGE_BUILD_PROJECT" "$TRAILS_REGION"

log INFO 'deploying the trails service stack'
cdk_deploy "$CDK_DIRECTORY" "${TRAILS_SERVICE_STACKS[@]}"
