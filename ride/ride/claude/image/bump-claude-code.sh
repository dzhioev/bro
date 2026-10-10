#!/usr/bin/env -S bash -e
source "$(bro-shell-dir)/prelude.sh"
VERSION=$(npm view @anthropic-ai/claude-code version)
echo "$VERSION" > "$HERE/claude-code-version"
echo "claude-code pinned to $VERSION" >&2
