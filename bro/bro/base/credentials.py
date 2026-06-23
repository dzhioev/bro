#!/usr/bin/env python
"""client-side credential resolver.

a reader calls `credentials.get(name)` for a secret's raw text, or
`get_json(name)` to parse it as a json object — without caring where it lives or
on which surface it runs. both are thin aliases over `default_store()`.
resolution walks an ordered list of `Source`s per secret; the first source that
has the value wins.

the one source type so far, `local`, searches `<project>/.configs/<file>` then
`~/.ppp/<file>` — the deployed services synthesize `<project>/.configs` at
runtime; on the host secrets live only in `~/.ppp`. a generated `credentials.json`
in either search dir overrides the built-in registry; `build_scoped_store` emits
a scoped one (in memory) that `cw` `docker cp`s into a container's `~/.ppp` to
bound it to a chosen set of secrets.
"""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Optional, Protocol

import configs
from base import log
from base.args import Parser

__cli_name__ = 'credentials'

# local search roots, highest priority first: the project `.configs` dir (where
# deployed services synthesize their configs) then the standalone `~/.ppp` host
# store. module-level so tests can point them at tmp dirs, and read at fetch time
# so the overrides take effect.
CONFIGS_DIR = configs.DEFAULT_CONFIGS_DIR
PPP_DIR = configs.DEFAULT_PPP_DIR

# a generated registry file (emitted by `build_scoped_store`) overrides the
# built-in default when present; absent, resolution falls through to the built-in.
REGISTRY_FILE = 'credentials.json'


class SecretNotFound(Exception):
  """no source yielded a value for the named secret."""

  def __init__(self, name: str):
    super().__init__(f'secret {name!r} not found')
    self.name = name


class Source(Protocol):
  """a place a secret's raw text might live."""

  def fetch(self) -> Optional[str]:
    """return the raw text, or None when this source doesn't have it (try the next)."""
    ...


class LocalSource:
  """reads `<dir>/<file>` for each dir in the local search path. per file: the
  first search dir that has *this* file wins (two paths like `$PATH`)."""

  TYPE = 'local'

  def __init__(self, file: str):
    self.file = file

  def fetch(self) -> Optional[str]:
    for directory in _search_dirs():
      path = Path(directory) / self.file
      if path.is_file():
        return path.read_text()
    return None

  @classmethod
  def from_dict(cls, data: dict) -> LocalSource:
    return cls(data['file'])


def _search_dirs() -> list[str]:
  return [CONFIGS_DIR, PPP_DIR]


def _source_from_dict(data: dict) -> Source:
  """reconstruct a Source from its `type` discriminator (mirrors LLMSpec.from_dict);
  `type` defaults to `local` when omitted."""
  type_name = data.get('type', LocalSource.TYPE)
  if type_name == LocalSource.TYPE:
    return LocalSource.from_dict(data)
  raise ValueError(f'unknown credential source type: {type_name!r}')


class Secret:
  """one named credential: an ordered source list (the order is the resolution
  priority). the resolver treats the value as an opaque text blob — callers pick
  the shape, `get()` for the raw text or `get_json()` to parse it as a json object.

  `install` is an optional static shell hook that wires the secret into the tool
  that consumes it from *outside* the resolver (git, the aws CLI, ...). The
  container entrypoint `eval`s it after hydration; the hook pulls the value via
  `credentials get <name>` at eval time, so per-secret wiring lives in the registry
  with no interpolated path and the entrypoint stays generic."""

  def __init__(self, name: str, sources: Sequence[Source], *, install: Optional[str] = None):
    self.name = name
    self.sources = sources
    self.install = install

  @classmethod
  def from_dict(cls, name: str, data: dict) -> Secret:
    return cls(
      name,
      [_source_from_dict(s) for s in data['sources']],
      install=data.get('install'),
    )


class Store:
  """resolves secrets against a registry, caching resolved values for its lifetime."""

  def __init__(self, registry: dict[str, Secret]):
    self._registry = registry
    self._cache: dict[str, str] = {}
    self._lock = threading.Lock()

  def try_get(self, name: str) -> Optional[str]:
    """resolve a secret to its raw text (stripped), or None when no source yields
    a value — the non-raising primitive, for callers that treat a missing secret
    as an expected case. `get` is the strict wrapper that raises on None."""
    # one lock around the whole resolve: a secret is fetched at most once even
    # under concurrent callers, and the store is read only a handful of times per
    # process (each value cached on first read), so a lock-free fast path buys
    # nothing.
    with self._lock:
      cached = self._cache.get(name)
      if cached is not None:
        return cached
      secret = self._registry.get(name)
      if secret is None:
        return None
      for source in secret.sources:
        raw = source.fetch()
        if raw is not None:
          value = raw.strip()
          self._cache[name] = value
          return value
      return None

  def get(self, name: str) -> str:
    """resolve a secret to its raw text, raising `SecretNotFound` when no source
    yields a value."""
    value = self.try_get(name)
    if value is not None:
      return value
    raise SecretNotFound(name)

  def available(self, name: str) -> bool:
    """whether `name` resolves in this store. the predicate behind both the
    runtime capability gate and the `has_cred` description renderer."""
    return self.try_get(name) is not None

  def get_json(self, name: str) -> dict:
    """resolve a secret and parse it as a json object. raises if the text isn't
    valid json or isn't an object (e.g. a scalar token)."""
    raw = self.get(name)
    try:
      value = json.loads(raw)
    except json.JSONDecodeError as e:
      raise ValueError(f'secret {name!r} is not valid json') from e
    if not isinstance(value, dict):
      raise ValueError(f'secret {name!r} is not a json object')
    return value


