#!/usr/bin/env -S bash -e
source "$(bro-shell-dir)/prelude.sh"

SCRIPT_DIRECTORY="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
REPO_ROOT="$SCRIPT_DIRECTORY/../../.."
CDK_DIRECTORY="$REPO_ROOT/oops/deployment"
source "$(bro-oops-dir)/deploy_lib.sh"
source "$SCRIPT_DIRECTORY/deployment_config.sh"
load_trails_deployment_config

log INFO 'diffing the trails repository, image-build, and service stacks against the account'
cdk_diff "$CDK_DIRECTORY" "${TRAILS_STACKS[@]}"
