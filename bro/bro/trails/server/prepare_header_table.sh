#!/usr/bin/env -S bash -e
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../../setup/prelude.sh"

DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
REPO_ROOT="$DIR/../.."
source "$REPO_ROOT/infra/deploy_lib.sh"

export TRAILS_HEADER_TABLE=trails
cdk_deploy TrailsServerStack
