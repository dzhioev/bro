"""Shared mechanics for provider-owned price tables."""

import hashlib
import json
import threading
import warnings
from collections.abc import Mapping
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

_STALE_AFTER = timedelta(days=30)
_warned_tables: set[tuple[str, date]] = set()
_warning_lock = threading.Lock()


class StalePriceTableWarning(UserWarning):
  """A provider price table is old enough that its rates should be checked."""


def content_sha256(content: Mapping[str, Any]) -> str:
  """Return the digest of a table's canonical, JSON-ready content."""
  encoded = json.dumps(content, sort_keys=True, separators=(',', ':')).encode()
  return hashlib.sha256(encoded).hexdigest()


def decimal_strings(values: Mapping[str, Decimal]) -> dict[str, str]:
  return {name: str(value) for name, value in values.items()}


def warn_if_stale(provider: str, as_of: date) -> None:
  if date.today() - as_of <= _STALE_AFTER:
    return
  key = (provider, as_of)
  with _warning_lock:
    if key in _warned_tables:
      return
    _warned_tables.add(key)
  warnings.warn(
    f'{provider} price table from {as_of.isoformat()} is more than 30 days old',
    StalePriceTableWarning,
    stacklevel=2,
  )
