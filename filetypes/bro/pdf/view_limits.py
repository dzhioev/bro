"""Cumulative string and list-entry bounds for PDF views."""

from typing import Any

from bro.files.protocol import InvalidInput
from bro.files.types import Limits


def measure(value: Any) -> tuple[int, int]:
  if isinstance(value, str):
    return len(value), 0
  if isinstance(value, (list, dict)):
    strings = 0
    entries = len(value) if isinstance(value, list) else 0
    for item in value if isinstance(value, list) else value.values():
      item_strings, item_entries = measure(item)
      strings += item_strings
      entries += item_entries
    return strings, entries
  return 0, 0


class ViewBudget:
  def __init__(self, limits: Limits):
    self.limits = limits
    self.characters = 0
    self.entries = 0

  def add(self, entry: dict[str, Any], *, location: str) -> None:
    characters, entries = measure(entry)
    self.characters += characters
    self.entries += entries + 1
    if self.characters > self.limits.max_view_characters:
      raise InvalidInput(f'PDF view exceeds max_view_characters at {location}; narrow pages')
    if self.entries > self.limits.max_view_entries:
      raise InvalidInput(f'PDF view exceeds max_view_entries at {location}; narrow pages')
