"""the session's live summon-status file: what the host has authorized, and how
the last one ended.

The host rewrites the whole file on every summon lifecycle event; the session
reads it back through the path `RIDE_SUMMON_STATUS` names. Every entry carries the
`request_id` a lost client reattaches with.

Stdlib-only on purpose: the statusline reads this file on every render, so it
must stay dependency-free.
"""

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

# points a session at its status file — set by the launch surfaces
STATUS_ENV = 'RIDE_SUMMON_STATUS'


@dataclass(frozen=True)
class ActiveSummon:
  """a summon the host authorized and has not yet seen end."""

  request_id: str
  target: str
  trail_id: Optional[str]
  summoner: dict[str, Any]
  started_at: float


@dataclass(frozen=True)
class FinishedSummon:
  """the session's most recent terminal summon outcome."""

  request_id: str
  target: str
  trail_id: Optional[str]
  summoner: dict[str, Any]
  outcome: str
  ended_at: float


@dataclass(frozen=True)
class SummonStatus:
  active: tuple[ActiveSummon, ...] = ()
  last: Optional[FinishedSummon] = None


def status_path() -> Optional[Path]:
  """the session's status file, or None where the environment names none."""
  path = os.environ.get(STATUS_ENV)
  return Path(path) if path is not None else None


def loads(text: str) -> SummonStatus:
  """parse a status file's contents. Raises `ValueError` on anything this module
  did not write."""
  data = json.loads(text)
  try:
    return SummonStatus(
      active=tuple(ActiveSummon(**entry) for entry in data['active']),
      last=FinishedSummon(**data['last']) if data['last'] is not None else None,
    )
  except (AttributeError, KeyError, TypeError) as error:
    raise ValueError(f'malformed summon status: {error}') from error


def read(path: Path) -> SummonStatus:
  """the recorded status, empty until the session's first summon writes the
  file."""
  try:
    text = path.read_text()
  except FileNotFoundError:
    return SummonStatus()
  return loads(text)


def write(path: Path, status: SummonStatus) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  scratch = path.with_suffix('.tmp')
  scratch.write_text(json.dumps(asdict(status), ensure_ascii=False))
  scratch.replace(path)  # atomic: the statusLine never sees a partial write
