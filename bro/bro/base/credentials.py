#!/usr/bin/env python
"""client-side credential resolver.

a reader calls `credentials.get(name)` for a secret's raw text, or
`get_json(name)` to parse it as a json object — without caring where it lives or
on which surface it runs. both are thin aliases over `default_store()`.
resolution walks an ordered list of `Source`s per secret; the first source that
has the value wins.

two source types: `local` searches `<project>/.configs/<file>` then
`~/.ppp/<file>` — the deployed services synthesize `<project>/.configs` at
runtime; on the host secrets live only in `~/.ppp`. `ssm` reads an AWS SSM
parameter from the region the source names, for surfaces that resolve secrets
from Parameter Store at runtime instead of carrying files. a generated
`credentials.json` in either search dir overrides the built-in registry
(`CREDENTIALS_REGISTRY=<file>` overrides both, process-scoped, and its directory
joins the local search path first — so a materialized scoped store resolves
wherever it lands); `build_scoped_store` emits a scoped one (in memory) that
`cw` `docker cp`s into a container's `~/.ppp` — or materializes into a host
session's state dir — to bound the resolver to a chosen set of secrets.

absent any of those overrides, resolution uses the host registry: the built-in
defaults merged per-name with a host-local `registry.json` found along the
same local search path as the secret files — entries that never enter the
repo, typically `kind+instance` variants of a checked-in kind
(`github+pavel`). the kind entry (the name up to `+`) owns kind-level
behavior — notably the install hook, a `base.template` text rendered with
`#name` bound to each instance's own name — so a variant declares only its
sources.

a json secret may reference other secrets instead of embedding copies: an
object node `{"$cred": "<name>"}` anywhere in its tree is replaced at
resolution time with the referenced secret's value — the parsed object when
that value is a json object, the raw text as a json string otherwise — and
`{"$cred": "<name>", "field": "<key>"}` picks one top-level field of a
json-object secret. expansion runs inside the store's resolve, before caching,
so every consumer (`get`, `get_json`, the CLI, `build_scoped_store`) sees the
effective, self-contained value — in particular a scoped store materializes
expanded text, keeping the container bounded to its declared secrets with no
knowledge of the references. a reference that does not resolve, a malformed
node, an absent field, or a reference cycle raises.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Optional, Protocol

import configs
from base import log, template
from base.args import Parser
from base.condition import StringVariable

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

# the process-scoped registry override described in the module docstring.
REGISTRY_ENV = 'CREDENTIALS_REGISTRY'

# the host-local additions file, searched along the resolver's local path and
# merged per-name over the built-in registry (`host_registry`) — unlike a
# generated REGISTRY_FILE, which replaces the registry wholesale to bound it.
HOST_REGISTRY_FILE = 'registry.json'

# a secret name: `kind` or `kind+instance`. the charsets keep every name safe
# to splice into the single-quoted insert slot of an install-hook template, and
# safe to type unquoted in a shell.
_NAME_GRAMMAR = re.compile(r'([a-z0-9_]+)(?:\+([a-z0-9_-]+))?')

# the reference-node keys of a json secret (module docstring): `$cred` names the
# referenced secret, `field` optionally picks one top-level field of its object.
_REFERENCE_KEY = '$cred'
_REFERENCE_FIELD = 'field'


def _parse_name(name: str) -> tuple[str, Optional[str]]:
  """split a secret name into (kind, instance); a plain name is its own kind
  with no instance."""
  match = _NAME_GRAMMAR.fullmatch(name)
  if match is None:
    raise ValueError(f'malformed secret name {name!r}; expected kind or kind+instance')
  return match.group(1), match.group(2)


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
  """reads `file` from the local search path (`_find_in_search_dirs`)."""

  TYPE = 'local'

  def __init__(self, file: str):
    self.file = file

  def fetch(self) -> Optional[str]:
    path = _find_in_search_dirs(self.file)
    if path is None:
      return None
    try:
      text = path.read_text()
    except UnicodeDecodeError:
      raise ValueError(f'credential file {path} is not valid UTF-8 text')
    if '\x00' in text:
      raise ValueError(f'credential file {path} contains null bytes')
    return text

  @classmethod
  def from_dict(cls, data: dict) -> LocalSource:
    return cls(data['file'])


class SSMSource:
  """reads an AWS SSM parameter (decrypted) from the region the source names.
  the region is required: SSM is a regional service, and a non-AWS surface (e.g.
  a cw container holding only static credentials) has no ambient region to
  discover, so the registry states it. credentials come from the ambient AWS
  configuration. a missing parameter falls through to the next source; credential
  or permission errors propagate — a surface that is supposed to reach SSM but
  can't is a loud failure, not a silent fallthrough."""

  TYPE = 'ssm'

  def __init__(self, parameter: str, region: str):
    self.parameter = parameter
    self.region = region

  def fetch(self) -> Optional[str]:
    # deferred so surfaces that never resolve an ssm-backed secret don't need boto3
    import boto3

    client = boto3.client('ssm', region_name=self.region)
    try:
      response = client.get_parameter(Name=self.parameter, WithDecryption=True)
    except client.exceptions.ParameterNotFound:
      return None
    return response['Parameter']['Value']

  @classmethod
  def from_dict(cls, data: dict) -> SSMSource:
    return cls(data['parameter'], data['region'])


