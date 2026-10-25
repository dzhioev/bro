"""Invisible-character removal and JSON reply-string validation."""

import re
from dataclasses import dataclass
from typing import Any

_INVISIBLE = re.compile(
  '[\u00ad\u061c\u180e\u200b\u200e-\u200f\u202a-\u202e'
  '\u2060-\u2064\u2066-\u206f\ufeff\ufff9-\ufffb\U000e0000-\U000e007f]'
)
_TAG_RUN = re.compile('[\U000e0020-\U000e007e]+')
_INVALID = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]')


@dataclass(frozen=True)
class TagPayload:
  start: int
  end: int
  text: str


@dataclass(frozen=True)
class SanitizedText:
  text: str
  tag_payloads: tuple[TagPayload, ...]


def sanitize(text: str) -> SanitizedText:
  """Return stripped text and printable tag runs at offsets in the input."""
  payloads: list[TagPayload] = []
  for match in _TAG_RUN.finditer(text):
    decoded = ''.join(chr(ord(character) - 0xE0000) for character in match[0])
    if decoded.strip(' '):
      payloads.append(TagPayload(match.start(), match.end(), decoded))
  return SanitizedText(_INVISIBLE.sub('', text), tuple(payloads))


def sanitize_strings(value: Any) -> Any:
  """Strip invisible characters throughout a JSON-ready value."""
  if isinstance(value, str):
    return sanitize(value).text
  if isinstance(value, list):
    return [sanitize_strings(item) for item in value]
  if isinstance(value, dict):
    result = {}
    for key, item in value.items():
      if not isinstance(key, str):
        raise ValueError('reply object keys must be strings')
      cleaned = sanitize(key).text
      if cleaned in result:
        raise ValueError('reply object keys collide after sanitization')
      result[cleaned] = sanitize_strings(item)
    return result
  return value


def validate_strings(value: Any) -> None:
  """Refuse lone surrogates and controls other than TAB, LF and CR."""
  if isinstance(value, str):
    if _INVALID.search(value):
      raise ValueError('invalid reply string: lone surrogate or control character')
  elif isinstance(value, list):
    for item in value:
      validate_strings(item)
  elif isinstance(value, dict):
    for key, item in value.items():
      if not isinstance(key, str):
        raise ValueError('reply object keys must be strings')
      validate_strings(key)
      validate_strings(item)
