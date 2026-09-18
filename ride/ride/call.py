"""Console-script alias for ``ride along``."""

from typing import Optional

from ride.cli import alias_main, reports_runtime_errors

__cli_name__ = 'call'


@reports_runtime_errors
def main(argv: list[str]) -> Optional[int]:
  return alias_main(argv, solo=False)