def _search_dirs() -> list[str]:
  # an explicit CREDENTIALS_REGISTRY carries its sibling files: a scoped store is
  # a registry plus the `{name}.cred` files it points at, materialized in one dir
  # (`build_scoped_store`), so that dir must be searched first for the store to
  # resolve wherever it lands — the container's ~/.ppp needs no override, a host
  # session's store lives outside the standard dirs.
  override = os.environ.get(REGISTRY_ENV)
  if override is not None and override != '':
    return [str(Path(override).parent), CONFIGS_DIR, PPP_DIR]
  return [CONFIGS_DIR, PPP_DIR]


def _find_in_search_dirs(file: str) -> Optional[Path]:
  """locate `file` along the local search path; per file, the first search dir
  that has it wins (dirs like `$PATH`), absent everywhere → None."""
  for directory in _search_dirs():
    path = Path(directory) / file
    if path.is_file():
      return path
  return None


def _contains_reference(node: Any) -> bool:
  if isinstance(node, dict):
    return _REFERENCE_KEY in node or any(_contains_reference(value) for value in node.values())
  if isinstance(node, list):
    return any(_contains_reference(item) for item in node)
  return False


def _source_from_dict(data: dict) -> Source:
  """reconstruct a Source from its `type` discriminator (mirrors LLMSpec.from_dict);
  `type` defaults to `local` when omitted."""
  type_name = data.get('type', LocalSource.TYPE)
  if type_name == LocalSource.TYPE:
    return LocalSource.from_dict(data)
  if type_name == SSMSource.TYPE:
    return SSMSource.from_dict(data)
  raise ValueError(f'unknown credential source type: {type_name!r}')


class Secret:
  """one named credential: an ordered source list (the order is the resolution
  priority). the resolver treats the value as an opaque text blob — callers pick
  the shape, `get()` for the raw text or `get_json()` to parse it as a json object.

  `install` is an optional shell hook that wires the secret into the tool that
  consumes it from *outside* the resolver (git, the aws CLI, ...). The registry
  declares it as a `base.template` text over the `#name` variable —
  `credentials get '{{insert #name}}'` — rendered here with the secret's own
  name, so one kind-level hook serves every instance of the kind. The container
  entrypoint `eval`s it after hydration; the hook pulls the value via
  `credentials get` at eval time, so per-secret wiring lives in the registry
  with no interpolated path and the entrypoint stays generic."""

  def __init__(self, name: str, sources: Sequence[Source], *, install: Optional[str] = None):
    self.name = name
    self.sources = sources
    self.install = install

  @classmethod
  def from_dict(cls, name: str, data: dict) -> Secret:
    install = data.get('install')
    if install is not None:
      install = template.render(install, {'name': StringVariable(name)})
    return cls(
      name,
      [_source_from_dict(s) for s in data['sources']],
      install=install,
    )


