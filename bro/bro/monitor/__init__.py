"""Session-local monitoring paths and signals shared across package boundaries."""

import os
from pathlib import Path


def claude_config_dir() -> Path:
  """the active Claude config root, defaulting to the user-level directory."""
  override = os.environ.get('CLAUDE_CONFIG_DIR')
  return Path(override) if override is not None else Path.home() / '.claude'
