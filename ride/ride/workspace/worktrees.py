import os
import subprocess
from pathlib import Path

from bro.base import log


def provision_workspace(tree: Path) -> bool:
  script = tree / 'setup.sh'
  if not script.is_file():
    log.info('%s not found; skipping project provisioning', script)
    return True
  env = {key: value for key, value in os.environ.items() if key != 'RIDE_VENV_MANIFEST'}
  if subprocess.run([str(script)], cwd=str(tree), env=env).returncode != 0:
    log.error('failed to provision workspace %s', tree)
    return False
  return True
