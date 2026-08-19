#!/usr/bin/env -S bash -e
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../prelude.sh"
DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
VERSION=$(npm view @anthropic-ai/claude-code version)
echo "$VERSION" > "$DIR/claude-code-version"
echo "claude-code pinned to $VERSION" >&2
