#!/usr/bin/env -S bash -e
source "$(bro-shell-dir)/prelude.sh"

REPO_ROOT="$HERE/../../.."
CDK_DIRECTORY="$REPO_ROOT/oops/deployment"
source "$(bro-oops-dir)/deploy_lib.sh"
source "$HERE/deployment_config.sh"
load_trails_deployment_config

log INFO 'diffing the trails repository, image-build, and service stacks against the account'
cdk_diff "$CDK_DIRECTORY" "${TRAILS_STACKS[@]}"
