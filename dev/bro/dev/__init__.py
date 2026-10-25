import importlib
from typing import Any


def __getattr__(name: str) -> Any:
  # `bro.dev.spells` pulls in the spell store, which the other `bro.dev`
  # modules (policy scripts, command-line tools) must not pay for on import.
  if name == 'spells':
    return importlib.import_module('bro.dev.spells')
  raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
