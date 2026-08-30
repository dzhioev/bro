#!/usr/bin/env -S bash -e
source "$(bro-shell-dir)/prelude.sh"

if [ "$#" -lt 3 ] || [ "$#" -gt 4 ]; then
  echo "usage: monitor_ecs.sh <region> <cluster> <service> [<old-deployment-id>]" >&2
  exit 2
fi

REGION="$1"
CLUSTER="$2"
SERVICE="$3"
OLD_DEPLOYMENT_ID="${4:-}"
POLL_INTERVAL="${POLL_INTERVAL:-5}"

get_json() {
  aws ecs describe-services \
    --cluster "$CLUSTER" \
    --services "$SERVICE" \
    --region "$REGION" \
    --query 'services[0].{deployments:deployments[*].{id:id,status:status,desired:desiredCount,running:runningCount,pending:pendingCount,failed:failedTasks,rollout:rolloutState},events:events[:3].{at:createdAt,msg:message}}' \
    --output json 2>&1
}

summarize() {
  python3 -c '
import json
import sys

service = json.loads(sys.stdin.read())
key_parts = []
for deployment in service["deployments"]:
  key_parts.append(
    f"{deployment['"'"'status'"'"']} rollout={deployment['"'"'rollout'"'"']} "
    f"running={deployment['"'"'running'"'"']}/{deployment['"'"'desired'"'"']} "
    f"failed={deployment['"'"'failed'"'"']}"
  )
key = " | ".join(key_parts)
event = service["events"][0]["msg"] if service["events"] else "no events"
print(key)
print(f"{key} | event: {event}")
' <<<"$1"
}

is_deploy_started() {
  OLD_DEPLOYMENT_ID="$OLD_DEPLOYMENT_ID" python3 -c '
import json
import os
import sys

service = json.loads(sys.stdin.read())
deployments = service["deployments"]
old_deployment_id = os.environ["OLD_DEPLOYMENT_ID"]
if old_deployment_id != "":
  if deployments[0]["id"] != old_deployment_id:
    sys.exit(0)
elif any(deployment["rollout"] in {"IN_PROGRESS", "FAILED"} for deployment in deployments):
  sys.exit(0)
elif len(deployments) == 1 and deployments[0]["rollout"] == "COMPLETED":
  # nothing is rolling out and nothing is left over: the deploy this run was meant
  # to watch already finished, so hand the settled service to the terminal check
  # rather than waiting for a rollout that will never start again.
  sys.exit(0)
sys.exit(1)
' <<<"$1"
}

check_terminal() {
  SAW_RUNNING="$saw_running" python3 -c '
import json
import os
import sys

service = json.loads(sys.stdin.read())
deployments = service["deployments"]
primary = next(deployment for deployment in deployments if deployment["status"] == "PRIMARY")
saw_running = os.environ["SAW_RUNNING"] == "1"

if any(deployment["rollout"] == "FAILED" for deployment in deployments):
  print("FAILED")
elif saw_running and primary["running"] == 0 and primary["rollout"] == "IN_PROGRESS":
  print("CRASH")
elif primary["failed"] > 0:
  print("CRASH")
elif len(deployments) == 1 and deployments[0]["rollout"] == "COMPLETED":
  print("DONE")
elif primary["running"] > 0:
  print("RUNNING")
else:
  print("WAITING")
' <<<"$1"
}

echo "waiting for deployment to start"

while true; do
  json="$(get_json)" || {
    echo "aws error: $json" >&2
    sleep "$POLL_INTERVAL"
    continue
  }
  if is_deploy_started "$json"; then
    break
  fi
  sleep "$POLL_INTERVAL"
done

previous_key=""
saw_running=0

while true; do
  json="$(get_json)" || {
    echo "aws error: $json" >&2
    sleep "$POLL_INTERVAL"
    continue
  }

  output="$(summarize "$json")"
  key="$(head -1 <<<"$output")"
  display="$(tail -1 <<<"$output")"
  if [ "$key" != "$previous_key" ]; then
    echo "$display"
    previous_key="$key"
  fi

  state="$(check_terminal "$json")"
  if [ "$state" = "RUNNING" ]; then
    saw_running=1
  elif [ "$state" = "DONE" ]; then
    echo "deploy succeeded"
    break
  elif [ "$state" = "FAILED" ] || [ "$state" = "CRASH" ]; then
    echo "deploy failed: container crashed or the deployment rolled back" >&2
    exit 1
  fi

  sleep "$POLL_INTERVAL"
done
