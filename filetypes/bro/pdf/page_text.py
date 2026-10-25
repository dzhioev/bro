"""Assemble PDFium's per-character list into sanitized page text."""

from collections.abc import Iterable
from dataclasses import dataclass

from bro.files.text import sanitize


@dataclass(frozen=True)
class Character:
  code: int
  generated: bool = False
  hyphen: bool = False


def assemble(characters: Iterable[Character]) -> str:
  output: list[str] = []
  high_surrogate: int | None = None
  for character in characters:
    code = character.code
    if character.generated and code == 13:
      continue
    if character.hyphen and code == 2:
      code = ord('-')
    if high_surrogate is not None:
      if 0xDC00 <= code <= 0xDFFF:
        output.append(chr(0x10000 + ((high_surrogate - 0xD800) << 10) + code - 0xDC00))
        high_surrogate = None
        continue
      output.append('\ufffd')
      high_surrogate = None
    if 0xD800 <= code <= 0xDBFF:
      high_surrogate = code
    elif (
      0xDC00 <= code <= 0xDFFF
      or code > 0x10FFFF
      or code == 0xFFFE
      or (code < 32 and code not in (9, 10))
      or 127 <= code <= 159
    ):
      output.append('\ufffd')
    else:
      output.append(chr(code))
  if high_surrogate is not None:
    output.append('\ufffd')
  return sanitize(''.join(output)).text
