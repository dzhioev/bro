"""Assemble PDFium's per-character list into sanitized page text."""

from collections.abc import Iterable
from typing import NamedTuple

from bro.files.text import sanitize
from bro.pdf import HiddenReason, HiddenText, HiddenTextMore, TextNote, UnmappedCharacters

HIDDEN_EXCERPT_CHARACTERS = 120
HIDDEN_NOTES = 5
_REASON_ORDER: tuple[HiddenReason, ...] = ('invisible', 'outside_page', 'tiny', 'white', 'layer')


class Character(NamedTuple):
  code: int
  generated: bool = False
  hyphen: bool = False
  reason: HiddenReason | None = None
  unmapped: bool = False
  box: tuple[float, float, float, float] | None = None


def decoded(characters: Iterable[Character]) -> Iterable[tuple[int, str]]:
  high_surrogate: tuple[int, int] | None = None
  for index, character in enumerate(characters):
    code = character.code
    if character.generated and code == 13:
      continue
    if character.hyphen and code == 2:
      code = ord('-')
    if high_surrogate is not None:
      if 0xDC00 <= code <= 0xDFFF:
        yield high_surrogate[0], chr(0x10000 + ((high_surrogate[1] - 0xD800) << 10) + code - 0xDC00)
        high_surrogate = None
        continue
      yield high_surrogate[0], '\ufffd'
      high_surrogate = None
    if 0xD800 <= code <= 0xDBFF:
      high_surrogate = (index, code)
    elif (
      0xDC00 <= code <= 0xDFFF
      or code > 0x10FFFF
      or code == 0xFFFE
      or (code < 32 and code not in (9, 10))
      or 127 <= code <= 159
    ):
      yield index, '\ufffd'
    else:
      yield index, chr(code)
  if high_surrogate is not None:
    yield high_surrogate[0], '\ufffd'


def assemble(characters: Iterable[Character]) -> str:
  return sanitize(''.join(text for _, text in decoded(characters))).text


def text_and_notes(
  characters: list[Character],
) -> tuple[str, list[TextNote]]:
  runs: list[tuple[int, HiddenReason, str]] = []
  start: int | None = None
  end = 0
  reason: HiddenReason | None = None
  for index, character in enumerate(characters):
    if character.generated:
      continue
    if character.reason is not None:
      if start is None:
        start = index
        reason = character.reason
      elif reason is not None and _REASON_ORDER.index(character.reason) < _REASON_ORDER.index(
        reason
      ):
        reason = character.reason
      end = index + 1
    elif start is not None:
      assert reason is not None
      runs.append((start, reason, assemble(characters[start:end])))
      start = None
  if start is not None:
    assert reason is not None
    runs.append((start, reason, assemble(characters[start:end])))
  units = list(decoded(characters))
  sanitized = sanitize(''.join(text for _, text in units))
  for payload in sanitized.tag_payloads:
    runs.append((units[payload.start][0], 'tag_characters', payload.text))
  runs.sort(key=lambda run: run[0])
  notes: list[TextNote] = [
    HiddenText(reason=reason, text=' '.join(text.split())[:HIDDEN_EXCERPT_CHARACTERS])
    for _, reason, text in runs[:HIDDEN_NOTES]
  ]
  if len(runs) > HIDDEN_NOTES:
    notes.append(HiddenTextMore(run_count=len(runs) - HIDDEN_NOTES))
  unmapped = sum(character.unmapped for character in characters)
  if unmapped:
    notes.append(UnmappedCharacters(character_count=unmapped))
  return sanitized.text, notes