class Store:
  """resolves secrets against a registry, caching resolved values for its
  lifetime. a json secret's `{"$cred": ...}` reference nodes are expanded
  during the resolve (module docstring), so cached and returned values are
  always the effective, self-contained text."""

  def __init__(self, registry: dict[str, Secret]):
    self._registry = registry
    self._cache: dict[str, str] = {}
    self._lock = threading.Lock()

  def try_get(self, name: str) -> Optional[str]:
    """resolve a secret to its raw text (stripped), or None when no source yields
    a value — the non-raising primitive, for callers that treat a missing secret
    as an expected case. `get` is the strict wrapper that raises on None. a
    malformed value (a broken reference, a non-UTF-8 file) still raises: absence
    is expected, corruption is not."""
    # one lock around the whole resolve: a secret is fetched at most once even
    # under concurrent callers, and the store is read only a handful of times per
    # process (each value cached on first read), so a lock-free fast path buys
    # nothing.
    with self._lock:
      return self._resolve(name, chain=())

  def _resolve(self, name: str, chain: tuple[str, ...]) -> Optional[str]:
    """fetch, expand, and cache one secret; `chain` is the stack of secrets whose
    expansions are in progress, for cycle detection. callers hold the lock."""
    cached = self._cache.get(name)
    if cached is not None:
      return cached
    secret = self._registry.get(name)
    if secret is None:
      return None
    for source in secret.sources:
      raw = source.fetch()
      if raw is not None:
        value = self._expand_references(raw.strip(), (*chain, name))
        self._cache[name] = value
        return value
    return None

  def _expand_references(self, text: str, chain: tuple[str, ...]) -> str:
    """substitute every reference node in a json secret's tree; text that isn't
    json, or json with no reference nodes, passes through byte-identical."""
    try:
      tree = json.loads(text)
    except json.JSONDecodeError:
      return text
    if not _contains_reference(tree):
      return text
    return json.dumps(self._substitute_references(tree, chain))

  def _substitute_references(self, node: Any, chain: tuple[str, ...]) -> Any:
    if isinstance(node, dict):
      if _REFERENCE_KEY in node:
        return self._referenced_value(node, chain)
      return {key: self._substitute_references(value, chain) for key, value in node.items()}
    if isinstance(node, list):
      return [self._substitute_references(item, chain) for item in node]
    return node

  def _referenced_value(self, node: dict, chain: tuple[str, ...]) -> Any:
    referrer = chain[-1]
    unknown = sorted(set(node) - {_REFERENCE_KEY, _REFERENCE_FIELD})
    if len(unknown) > 0:
      raise ValueError(
        f'secret {referrer!r}: reference node has unknown keys: {", ".join(map(repr, unknown))}'
      )
    target = node[_REFERENCE_KEY]
    if not isinstance(target, str):
      raise ValueError(f'secret {referrer!r}: reference name must be a string, got {target!r}')
    field = node.get(_REFERENCE_FIELD)
    if field is not None and not isinstance(field, str):
      raise ValueError(f'secret {referrer!r}: reference field must be a string, got {field!r}')
    if target in chain:
      raise ValueError(f'credential reference cycle: {" -> ".join((*chain, target))}')
    value = self._resolve(target, chain)
    if value is None:
      raise ValueError(f'secret {referrer!r} references {target!r}, which does not resolve')
    try:
      parsed = json.loads(value)
    except json.JSONDecodeError:
      parsed = None
    if isinstance(parsed, dict):
      if field is None:
        return parsed
      if field not in parsed:
        raise ValueError(
          f'secret {referrer!r}: referenced secret {target!r} has no field {field!r}'
        )
      return parsed[field]
    if field is not None:
      raise ValueError(
        f'secret {referrer!r}: referenced secret {target!r} is not a json object; '
        f'cannot take field {field!r}'
      )
    return value

  def get(self, name: str) -> str:
    """resolve a secret to its raw text, raising `SecretNotFound` when no source
    yields a value."""
    value = self.try_get(name)
    if value is not None:
      return value
    raise SecretNotFound(name)

  def available(self, name: str) -> bool:
    """whether `name` resolves in this store. the predicate behind both the
    runtime capability gate and the credential template directives (llm/mcp.py)."""
    return self.try_get(name) is not None

  def known_names(self) -> frozenset[str]:
    """every secret name this store's registry knows, resolvable or not."""
    return frozenset(self._registry)

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


# every secret the project knows about, in the same shape as a generated
# `credentials.json` so it is constructed the same way (via `Secret.from_dict`).
_BUILTIN_REGISTRY_PATH = Path(__file__).with_name('registry.json')


def _registry_from_dict(data: dict) -> dict[str, Secret]:
  return {name: Secret.from_dict(name, spec) for name, spec in data.items()}


def _resolve_kinds(data: dict) -> dict:
  """validate every name against the grammar and give each `kind+instance`
  variant its kind entry's install-hook template (instantiated per-entry by
  `Secret.from_dict`). the kind owns kind-level behavior, so a variant carrying
  its own `install` — or naming a kind the registry lacks — is an error. only
  the built-in/host registries pass through here: a generated registry is
  self-contained, its variant entries already carrying their materialized hooks.
  """
  resolved: dict[str, dict] = {}
  for name, entry in data.items():
    kind, instance = _parse_name(name)
    if instance is None:
      resolved[name] = entry
      continue
    if 'install' in entry:
      raise ValueError(f'variant {name!r} declares an install hook; the kind entry owns it')
    kind_entry = data.get(kind)
    if kind_entry is None:
      raise ValueError(f'variant {name!r} has no kind entry {kind!r} in the registry')
    install = kind_entry.get('install')
    resolved[name] = entry if install is None else {**entry, 'install': install}
  return resolved


def default_registry() -> dict[str, Secret]:
  """the built-in registry (every known secret as a single local source)."""
  return _registry_from_dict(_resolve_kinds(json.loads(_BUILTIN_REGISTRY_PATH.read_text())))


