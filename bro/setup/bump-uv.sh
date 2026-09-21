#!/usr/bin/env -S bash -e
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/prelude.sh"
set -o pipefail

VERSION="$(
  curl -LsSf https://pypi.org/pypi/uv/json \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["info"]["version"])'
)"
printf '%s\n' "$VERSION" > "$HERE/uv-version"
echo "uv pinned to $VERSION" >&2
