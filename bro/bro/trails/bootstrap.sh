#!/usr/bin/env -S bash -e
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../setup/prelude.sh"
# one-time setup for the trails sink.
#
# reads the bearer token from SSM (/trails/bearer-token), derives the trails
# server base URL from the `infra` secret's delegated_subdomain, and writes
# ~/.ppp/trails.json. Once this file exists, BaseBro defaults to
# the default bro recorder factory and every bro run gets recorded
# end-to-end against the deployed trails-server.
#
# prerequisites:
#   - AWS CLI configured (read-only on /trails/bearer-token is enough)
#   - TrailsServerStack deployed (creates the SSM parameter)
#   - the `infra` secret resolvable (~/.ppp/infra.json)
#
# run once after deploying the trails CDK stack:
#   ./trails/bootstrap.sh
#
# idempotent: if ~/.ppp/trails.json already exists, the script is a no-op.

REGION=eu-central-1
PARAM_NAME=/trails/bearer-token
CONFIG_PATH="$HOME/.ppp/trails.json"

if [ -f "$CONFIG_PATH" ]; then
  echo "$CONFIG_PATH already exists, skipping"
  exit 0
fi

DELEGATED_SUBDOMAIN=$(credentials get infra --field delegated_subdomain)
BASE_URL="https://trails.$DELEGATED_SUBDOMAIN"

BEARER=$(aws ssm get-parameter --region "$REGION" --name "$PARAM_NAME" \
         --with-decryption --query 'Parameter.Value' --output text)

mkdir -p "$(dirname "$CONFIG_PATH")"
python3 - "$CONFIG_PATH" "$BASE_URL" "$BEARER" <<'PYEOF'
import json, sys
config = {'base_url': sys.argv[2], 'token': sys.argv[3]}
with open(sys.argv[1], 'w') as f:
    json.dump(config, f, indent=2)
    f.write('\n')
PYEOF

echo "wrote $CONFIG_PATH ($BASE_URL)"
