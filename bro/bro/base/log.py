import logging
import sys

_logger = logging.getLogger('ppp')
_logger.setLevel(logging.INFO)
_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(
  logging.Formatter(
    fmt='%(asctime)s %(levelname)s[%(name)s] %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S',
  )
)
_logger.addHandler(_handler)
_logger.propagate = False

debug = _logger.debug
info = _logger.info
warning = _logger.warning
error = _logger.error
exception = _logger.exception


def set_level(level: int = logging.INFO) -> None:
  _logger.setLevel(level)
