#!/usr/bin/env bash
# sourceable helpers for service deployment scripts; callers provide deployment config.

_DEPLOY_LIB_DIRECTORY="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"

BRO_SERVER_BASE_IMAGE="${BRO_SERVER_BASE_IMAGE:-bro-server-base}"
_BRO_WHEEL_CONTEXT_DIRECTORY=build/bro-wheel

_account_id() {
  aws sts get-caller-identity --query Account --output text
}

_framework_checkout() {
  local repository_root="$1"
  python3 - "$repository_root" <<'PY'
import sys
import tomllib
from pathlib import Path

root = Path(sys.argv[1])
metadata = tomllib.loads((root / 'pyproject.toml').read_text())
is_framework = metadata.get('project', {}).get('name') == 'bro' and (root / 'bro').is_dir()
sys.exit(0 if is_framework else 1)
PY
}

_locked_bro_source() {
  local repository_root="$1"
  python3 - "$repository_root/uv.lock" <<'PY'
import sys
import tomllib
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

lock_file = Path(sys.argv[1])
metadata = tomllib.loads(lock_file.read_text())
packages = [package for package in metadata['package'] if package['name'] == 'bro']
if len(packages) != 1:
  raise SystemExit('error: expected uv.lock to contain one bro package')
source = packages[0].get('source', {}).get('git')
if not isinstance(source, str):
  raise SystemExit('error: expected uv.lock to resolve bro to a git source')
parsed = urlsplit(source)
if parsed.fragment == '':
  raise SystemExit('error: the bro git source in uv.lock has no pinned revision')
print(urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', '')))
print(parsed.fragment)
PY
}

ecr_uri() {
  if [ "$#" -ne 2 ]; then
    echo "usage: ecr_uri <repository> <region>" >&2
    return 2
  fi
  local repository="$1" region="$2"
  echo "$(_account_id).dkr.ecr.${region}.amazonaws.com/${repository}"
}

ecr_login() {
  if [ "$#" -ne 1 ]; then
    echo "usage: ecr_login <region>" >&2
    return 2
  fi
  local region="$1" registry
  registry="$(_account_id).dkr.ecr.${region}.amazonaws.com"
  aws ecr get-login-password --region "$region" \
    | docker login --username AWS --password-stdin "$registry"
}

stage_bro_wheel() {
  local repository_root output_directory checkout source_data url revision description
  local -a source_parts wheel_files
  repository_root="$(git rev-parse --show-toplevel)"
  output_directory="${repository_root}/${_BRO_WHEEL_CONTEXT_DIRECTORY}"
  mkdir -p "$output_directory"

  if _framework_checkout "$repository_root"; then
    (
      cd "$repository_root"
      uv build --package bro --wheel --out-dir "$output_directory" --clear --no-build-logs \
        --no-create-gitignore
    )
    description="the framework working tree"
  else
    source_data="$(_locked_bro_source "$repository_root")" || return
    mapfile -t source_parts <<<"$source_data"
    url="${source_parts[0]}"
    revision="${source_parts[1]}"
    checkout="$(mktemp -d)"
    (
      trap 'rm -rf "$checkout"' EXIT
      git clone --quiet --filter=blob:none "$url" "$checkout"
      git -C "$checkout" checkout --quiet --detach "$revision"
      cd "$checkout"
      uv build --package bro --wheel --out-dir "$output_directory" --clear --no-build-logs \
        --no-create-gitignore
    )
    description="${url}@${revision}"
  fi

  mapfile -t wheel_files < <(find "$output_directory" -maxdepth 1 -type f -name 'bro-*.whl')
  if [ "${#wheel_files[@]}" -ne 1 ]; then
    echo "error: expected one bro wheel in $output_directory, found ${#wheel_files[@]}" >&2
    return 1
  fi
  echo "staged ${wheel_files[0]} from ${description}"
}

ensure_server_base() {
  if [ "$#" -lt 1 ]; then
    echo "usage: ensure_server_base <dockerfile> [buildx-argument ...]" >&2
    return 2
  fi
  local dockerfile="$1"
  shift
  if ! grep -Eq "^FROM ${BRO_SERVER_BASE_IMAGE}"'( |$)' "$dockerfile"; then
    return 0
  fi
  docker buildx build --provenance=false --load -t "$BRO_SERVER_BASE_IMAGE" "$@" \
    "$_DEPLOY_LIB_DIRECTORY/server_base"
}

prepare_image_build() {
  if [ "$#" -lt 1 ]; then
    echo "usage: prepare_image_build <dockerfile> [buildx-argument ...]" >&2
    return 2
  fi
  local dockerfile="$1"
  shift
  stage_bro_wheel
  ensure_server_base "$dockerfile" "$@"
}

build_and_push() {
  if [ "$#" -lt 3 ]; then
    echo "usage: build_and_push <tag> <dockerfile> [buildx-argument ...] <context>" >&2
    return 2
  fi
  local image_tag="$1" dockerfile="$2"
  shift 2
  prepare_image_build "$dockerfile" --platform linux/amd64
  docker buildx build --platform linux/amd64 --provenance=false \
    -f "$dockerfile" -t "$image_tag" --push "$@"
}

build_image() {
  if [ "$#" -lt 3 ]; then
    echo "usage: build_image <tag> <dockerfile> [buildx-argument ...] <context>" >&2
    return 2
  fi
  local image_tag="$1" dockerfile="$2"
  shift 2
  prepare_image_build "$dockerfile" --platform linux/amd64
  docker buildx build --platform linux/amd64 --provenance=false --load \
    -f "$dockerfile" -t "$image_tag" "$@"
}

trigger_image_build() {
  if [ "$#" -ne 4 ]; then
    echo "usage: trigger_image_build <target> <repository> <project> <region>" >&2
    return 2
  fi
  local target="$1" repository="$2" project="$3" region="$4"
  local commit image_digest pushed tip build_id stream status token events
  local -a token_arguments
  commit="$(git rev-parse HEAD)"
  image_digest="$(aws ecr list-images \
    --region "$region" \
    --repository-name "$repository" \
    --filter tagStatus=TAGGED \
    --query "imageIds[?imageTag=='${commit}'].imageDigest | [0]" \
    --output text)"
  case "$image_digest" in
    None) ;;
    sha256:*)
      echo "using existing image for target ${target}, commit ${commit}: ${image_digest}"
      return 0
      ;;
    *)
      echo "error: unexpected ECR digest for target ${target}, commit ${commit}: ${image_digest}" >&2
      return 1
      ;;
  esac

  git fetch origin --quiet
  pushed=false
  while read -r tip; do
    if git merge-base --is-ancestor "$commit" "$tip"; then
      pushed=true
      break
    fi
  done < <(git for-each-ref --format='%(objectname)' refs/remotes/origin)
  if [ "$pushed" != "true" ]; then
    echo "error: HEAD ${commit} is not reachable from any origin branch; push it first" >&2
    return 1
  fi

  build_id="$(aws codebuild start-build \
    --region "$region" \
    --project-name "$project" \
    --source-version "$commit" \
    --environment-variables-override "name=TARGET,value=${target},type=PLAINTEXT" \
    --query build.id --output text)"
  echo "started build ${build_id} (target ${target}, commit ${commit})"

  status=IN_PROGRESS
  token=""
  while true; do
    read -r status stream <<<"$(aws codebuild batch-get-builds \
      --region "$region" \
      --ids "$build_id" \
      --query 'builds[0].[buildStatus,logs.streamName]' --output text)"
    if [ "$status" != "IN_PROGRESS" ]; then
      sleep 5
    fi
    if [ "$stream" != "None" ]; then
      token_arguments=()
      if [ -n "$token" ]; then
        token_arguments=(--next-token "$token")
      fi
      events="$(aws logs get-log-events \
        --region "$region" \
        --log-group-name "/aws/codebuild/${project}" \
        --log-stream-name "$stream" \
        --start-from-head \
        "${token_arguments[@]}" \
        --output json)"
      jq -j '.events[].message' <<<"$events"
      token="$(jq -r '.nextForwardToken' <<<"$events")"
    fi
    if [ "$status" != "IN_PROGRESS" ]; then
      break
    fi
    sleep 10
  done
  echo "build ${build_id}: ${status}"
  [ "$status" = "SUCCEEDED" ]
}

cdk_deploy() {
  if [ "$#" -lt 2 ]; then
    echo "usage: cdk_deploy <cdk-directory> <stack> [cdk-argument ...]" >&2
    return 2
  fi
  local cdk_directory="$1"
  shift
  (
    cd "$cdk_directory"
    CDK_CLI_TELEMETRY_OPTOUT=1 npx --yes cdk deploy "$@" --require-approval never
  )
}
