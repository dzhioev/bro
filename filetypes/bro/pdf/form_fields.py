"""Read widget values and nearby printed labels."""

import ctypes
from collections.abc import Iterator
from typing import Any

import pypdfium2_raw as raw

from bro.pdf import FieldKind, FieldOption, FormField
from bro.pdf.engine import Document, TextPage
from bro.pdf.page_text import Character, assemble
from bro.pdf.view_limits import NAME_CHARACTERS, ViewBudget

LEFT_TOLERANCE_POINTS = 2
LEFT_GAP_POINTS = 12
ABOVE_DISTANCE_POINTS = 28
CHARACTER_OVERLAP_FRACTION = 0.5
NEARBY_TEXT_CHARACTERS = 80
_FIELD_KINDS: dict[int, FieldKind] = {
  1: 'pushbutton',
  2: 'checkbox',
  3: 'radio',
  4: 'combobox',
  5: 'listbox',
  6: 'text',
  7: 'signature',
}


def nearby_text(
  characters: list[Character], rectangle: tuple[float, float, float, float]
) -> str | None:
  left, right = sorted(rectangle[:2])
  bottom, top = sorted(rectangle[2:])
  visible = {
    index: (*sorted(character.box[:2]), *sorted(character.box[2:]))
    for index, character in enumerate(characters)
    if not character.generated and character.reason is None and character.box is not None
  }
  candidates = sorted(
    (
      index
      for index, (
        character_left,
        character_right,
        character_bottom,
        character_top,
      ) in visible.items()
      if min(top, character_top) - max(bottom, character_bottom)
      >= (character_top - character_bottom) * CHARACTER_OVERLAP_FRACTION
      and character_right <= left + LEFT_TOLERANCE_POINTS
    ),
    key=lambda index: (-visible[index][1], index),
  )
  selected = []
  if candidates:
    first, *remaining = candidates
    selected.append(first)
    run_left = visible[first][0]
    for index in remaining:
      if run_left - visible[index][1] >= LEFT_GAP_POINTS:
        break
      selected.append(index)
      run_left = min(run_left, visible[index][0])
  else:
    candidates = [
      index
      for index, (
        character_left,
        character_right,
        character_bottom,
        character_top,
      ) in visible.items()
      if min(right, character_right) > max(left, character_left)
      and 0 <= character_bottom - top <= ABOVE_DISTANCE_POINTS
    ]
    if candidates:
      lowest = min(visible[index][2] for index in candidates)
      selected = [
        index
        for index in candidates
        if visible[index][2] - lowest
        <= (visible[index][3] - visible[index][2]) * CHARACTER_OVERLAP_FRACTION
      ]
  if not selected:
    return None
  run = []
  previous = None
  for index in sorted(selected):
    if previous is not None and any(
      character.generated and character.code in (32, 10, 13)
      for character in characters[previous + 1 : index]
    ):
      run.append(Character(32, generated=True))
    run.append(characters[index])
    previous = index
  text = ' '.join(assemble(run).split())[:NEARBY_TEXT_CHARACTERS]
  return text or None


def page_fields(page: TextPage, environment: int, number: int) -> Iterator[FormField]:
  engine = page.engine
  count = engine.annotation_count(page.handle)
  if count < 0:
    raise RuntimeError('PDFium failed to count annotations')
  bounds = raw.FS_RECTF()
  if not engine.page_box(page.handle, ctypes.byref(bounds)):
    raise RuntimeError('PDFium failed to read a page box')
  characters: list[Character] | None = None
  for index in range(count):
    with page.annotation(index) as annotation:
      if engine.annotation_subtype(annotation) != raw.FPDF_ANNOT_WIDGET:
        continue
      if characters is None:
        characters = page.classified_characters()
      rectangle = raw.FS_RECTF()
      if not engine.annotation_rectangle(annotation, ctypes.byref(rectangle)):
        raise RuntimeError('PDFium failed to read a widget rectangle')
      left, right = sorted((rectangle.left, rectangle.right))
      bottom, top = sorted((rectangle.bottom, rectangle.top))
      flags = engine.annotation_flags(annotation)
      field_flags = engine.field_flags(environment, annotation)
      kind = _FIELD_KINDS.get(engine.field_type(environment, annotation), 'unknown')
      options = None
      if kind in ('combobox', 'listbox'):
        option_count = engine.option_count(environment, annotation)
        if option_count < 0:
          raise RuntimeError('PDFium failed to count field options')
        options = [
          FieldOption(
            label=engine.wide_string(engine.option_label, environment, annotation, option),
            selected=bool(engine.option_selected(environment, annotation, option)),
          )
          for option in range(option_count)
        ]
      field = FormField(
        page_number=number,
        name=engine.wide_string(engine.field_name, environment, annotation)[:NAME_CHARACTERS],
        kind=kind,
        value=(
          None
          if kind in ('pushbutton', 'signature')
          else engine.wide_string(engine.field_value, environment, annotation)
        ),
        checked=(
          bool(engine.field_checked(environment, annotation))
          if kind in ('checkbox', 'radio')
          else None
        ),
        options=options,
        hidden=bool(
          flags & (raw.FPDF_ANNOT_FLAG_HIDDEN | raw.FPDF_ANNOT_FLAG_NOVIEW)
          or left >= right
          or bottom >= top
          or right < bounds.left
          or left > bounds.right
          or top < bounds.bottom
          or bottom > bounds.top
        ),
        read_only=bool(field_flags & raw.FPDF_FORMFLAG_READONLY),
        nearby_text=nearby_text(characters, (left, right, bottom, top)),
      )
    yield field


def fields_view(document: Document, selected: list[int], budget: ViewBudget) -> dict[str, Any]:
  fields = []
  if document.engine.form_type(document.handle) == raw.FORMTYPE_NONE:
    return {'fields': fields}
  with document.form_environment() as environment:
    for number in selected:
      with document.form_page(number - 1, environment) as page:
        if page is None:
          continue
        for field in page_fields(page, environment, number):
          value = field.model_dump()
          budget.add(value, location=f'field {len(fields) + 1} on page {number}')
          fields.append(value)
  return {'fields': fields}
