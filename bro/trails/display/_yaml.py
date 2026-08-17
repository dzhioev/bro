"""YAML rendering of the JSON value model.

Block form carries a string's line breaks as a literal scalar, so text holding code, a
shell command or a document keeps its own lines. Every rendering parses back as the value
it was given: a string no plain or literal form reproduces exactly is double-quoted.
"""

import json
import math
import re
from typing import Any

_INDENT = 2

# what a YAML 1.1 or 1.2 parser resolves to something other than a string
_RESOLVED_AS_NON_STRING = re.compile(
  r"""^(?:
    [~=]|<<
    |(?:null|Null|NULL|true|True|TRUE|false|False|FALSE|yes|Yes|YES|no|No|NO
       |on|On|ON|off|Off|OFF|y|Y|n|N)
    |[-+]?(?:0b[01_]+|0o?[0-7_]*|0x[0-9a-fA-F_]+|[0-9][0-9_]*(?::[0-5]?[0-9])*)
    |[-+]?(?:[0-9][0-9_]*)?\.[0-9_]*(?:[eE][-+]?[0-9]+)?
    |[-+]?[0-9][0-9_]*(?:\.[0-9_]*)?[eE][-+]?[0-9]+
    |[-+]?\.(?:inf|Inf|INF|nan|NaN|NAN)
    |[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}(?:[Tt ].*)?
  )$""",
  re.VERBOSE,
)
_PLAIN_FORBIDDEN = re.compile(r'[\x00-\x1f\x7f]')
# a literal block carries tabs and every printable byte, control characters excepted
_LITERAL_FORBIDDEN = re.compile(r'[\x00-\x08\x0b-\x1f\x7f]')
_INDICATORS = frozenset("""-?:,[]{}#&*!|>'"%@`""")


def flow(value: Any) -> str:
  """One-line YAML: flow collections, and quotes around any string with a line break."""
  if isinstance(value, dict):
    entries = (f'{_string(_key(name), in_flow=True)}: {flow(item)}' for name, item in value.items())
    return '{' + ', '.join(entries) + '}'
  if isinstance(value, list):
    return '[' + ', '.join(flow(item) for item in value) + ']'
  return _scalar(value, in_flow=True)


def block(value: Any) -> str:
  """Multi-line YAML: one entry per line, strings with line breaks as literal scalars.

  Ends with a line break, which a trailing literal block needs to keep its own final one.
  """
  return _block(value, 0) + '\n'


def render(value: Any, *, width: int) -> str:
  """`flow` where it fits `width` and no string carries a line break, `block` otherwise."""
  if not _has_line_break(value):
    line = flow(value)
    if len(line) <= width:
      return line
  return block(value)


def _block(value: Any, indent: int) -> str:
  pad = ' ' * indent
  if isinstance(value, dict) and len(value) > 0:
    return '\n'.join(
      _entry(f'{pad}{_string(_key(name), in_flow=False)}:', item, indent)
      for name, item in value.items()
    )
  if isinstance(value, list) and len(value) > 0:
    return '\n'.join(_sequence_entry(item, indent) for item in value)
  if isinstance(value, str) and _fits_literal(value):
    return pad + _literal(value, indent + _INDENT)
  return pad + _inline(value)


def _entry(prefix: str, item: Any, indent: int) -> str:
  if isinstance(item, str) and _fits_literal(item):
    return f'{prefix} {_literal(item, indent + _INDENT)}'
  if isinstance(item, (dict, list)) and len(item) > 0:
    return f'{prefix}\n{_block(item, indent + _INDENT)}'
  return f'{prefix} {_inline(item)}'


def _inline(value: Any) -> str:
  """The one-line rendering of a value an entry of a block carries."""
  if isinstance(value, (dict, list)):
    return flow(value)
  return _scalar(value, in_flow=False)


def _sequence_entry(item: Any, indent: int) -> str:
  prefix = f'{" " * indent}-'
  if isinstance(item, (dict, list)) and len(item) > 0:
    nested = _block(item, indent + _INDENT)
    return f'{prefix} {nested[indent + _INDENT :]}'
  return _entry(prefix, item, indent)


def _literal(text: str, indent: int) -> str:
  """A literal block scalar: its header, then the content indented to `indent`."""
  pad = ' ' * indent
  trailing = len(text) - len(text.rstrip('\n'))
  body = text[: len(text) - trailing]
  header = '|' if trailing == 1 else '|-' if trailing == 0 else '|+'
  if body.startswith((' ', '\t')):
    # content opening on whitespace needs the indentation spelled out, relative to the
    # entry the block belongs to
    header += str(_INDENT)
  lines = [header]
  lines.extend(f'{pad}{line}' if len(line) > 0 else '' for line in body.split('\n'))
  lines.extend('' for _ in range(max(trailing - 1, 0)))
  return '\n'.join(lines)


def _fits_literal(text: str) -> bool:
  if '\n' not in text or len(text.strip()) == 0:
    return False
  if _LITERAL_FORBIDDEN.search(text) is not None:
    return False
  # a whitespace-only line is indistinguishable from an empty one once indented
  return all(len(line.strip()) > 0 or len(line) == 0 for line in text.split('\n'))


def _scalar(value: Any, *, in_flow: bool) -> str:
  if value is None:
    return 'null'
  if value is True:
    return 'true'
  if value is False:
    return 'false'
  if isinstance(value, str):
    return _string(value, in_flow=in_flow)
  if isinstance(value, int):
    return str(value)
  if isinstance(value, float):
    if not math.isfinite(value):
      raise ValueError(f'display value is not finite: {value!r}')
    rendered = repr(value)
    # exponent form needs its decimal point spelled out to read back as a float
    return rendered if '.' in rendered else rendered.replace('e', '.0e')
  raise TypeError(f'display value has no YAML rendering: {value!r}')


def _string(text: str, *, in_flow: bool) -> str:
  if _fits_plain(text, in_flow=in_flow):
    return text
  # JSON string syntax is YAML's double-quoted style
  return json.dumps(text, ensure_ascii=False)


def _fits_plain(text: str, *, in_flow: bool) -> bool:
  if len(text) == 0 or text != text.strip():
    return False
  if _RESOLVED_AS_NON_STRING.match(text) is not None:
    return False
  if _PLAIN_FORBIDDEN.search(text) is not None:
    return False
  first = text[0]
  # an indicator opens a plain scalar only where nothing can follow it as one: a flow
  # collection reads even `?x` as a key marker, block context only `? x`
  openers = '-' if in_flow else '-?:'
  if first in _INDICATORS and not (first in openers and len(text) > 1 and text[1] != ' '):
    return False
  if ': ' in text or text.endswith(':') or ' #' in text:
    return False
  return not (in_flow and any(character in text for character in ',[]{}:'))


def _key(name: Any) -> str:
  if not isinstance(name, str):
    raise TypeError(f'display mapping key is not a string: {name!r}')
  return name


def _has_line_break(value: Any) -> bool:
  if isinstance(value, dict):
    return any(_has_line_break(item) for item in value.values())
  if isinstance(value, list):
    return any(_has_line_break(item) for item in value)
  return isinstance(value, str) and '\n' in value