# the built-in registry for every secret the project knows about, in the same
# shape as a generated `credentials.json` so it is constructed the same way (via
# `Secret.from_dict`). each secret maps to one local file. an `install` hook wires
# a secret into a tool that reads it from outside the resolver (git, the aws CLI)
# — see `install_hooks`.
_BUILTIN_REGISTRY: dict = {
  'notion': {'sources': [{'file': 'notion.json'}]},
  'focus': {'sources': [{'file': 'focus.json'}]},
  'flow_mcp': {'sources': [{'file': 'flow_mcp.json'}]},
  'infra': {'sources': [{'file': 'infra.json'}]},
  'trails': {'sources': [{'file': 'trails.json'}]},
  'session_log': {'sources': [{'file': 'session_log.json'}]},
  'process_inbox': {'sources': [{'file': 'process_inbox.json'}]},
  'openai': {'sources': [{'file': 'openai.json'}]},
  'anthropic': {'sources': [{'file': 'anthropic.json'}]},
  'tmdb': {'sources': [{'file': 'tmdb.json'}]},
  'brave': {'sources': [{'file': 'brave.json'}]},
  'google_api': {'sources': [{'file': 'google_api.json'}]},
  'gmail_creds': {'sources': [{'file': 'gmail_creds.json'}]},
  'twitch': {'sources': [{'file': 'twitch.json'}]},
  'twitch_user_token': {'sources': [{'file': 'twitch_user_token.json'}]},
  'github': {
    'sources': [{'file': 'cw_github_token_bro'}],
    # GH_TOKEN is exported once and read by both gh and the git credential helper
    # below (which expands it per push).
    'install': (
      'export GH_TOKEN="$(credentials get github)"\n'
      'git config --global credential.helper '
      '\'!f() { echo username=x-access-token; echo "password=$GH_TOKEN"; }; f\''
    ),
  },
  'aws': {
    'sources': [{'file': 'aws_credentials'}],
    # ~/.aws/credentials is where the aws CLI/SDK reads by default, so placing the
    # value there is the whole install. the subshell confines umask 077 to the
    # write so the file lands 0600 without the umask persisting into the session.
    'install': '(umask 077; mkdir -p "$HOME/.aws"; credentials get aws > "$HOME/.aws/credentials")',
  },
}


def _registry_from_dict(data: dict) -> dict[str, Secret]:
  return {name: Secret.from_dict(name, spec) for name, spec in data.items()}


def default_registry() -> dict[str, Secret]:
  """the built-in registry (every known secret as a single local source)."""
  return _registry_from_dict(_BUILTIN_REGISTRY)


def _load_registry() -> dict[str, Secret]:
  # a generated registry file in either search dir (`<project>/.configs` for the
  # deployed services, `~/.ppp` for a scoped per-container store) overrides the
  # built-in default; the first dir that has it wins, absent everywhere → built-in.
  for directory in _search_dirs():
    path = Path(directory) / REGISTRY_FILE
    if path.is_file():
      return _registry_from_dict(json.loads(path.read_text()))
  return default_registry()


_default_store: Optional[Store] = None
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


def get(name: str) -> str:
  """resolve a secret to its raw text via the process-wide default store."""
  return default_store().get(name)


def get_json(name: str) -> dict:
  """resolve a secret and parse it as a json object via the process-wide default store."""
  return default_store().get_json(name)


def available(name: str) -> bool:
  """whether `name` resolves in the process-wide default store, without raising."""
  return default_store().available(name)


