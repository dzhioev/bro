import logging
import os
import sys

# the detail tier between DEBUG and INFO
VERBOSE = 15
logging.addLevelName(VERBOSE, 'VERBOSE')

# carries the level across process boundaries: set_level exports it, and both
# this module (at import) and setup/log.sh read it, so child processes inherit
# the verbosity of the CLI that spawned them
LEVEL_ENV = 'BRO_LOG_LEVEL'

# the CLI-facing level vocabulary (`--log` choices)
LEVEL_NAMES = ('debug', 'verbose', 'info', 'warning', 'error')


def level_number(name: str) -> int:
  """map a level name (case-insensitive) to its numeric value."""
  level = logging.getLevelNamesMapping().get(name.upper())
  if level is None:
    raise ValueError(f'unknown log level: {name!r}')
  return level


def _initial_level() -> int:
  name = os.environ.get(LEVEL_ENV)
  if name is None:
    return logging.INFO
  return level_number(name)


class _DynamicStderrHandler(logging.StreamHandler):
  """a StreamHandler resolving sys.stderr at emit time, like print() does — a
  stream captured at import would pin records to a redirected/replaced stderr
  (pytest capture, fd redirects) for the process's whole lifetime."""

  @property
  def stream(self):
    return sys.stderr

  @stream.setter
  def stream(self, value):
    pass


_logger = logging.getLogger('bro')
_logger.setLevel(_initial_level())
_handler = _DynamicStderrHandler()
_handler.setFormatter(
  logging.Formatter(
    fmt='%(asctime)s %(levelname)s[%(scope)s] %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S',
  )
)
_logger.addHandler(_handler)
_logger.propagate = False


def _caller_scope():
  frame = sys._getframe(2)
  module = frame.f_globals.get('__name__', '')
  if module == '__main__':
    filepath = frame.f_globals.get('__file__')
    return os.path.splitext(os.path.basename(filepath))[0] if filepath is not None else 'main'
  return module


def debug(msg, *args, **kwargs):
  kwargs.setdefault('extra', {})['scope'] = _caller_scope()
  _logger.debug(msg, *args, **kwargs)


def verbose(msg, *args, **kwargs):
  kwargs.setdefault('extra', {})['scope'] = _caller_scope()
  _logger.log(VERBOSE, msg, *args, **kwargs)


def info(msg, *args, **kwargs):
  kwargs.setdefault('extra', {})['scope'] = _caller_scope()
  _logger.info(msg, *args, **kwargs)


def warning(msg, *args, **kwargs):
  kwargs.setdefault('extra', {})['scope'] = _caller_scope()
  _logger.warning(msg, *args, **kwargs)


def error(msg, *args, **kwargs):
  kwargs.setdefault('extra', {})['scope'] = _caller_scope()
  _logger.error(msg, *args, **kwargs)


def exception(msg, *args, **kwargs):
  kwargs.setdefault('extra', {})['scope'] = _caller_scope()
  _logger.exception(msg, *args, **kwargs)


def set_level(level: int = logging.INFO) -> None:
  """set this process's level and export it (LEVEL_ENV) to child processes."""
  _logger.setLevel(level)
  os.environ[LEVEL_ENV] = logging.getLevelName(level)


def verbose_enabled() -> bool:
  return _logger.isEnabledFor(VERBOSE)
