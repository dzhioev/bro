#!/usr/bin/env -S bash -e
source "$(bro-shell-dir)/prelude.sh"

DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
source "$DIR/deployment_config.sh"
load_trails_deployment_config

put_parameter() {
  aws ssm put-parameter --region "$TRAILS_REGION" --type SecureString --overwrite \
    --name "$1" --value "$2" >/dev/null
  log INFO "wrote $1"
}

fetch_parameter() {
  aws ssm get-parameter --region "$TRAILS_REGION" --name "$1" \
    --with-decryption --query 'Parameter.Value' --output text 2>/dev/null
}

if tokens="$(fetch_parameter /trails/tokens)"; then
  log INFO 'reusing existing /trails/tokens'
else
  if sessions_token="$(fetch_parameter /trails/bearer-token)"; then
    log INFO 'seeding the sessions token from /trails/bearer-token'
  else
    sessions_token="$(openssl rand -hex 32)"
    log INFO 'generated a sessions token'
  fi
  analyst_token="$(openssl rand -hex 32)"
  admin_token="$(openssl rand -hex 32)"
  tokens="$(cat <<JSON
{
  "tokens": {
    "sessions": {"token": "$sessions_token", "permissions": ["write"]},
    "analyst": {"token": "$analyst_token", "permissions": ["read", "write"]},
    "admin": {"token": "$admin_token", "permissions": ["read", "write", "admin"]}
  }
}
JSON
)"
  jq -n \
    --arg sessions "$sessions_token" \
    --arg analyst "$analyst_token" \
    --arg admin "$admin_token" \
    '{sessions: $sessions, analyst: $analyst, admin: $admin}'
fi

put_parameter /trails/tokens "$tokens"
