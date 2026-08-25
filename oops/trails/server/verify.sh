#!/usr/bin/env -S bash -e
source "$(bro-shell-dir)/prelude.sh"

DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
source "$DIR/deployment_config.sh"
load_trails_deployment_config

"$(bro-oops-dir)/monitor_ecs.sh" "$TRAILS_REGION" "$TRAILS_CLUSTER" "$TRAILS_SERVICE"
curl --fail --silent --show-error "$TRAILS_URL/health"
printf '\n'