def build_scoped_store(names: Iterable[str], *, optional: Iterable[str] = ()) -> dict[str, bytes]:
  """build a per-container scoped credential store in memory.

  returns a map of relative file name to its bytes: one `{name}.cred` entry per
  hydrated secret holding its resolved raw text, plus a generated
  `credentials.json` registry covering exactly those secrets and pointing each at
  its `{name}.cred`. materialising this map as the container's
  `~/.ppp` then bounds the container to this set; any other secret resolves to a
  clean `SecretNotFound`. The bytes never touch a host file — `cw` packs them
  into a tar and `docker cp`s them straight into the container.

  `names` is the required tier: hydration is strict — an unknown name (not in the
  host registry) raises `ValueError`, and a declared name whose value can't be
  resolved raises `SecretNotFound`, so a typo or a missing secret fails loudly
  here, on the host, before the container exists.

  `optional` is the best-effort tier: each name (minus those already in `names` —
  required wins) is resolved if it can be, and silently skipped when unknown to
  the registry or unresolvable. This is for secrets a component uses if present
  but degrades without (e.g. the LLM key behind a query-focused fetch summary),
  so an absent optional secret degrades the component instead of failing launch.
  """
  registry = _load_registry()
  store = Store(registry)
  files: dict[str, bytes] = {}
  scoped: dict[str, dict] = {}

  def materialize(name: str, value: str, secret: Secret) -> None:
    # resolve generically on the host (a future non-local source uses the host's
    # own credentials), then materialize under a uniform `{name}.cred`. the scoped
    # file is local regardless of the host source type, so the container only ever
    # sees a plain local file and the registry it reads stays local-only by
    # construction — the filename is internal to the scoped store, not borrowed
    # from the source.
    file = f'{name}.cred'
    files[file] = value.encode()
    entry: dict = {'sources': [{'file': file}]}
    if secret.install is not None:
      entry['install'] = secret.install
    scoped[name] = entry

  for name in sorted(set(names)):
    secret = registry.get(name)
    if secret is None:
      raise ValueError(f'unknown secret {name!r} declared in manifest; not in the registry')
    value = store.get(name)  # strict: SecretNotFound propagates on a missing value
    materialize(name, value, secret)
  for name in sorted(set(optional) - set(names)):
    secret = registry.get(name)
    if secret is None:
      log.debug('optional secret %r not in the registry; skipping', name)
      continue
    value = store.try_get(name)
    if value is None:
      log.debug('optional secret %r unresolvable; skipping', name)
      continue
    materialize(name, value, secret)
  files[REGISTRY_FILE] = json.dumps(scoped).encode()
  return files


def apply_grant_revoke(
  computed: Iterable[str], *, grant: Iterable[str] = (), revoke: Iterable[str] = ()
) -> set[str]:
  """layer per-session grant/revoke overrides onto a computed scoped credential set.

  returns `(computed | grant) - revoke`. every override must change the set:
  granting a secret already present, or revoking one absent, raises `ValueError`
  and stops — a redundant grant/revoke is a mistake to surface, not silently
  swallow (a no-op revoke especially: it would read as "tightened" while changing
  nothing). granting or revoking the same name twice trips the same checks, and a
  name in both lists is rejected outright. unknown grant names are not validated
  here — `build_scoped_store` is strict on the registry and rejects them loudly on
  the host. does not mutate the inputs.
  """
  result = set(computed)
  grant = list(grant)
  revoke = list(revoke)
  both = sorted(set(grant) & set(revoke))
  if len(both) > 0:
    raise ValueError(f'cannot grant and revoke the same secret: {", ".join(both)}')
  for name in grant:
    if name in result:
      raise ValueError(f'cannot grant {name!r}: already in the scoped credential set')
    result.add(name)
  for name in revoke:
    if name not in result:
      raise ValueError(f'cannot revoke {name!r}: not in the scoped credential set')
    result.remove(name)
  return result


def install_hooks() -> str:
  """shell wiring each secret into the tool that consumes it from outside the
  resolver (git, the aws CLI, ...), for the container entrypoint to `eval`. each
  secret declares a static `install` hook in the registry that pulls its value via
  `credentials get` at eval time — no path interpolation. a hook emits for every
  registry secret that declares one: in a scoped container the registry is exactly
  the hydrated (present) set, since `build_scoped_store` only includes resolvable
  secrets."""
  lines: list[str] = []
  for secret in _load_registry().values():
    if secret.install is not None:
      lines.append(secret.install)
  return '\n'.join(lines)


def _get(name: str, field: Optional[str], as_json: bool) -> Optional[int]:
  store = default_store()
  try:
    # a bare get prints the raw text; --field / --json need the parsed object.
    if field is None and not as_json:
      print(store.get(name))
      return None
    data = store.get_json(name)
  except (SecretNotFound, ValueError) as e:
    print(str(e), file=sys.stderr)
    return 1
  value: dict | str = data
  if field is not None:
    if field not in data:
      print(f'secret {name!r} has no field {field!r}', file=sys.stderr)
      return 1
    value = data[field]
  if as_json:
    print(json.dumps(value, indent=2))
  else:
    print(value if isinstance(value, str) else json.dumps(value))
  return None


def _print_hooks() -> None:
  print(install_hooks())


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description='resolve credentials from the default store')
  subparser = parser.add_subparsers(dest='action', required=True)
  get_parser = subparser.add_parser('get', help='resolve a secret and print it')
  get_parser.add_argument('name', help='secret name (e.g. anthropic, notion)')
  get_parser.add_argument('--field', help='for a json secret, print only this field')
  get_parser.add_argument(
    '--json', dest='as_json', action='store_true', help='parse as json and pretty-print (indent=2)'
  )
  get_parser.set_handler(_get)
  subparser.add_parser(
    'install-hooks', help='print shell install hooks for the container entrypoint to eval'
  ).set_handler(_print_hooks)
  return parser.dispatch(argv)
