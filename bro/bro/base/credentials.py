#!/usr/bin/env python
"""client-side credential resolver.

a reader calls `credentials.default_store().get(name)` and gets the secret's
value — a parsed dict for a json secret, a stripped string for a scalar token —
without caring where it lives or on which surface it runs. resolution walks an
ordered list of `Source`s per secret; the first source that has the value wins.

phase 1 ships one source type, `local`, reading `<project>/.configs/<file>`.
later phases add the `~/.ppp` search path, AWS-backed sources, and let a
generated `.configs/credentials.json` override the built-in registry — see the
"share credentials with bros" design doc.
"""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import configs

__cli_name__ = 'credentials'

# local search roots, highest priority first. phase 1 has exactly one — the
# project `.configs` dir; phase 1.5 appends `~/.ppp`. module-level so tests can
# point it at a tmp dir, and read at fetch time so the override takes effect.
CONFIGS_DIR = configs.DEFAULT_CONFIGS_DIR

# a generated registry file overrides the built-in default when present (phase 2
# writes scoped ones); phase 1 always falls through to the built-in registry.
REGISTRY_FILE = 'credentials.json'


class SecretNotFound(Exception):
  """no source yielded a value for the named secret; `tried` names what was checked."""

  def __init__(self, name: str, tried: list[str]):
    detail = ', '.join(tried) if len(tried) > 0 else '(no sources)'
    super().__init__(f'secret {name!r} not found; tried: {detail}')
    self.name = name
    self.tried = tried


class Source(Protocol):
  """a place a secret's raw text might live."""

  def fetch(self) -> str | None:
    """return the raw text, or None when this source doesn't have it (try the next)."""
    ...

  def describe(self) -> str:
    """short human label for diagnostics (the `tried` list in SecretNotFound)."""
    ...


class LocalSource:
  """reads `<dir>/<file>` for each dir in the local search path. per file: the
  first search dir that has *this* file wins (two paths like `$PATH`)."""

  TYPE = 'local'

  def __init__(self, file: str):
    self.file = file

  def fetch(self) -> str | None:
    for directory in _search_dirs():
      path = Path(directory) / self.file
      if path.is_file():
        return path.read_text()
    return None

  def describe(self) -> str:
    return f'{self.TYPE}:{self.file}'

  @classmethod
  def from_dict(cls, data: dict) -> LocalSource:
    return cls(data['file'])


def _search_dirs() -> list[str]:
  # phase 1: only the project `.configs`. phase 1.5 appends ~/.ppp here.
  return [CONFIGS_DIR]


def _source_from_dict(data: dict) -> Source:
  """reconstruct a Source from its `type` discriminator (mirrors LLMSpec.from_dict)."""
  type_name = data['type']
  if type_name == LocalSource.TYPE:
    return LocalSource.from_dict(data)
  raise ValueError(f'unknown credential source type: {type_name!r}')


class Secret:
  """one named credential: an ordered source list (the order is the resolution
  priority) plus how to parse the raw text. `text=True` → a scalar token returned
  stripped; `text=False` → a json object parsed to a dict."""

  def __init__(self, name: str, sources: Sequence[Source], *, text: bool):
    self.name = name
    self.sources = sources
    self.text = text

  @classmethod
  def from_dict(cls, name: str, data: dict) -> Secret:
    return cls(name, [_source_from_dict(s) for s in data['sources']], text=data['text'])


