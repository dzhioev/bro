#!/usr/bin/env -S bash -e
# one-time setup for the trails sink.
#
# reads the bearer token from SSM (/trails/bearer-token), derives the trails
# server base URL from .configs/infra.json's delegated_subdomain, and writes
# .configs/trails.json. Once this file exists, BaseBro defaults to
# HTTPTracker via _default_tracker_factory and every bro run gets recorded
# end-to-end against the deployed trails-server.
#
# prerequisites:
#   - AWS CLI configured (read-only on /trails/bearer-token is enough)
#   - TrailsServerStack deployed (creates the SSM parameter)
#   - .configs/infra.json present
#
# run once after deploying the trails CDK stack:
#   ./setup/bootstrap_trails.sh
#
# also invoked silently by the container entrypoint so dive-in sessions with
# AWS creds bootstrap themselves on first run.
#
# idempotent: if .configs/trails.json already exists, the script is a no-op.

DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
cd "$DIR/.."

REGION=eu-central-1
PARAM_NAME=/trails/bearer-token
CONFIG_PATH=.configs/trails.json

if [ -f "$CONFIG_PATH" ]; then
  echo "$CONFIG_PATH already exists, skipping"
  exit 0
fi

if [ ! -f .configs/infra.json ]; then
  echo "error: .configs/infra.json missing — cannot derive trails base_url" >&2
  exit 1
fi

DELEGATED_SUBDOMAIN=$(python3 -c "import json,sys; print(json.load(open('.configs/infra.json'))['delegated_subdomain'])")
BASE_URL="https://trails.$DELEGATED_SUBDOMAIN"

BEARER=$(aws ssm get-parameter --region "$REGION" --name "$PARAM_NAME" \
         --with-decryption --query 'Parameter.Value' --output text)

python3 - "$BASE_URL" "$BEARER" <<'PYEOF'
import json, sys
config = {'base_url': sys.argv[1], 'token': sys.argv[2]}
with open('.configs/trails.json', 'w') as f:
    json.dump(config, f, indent=2)
    f.write('\n')
PYEOF

echo "wrote $CONFIG_PATH ($BASE_URL)"
