"""ANSI color helpers shared by the terminal-rendering CLIs."""

import os
import sys
from typing import TextIO


class Colors:
  """ANSI escape accessors that collapse to empty strings when disabled, so
  rendering code interpolates them unconditionally."""

  def __init__(self, enabled: bool) -> None:
    self.enabled = enabled

  def _code(self, code: str) -> str:
    return f'\033[{code}m' if self.enabled else ''

  @property
  def reset(self) -> str:
    return self._code('0')

  @property
  def bold(self) -> str:
    return self._code('1')

  @property
  def dim(self) -> str:
    return self._code('2')

  @property
  def red(self) -> str:
    return self._code('31')

  @property
  def green(self) -> str:
    return self._code('32')

  @property
  def yellow(self) -> str:
    return self._code('33')

  @property
  def blue(self) -> str:
    return self._code('34')

  @property
  def magenta(self) -> str:
    return self._code('35')

  @property
  def cyan(self) -> str:
    return self._code('36')


def should_color(mode: str, stream: TextIO = sys.stdout) -> bool:
  """resolve a `--color auto|always|never` flag: auto means the stream is a
  TTY and `NO_COLOR` is unset."""
  if mode == 'always':
    return True
  if mode == 'never':
    return False
  if os.environ.get('NO_COLOR') is not None:
    return False
  return stream.isatty()
