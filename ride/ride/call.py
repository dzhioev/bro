"""Console-script alias for ``ride along``."""

import os
from typing import Optional

from ride.cli import alias_main, reports_location_errors

__cli_name__ = 'call'


@reports_location_errors
def main(argv: list[str]) -> Optional[int]:
  os.environ.setdefault('BRO_SHELL_COMMAND', ' '.join(argv))
  return alias_main(argv, solo=False)
