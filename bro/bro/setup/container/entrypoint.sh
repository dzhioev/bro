#!/usr/bin/env -S bash -e

if [ ! -x "$UV_PROJECT_ENVIRONMENT/bin/python" ]; then
  echo 'provisioning linux venv' >&2
  uv sync --all-groups >&2
fi

exec "$@"
