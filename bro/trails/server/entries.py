"""The format and wire entries a server installation promises to serve."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from bro.llm import wires
from bro.trails import backends


class ServedEntry(BaseModel, frozen=True):
  model_config = ConfigDict(extra='forbid')

  group: Literal['bro.trail_formats', 'bro.llm.wires']
  name: str = Field(pattern=r'^[a-z][a-z0-9-]*$')


def read_entries(path: Path) -> tuple[ServedEntry, ...]:
  entries = TypeAdapter(tuple[ServedEntry, ...]).validate_json(path.read_text())
  if not entries or len(set(entries)) != len(entries):
    raise ValueError('served entries must be non-empty and unique')
  return entries


def resolve_entries(entries: tuple[ServedEntry, ...]) -> None:
  registry = wires.WireRegistry()
  claimants = []
  for entry in entries:
    try:
      if entry.group == backends.ENTRY_POINT_GROUP:
        backends.get_format(entry.name)
      elif getattr(registry.get(entry.name), 'LEGACY', False):
        claimants.append(entry.name)
    except Exception as error:
      raise RuntimeError(f'cannot serve {entry.group}:{entry.name}: {error}') from error
  if len(claimants) != 1:
    raise ValueError(
      'exactly one declared wire must claim untagged records; '
      f'claimants: {", ".join(claimants) or "(none)"}'
    )
