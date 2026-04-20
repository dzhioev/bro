#!/usr/bin/env -S bash -e

# seed per-worktree ~/.claude/ from host on first run; skip sensitive transcript
# data so prior sessions from other repos don't leak into this container
if [ ! -f "$HOME/.claude/settings.json" ] && [ -d /host-claude ]; then
  echo 'seeding ~/.claude from host' >&2
  find /host-claude -mindepth 1 -maxdepth 1 \
    ! -name sessions ! -name projects ! -name history.jsonl ! -name cw-sessions \
    -exec cp -rn {} "$HOME/.claude/" \;
fi

if [ ! -x "$UV_PROJECT_ENVIRONMENT/bin/python" ]; then
  echo 'provisioning linux venv' >&2
  uv sync --all-groups >&2
fi

exec "$@"