def host_registry() -> dict[str, Secret]:
  """the built-in registry merged per-name with the host-local additions file —
  entries that never enter the repo, typically variants of a checked-in kind.
  the additions file follows the local search path, like any secret file. kind
  resolution runs after the merge, so a variant picks up its kind's hook even
  when an addition overrides the kind."""
  data = json.loads(_BUILTIN_REGISTRY_PATH.read_text())
  additions_path = _find_in_search_dirs(HOST_REGISTRY_FILE)
  if additions_path is not None:
    data.update(json.loads(additions_path.read_text()))
  return _registry_from_dict(_resolve_kinds(data))


def _load_registry() -> dict[str, Secret]:
  # CREDENTIALS_REGISTRY points the process at an explicit registry file, above
  # every other source of one — for a run that must resolve against a specific
  # registry (e.g. emails/run_e2e_tests.sh pointing the harness at the pipeline's
  # ssm-backed registry). a bad path raises rather than falling through: an
  # explicit override that silently degraded to the built-in would resolve
  # against the wrong secret set.
  override = os.environ.get(REGISTRY_ENV)
  if override is not None and override != '':
    return _registry_from_dict(json.loads(Path(override).read_text()))
  # a generated registry file in either search dir (`<project>/.configs` for the
  # deployed services, `~/.ppp` for a scoped per-container store) overrides the
  # host registry wholesale; absent everywhere → the host registry (built-in
  # defaults + host-local additions).
  path = _find_in_search_dirs(REGISTRY_FILE)
  if path is not None:
    return _registry_from_dict(json.loads(path.read_text()))
  return host_registry()


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


def try_get(name: str) -> Optional[str]:
  """resolve a secret to its raw text via the process-wide default store, or None
  when no source yields a value — the non-raising sibling of `get`."""
  return default_store().try_get(name)


def get_json(name: str) -> dict:
  """resolve a secret and parse it as a json object via the process-wide default store."""
  return default_store().get_json(name)


def available(name: str) -> bool:
  """whether `name` resolves in the process-wide default store, without raising."""
  return default_store().available(name)


def known_names() -> frozenset[str]:
  """every secret name the process-wide default store's registry knows."""
  return default_store().known_names()


def _require_one_instance_per_kind(names: Iterable[str]) -> None:
  by_kind: dict[str, list[str]] = {}
  for name in sorted(set(names)):
    kind, _ = _parse_name(name)
    by_kind.setdefault(kind, []).append(name)
  for kind, instances in by_kind.items():
    if len(instances) > 1:
      raise ValueError(
        f'secrets {", ".join(map(repr, instances))} are instances of the same kind '
        f'{kind!r}; a session installs at most one'
      )


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

  a session installs at most one instance of each kind: declaring two —
  `github` and `github+pavel`, in whichever tiers — raises `ValueError`. The
  check runs over the declared union up front, so an unresolvable optional name
  cannot flap the outcome.
  """
  _require_one_instance_per_kind(set(names) | set(optional))
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
  computed: Iterable[str],
  *,
  grant: Iterable[str] = (),
  revoke: Iterable[str] = (),
  subject: str = 'set',
) -> set[str]:
  """layer per-session grant/revoke overrides onto a computed name set (a scoped
  credential set, a summon allow-list, ...).

  returns `(computed | grant) - revoke`. every override must change the set:
  granting a name already present, or revoking one absent, raises `ValueError`
  and stops — a redundant grant/revoke is a mistake to surface, not silently
  swallow (a no-op revoke especially: it would read as "tightened" while changing
  nothing). granting or revoking the same name twice trips the same checks, and a
  name in both lists is rejected outright. `subject` names the set in the error
  messages so a caller's flag misuse reads in its own terms. unknown grant names
  are not validated here — each caller owns its registry check (for credentials,
  `build_scoped_store` rejects them loudly on the host). does not mutate the
  inputs.
  """
  result = set(computed)
  grant = list(grant)
  revoke = list(revoke)
  both = sorted(set(grant) & set(revoke))
  if len(both) > 0:
    raise ValueError(f'cannot grant and revoke the same name: {", ".join(both)}')
  for name in grant:
    if name in result:
      raise ValueError(f'cannot grant {name!r}: already in the {subject}')
    result.add(name)
  for name in revoke:
    if name not in result:
      raise ValueError(f'cannot revoke {name!r}: not in the {subject}')
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


def _list_available() -> None:
  store = default_store()
  for name in sorted(store.known_names()):
    if store.available(name):
      print(name)


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
    'list', help='list credential names that resolve in the default store'
  ).set_handler(_list_available)
  subparser.add_parser(
    'install-hooks', help='print shell install hooks for the container entrypoint to eval'
  ).set_handler(_print_hooks)
  return parser.dispatch(argv)
