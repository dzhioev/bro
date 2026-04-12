import logging
import os
import sys

_logger = logging.getLogger('ppp')
_logger.setLevel(logging.INFO)
_handler = logging.StreamHandler(sys.stderr)
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
    filepath = frame.f_globals.get('__file__', '')
    return os.path.splitext(os.path.basename(filepath))[0] if filepath else 'main'
  return module


def debug(msg, *args, **kwargs):
  kwargs.setdefault('extra', {})['scope'] = _caller_scope()
  _logger.debug(msg, *args, **kwargs)


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
  _logger.setLevel(level)