class Store:
  """resolves secrets against a registry, caching resolved values for its lifetime."""

  def __init__(self, registry: dict[str, Secret]):
    self._registry = registry
    self._cache: dict[str, dict | str] = {}
    self._lock = threading.Lock()

  def get(self, name: str) -> dict | str:
    cached = self._cache.get(name)
    if cached is not None:
      return cached
    # double-checked lock: the hot path above is lock-free (dict reads are atomic
    # under the GIL), and the lock makes a secret fetch at most once even under
    # concurrent callers — no duplicate source reads, no torn cache.
    with self._lock:
      cached = self._cache.get(name)
      if cached is not None:
        return cached
      secret = self._registry.get(name)
      if secret is None:
        raise SecretNotFound(name, [])
      tried: list[str] = []
      for source in secret.sources:
        tried.append(source.describe())
        raw = source.fetch()
        if raw is not None:
          value = raw.strip() if secret.text else json.loads(raw)
          self._cache[name] = value
          return value
      raise SecretNotFound(name, tried)

  def get_json(self, name: str) -> dict:
    """resolve a json secret, narrowing the return type to dict for callers."""
    value = self.get(name)
    if not isinstance(value, dict):
      raise TypeError(f'secret {name!r} resolved to a scalar token, expected a json object')
    return value


# (name, file, text) for every secret the project knows about. the built-in
# default registry maps each to a single local source — this is what keeps any
# surface without a generated registry file (notably the deployed services,
# which synthesize `.configs` at runtime via `_write_configs`) resolving exactly
# as before. the two github tokens are scalar text; everything else is json.
_DEFAULT_SECRETS: list[tuple[str, str, bool]] = [
  ('notion', 'notion.json', False),
  ('focus', 'focus.json', False),
  ('flow_mcp', 'flow_mcp.json', False),
  ('infra', 'infra.json', False),
  ('trails', 'trails.json', False),
  ('session_log', 'session_log.json', False),
  ('process_inbox', 'process_inbox.json', False),
  ('openai', 'openai.json', False),
  ('anthropic', 'anthropic.json', False),
  ('tmdb', 'tmdb.json', False),
  ('brave', 'brave.json', False),
  ('google_api', 'google_api.json', False),
  ('gmail_creds', 'gmail_creds.json', False),
  ('twitch', 'twitch.json', False),
  ('twitch_user_token', 'twitch_user_token.json', False),
  ('github', 'cw_github_token', True),
  ('github_bro', 'cw_github_token_bro', True),
]


def default_registry() -> dict[str, Secret]:
  """the built-in registry: every known secret as a single local source."""
  return {
    name: Secret(name, [LocalSource(file)], text=text) for name, file, text in _DEFAULT_SECRETS
  }


def _load_registry() -> dict[str, Secret]:
  path = Path(CONFIGS_DIR) / REGISTRY_FILE
  if path.is_file():
    data = json.loads(path.read_text())
    return {name: Secret.from_dict(name, spec) for name, spec in data.items()}
  return default_registry()


_default_store: Store | None = None
_default_store_lock = threading.Lock()


def default_store() -> Store:
  """process-wide store over the registry; resolved values cache for the life of
  the process. built lazily under a lock so concurrent callers share one store."""
  global _default_store
  if _default_store is None:
    with _default_store_lock:
      if _default_store is None:
        _default_store = Store(_load_registry())
  return _default_store


def _get(name: str, field: str | None) -> int | None:
  try:
    value = default_store().get(name)
  except SecretNotFound as e:
    print(str(e), file=sys.stderr)
    return 1
  if field is not None:
    if not isinstance(value, dict):
      print(f'secret {name!r} is a scalar token; --field does not apply', file=sys.stderr)
      return 1
    if field not in value:
      print(f'secret {name!r} has no field {field!r}', file=sys.stderr)
      return 1
    value = value[field]
  print(value if isinstance(value, str) else json.dumps(value))
  return None


def main(argv=None) -> int | None:
  import base.args

  parser = base.args.Parser(description='resolve credentials from the default store')
  parser.add_argument('action', choices=['get'], help='operation')
  parser.add_argument('name', help='secret name (e.g. anthropic, notion)')
  parser.add_argument('--field', help='for a json secret, print only this field')
  args = parser.parse(argv)
  del args['action']
  return _get(**args)


if __name__ == '__main__':
  sys.exit(main(sys.argv))
