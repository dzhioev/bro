"""Environment names shared by broker launchers and clients."""

from collections.abc import Mapping
from pathlib import Path

CHANNEL_ENV = 'BROKER_CHANNEL'
UPSTREAM_ENV = 'BROKER_UPSTREAM'
BROXY_LOG_NAME = 'broxy.log'


def broxy_log_path(environment: Mapping[str, str]) -> Path:
  return Path(environment.get('RIDE_SESSION_DIR', '/tmp')) / BROXY_LOG_NAME
