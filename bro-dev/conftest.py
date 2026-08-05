"""keep bro-dev tests independent of the launching session's process environment."""

import os

for variable in (
  'BROKER_CHANNEL',
  'BRO_HOLD',
  'BRO_LOG_LEVEL',
  'CREDENTIALS_REGISTRY',
  'CW_RUNNER_PID',
  'BRO_USAGE_FILE',
):
  os.environ.pop(variable, None)
