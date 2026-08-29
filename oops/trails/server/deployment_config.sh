#!/usr/bin/env bash
# sourceable trails deployment configuration resolved from the infra credential.

load_trails_deployment_config() {
  local config
  config="$(python3 - <<'PY'
import json
from dataclasses import asdict

from bro.oops.cdk import resolve

print(json.dumps(asdict(resolve())))
PY
)"
  TRAILS_REGION="$(jq -er '.region' <<<"$config")"
  TRAILS_CLUSTER="$(jq -er '.platform.cluster_name' <<<"$config")"
  TRAILS_REPOSITORY_STACK="$(
    jq -er '.trails.repository as $name | .repositories[$name].stack_name' <<<"$config"
  )"
  TRAILS_REPOSITORY="$(
    jq -er '.trails.repository as $name | .repositories[$name].repository_name' <<<"$config"
  )"
  TRAILS_IMAGE_BUILD_STACK="$(jq -er '.image_build.stack_name' <<<"$config")"
  TRAILS_IMAGE_BUILD_PROJECT="$(jq -er '.image_build.project_name' <<<"$config")"
  TRAILS_STACK="$(jq -er '.trails.stack_name' <<<"$config")"
  TRAILS_SERVICE="$(jq -er '.trails.service_name' <<<"$config")"
  TRAILS_URL="https://trails.$(jq -er '.delegated_subdomain' <<<"$config")"
}
