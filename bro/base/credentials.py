#!/usr/bin/env python
"""client-side credential resolver.

a reader calls `credentials.get(kind)` for a secret's raw text, or
`get_json(kind)` to parse it as a json object — without caring where it lives or
on which surface it runs. both are thin aliases over `default_store()`.
resolution walks an ordered list of `Source`s per secret; the first source that
has the value wins.

reads come in two addressing families, differing only in the names they accept —
both resolve entries of whatever registry is loaded. the get family (`get`,
`try_get`, `get_json`, `available`) is kind-addressed: it takes a kind and
rejects a `kind+instance` name, always reading the kind's default-instance
entry — the portable mode, since a generated scoped registry stores every
hydrated secret under its kind name and so resolves kind reads by construction.
the get_instance family (`get_instance`, `try_get_instance`,
`get_instance_json`) is storage-addressed: it takes any stored entry name,
plain or `kind+instance`, for readers that mean one specific entry. the CLI's
`get` and `list` default to kind addressing; `--instance` switches them.

sources are either stored or minting. two stored types: `local` searches the
explicit `BRO_CONFIGS_DIR` when set, then `~/.bro/<file>`; deployed services set
the explicit directory when they synthesize configs, while the host uses only
`~/.bro`. `ssm` reads an AWS SSM parameter from the region the source names,
for surfaces that resolve secrets from Parameter Store at runtime instead of
carrying files. a minting type (a `MintingSource` subclass owned by a domain
package, e.g. `github_app` in `extra/github/app.py`) derives short-lived values
from a minting config file found
along the same local search path; the source keeps the minted value and
re-mints as expiry nears, and such a secret — like any secret whose references
reach one — bypasses the store's process-lifetime cache, so every read observes
a value with usable lifetime left. a generated
`credentials.json` in either search dir overrides the built-in registry
(`CREDENTIALS_REGISTRY=<file>` overrides both, process-scoped, and its directory
joins the local search path first — so a materialized scoped store resolves
wherever it lands); `build_scoped_store` emits a scoped one (in memory) that
`cw` `docker cp`s into a container's `~/.bro` — or materializes into a host
session's state dir — to bound the resolver to a chosen set of secrets.

absent any of those overrides, resolution uses the host registry: the built-in
defaults merged per-name with a host-local `registry.json` found along the
same local search path as the secret files — entries that never enter the
repo, typically `kind+instance` variants of a checked-in kind
(`github+alice`). the kind entry (the name up to `+`) owns kind-level
behavior — notably the install hook, a `bro.base.template` text rendered with
`#name` bound to each instance's own name — so a variant declares only its
sources. a kind entry may select its default instance by name instead of
declaring sources: `{"instance": "alice"}` borrows `kind+alice`'s sources,
keeping the kind's own install hook — the durable, host-owned way to decide
which variant backs a kind (per-launch grant/revoke overrides it). instance
entries never enter a generated registry: `build_scoped_store` materializes a
variant under its kind name, so a scoped store carries kind entries only.

a json secret may reference other secrets instead of embedding copies: an
object node `{"$cred": "<name>"}` anywhere in its tree is replaced at
resolution time with the referenced secret's value — the parsed object when
that value is a json object, the raw text as a json string otherwise — and
`{"$cred": "<name>", "field": "<key>"}` picks one top-level field of a
json-object secret. expansion runs inside the store's resolve, before caching,
so every consumer (`get`, `get_json`, the CLI, `build_scoped_store`) sees the
effective, self-contained value — in particular a scoped store materializes
expanded text for a cacheable expansion, keeping the container bounded to its
declared secrets with no knowledge of the references. an expansion whose
reference chain reaches a minting source is the exception: freezing it would
strand the session on an expired value, so the scoped store ships the winning
source's raw reference-preserving text instead and the session re-expands —
and re-mints — per read; every referenced name must then be a kind hydrated
into the scoped set (`build_scoped_store`). a reference that does not resolve,
a malformed node, an absent field, or a reference cycle raises.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import sys
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, Optional, Protocol

from bro.base import configs, log, template
from bro.base.args import Parser
from bro.base.condition import StringVariable

__cli_name__ = 'credentials'

# local search roots, highest priority first: the explicit service config dir
# when set, then the standalone `~/.bro` host store. module-level so tests can
# point them at tmp dirs, and read at fetch time so the overrides take effect.
CONFIGS_DIR = configs.BRO_CONFIGS_DIR
BRO_DIR = configs.DEFAULT_BRO_DIR

# a generated registry file (emitted by `build_scoped_store`) overrides the
# built-in default when present; absent, resolution falls through to the built-in.
REGISTRY_FILE = 'credentials.json'

# the process-scoped registry override described in the module docstring.
REGISTRY_ENV = 'CREDENTIALS_REGISTRY'

# the host-local additions file, searched along the resolver's local path and
# merged per-name over the built-in registry (`host_registry`) — unlike a
# generated REGISTRY_FILE, which replaces the registry wholesale to bound it.
HOST_REGISTRY_FILE = 'registry.json'

_CREDENTIAL_SOURCE_GROUP = 'bro.credential_sources'
_CREDENTIAL_REGISTRY_GROUP = 'bro.credentials'


def _entry_points(group: str) -> tuple[importlib.metadata.EntryPoint, ...]:
  return tuple(importlib.metadata.entry_points(group=group))


def _entry_point(group: str, name: str) -> Optional[importlib.metadata.EntryPoint]:
  matches = [entry_point for entry_point in _entry_points(group) if entry_point.name == name]
  if len(matches) > 1:
    values = ', '.join(entry_point.value for entry_point in matches)
    raise ValueError(f'duplicate {group} entry point {name!r}: {values}')
  return matches[0] if len(matches) == 1 else None


# a secret name: `kind` or `kind+instance`. the charsets keep every name safe
# to splice into the single-quoted insert slot of an install-hook template, and
# safe to type unquoted in a shell.
_NAME_GRAMMAR = re.compile(r'([a-z0-9_]+)(?:\+([a-z0-9_-]+))?')


def parse_name(name: str) -> tuple[str, Optional[str]]:
  """split a secret name into (kind, instance); a plain name is its own kind
  with no instance. raises on a name outside the grammar."""
  match = _NAME_GRAMMAR.fullmatch(name)
  if match is None:
    raise ValueError(f'malformed secret name {name!r}; expected kind or kind+instance')
  return match.group(1), match.group(2)


def _require_kind(name: str) -> None:
  """gate of the kind-addressed read family: the name must be a kind alone,
  with no instance part."""
  kind, instance = parse_name(name)
  if instance is not None:
    raise ValueError(
      f'secret {name!r} names a storage instance; this read is kind-addressed — '
      f'read the kind {kind!r}, or address storage via the get_instance family '
      f'(--instance in the CLI)'
    )


# the reference-node keys of a json secret (module docstring): `$cred` names the
# referenced secret, `field` optionally picks one top-level field of its object.
_REFERENCE_KEY = '$cred'
_REFERENCE_FIELD = 'field'


class SecretNotFound(Exception):
  """no source yielded a value for the named secret."""

  def __init__(self, name: str):
    super().__init__(f'secret {name!r} not found')
    self.name = name


class Source(Protocol):
  """a place a secret's raw text might live: the source lifecycle contract.

  a source either stores durable text (its fetch retrieves the value as-is) or
  derives short-lived values from stored material (`MintingSource`). the
  contract covers the three lifecycle points the store and the scoped-store
  build need: producing the value (`fetch`), whether the produced value may be
  memoized for the process (`CACHEABLE`), and how the source travels into a
  bounded per-session store (`materialize_scoped`)."""

  # whether the store may cache a fetched value for its lifetime; a source that
  # mints short-lived values declares False and owns its own refresh instead
  CACHEABLE: ClassVar[bool]

  def fetch(self) -> Optional[str]:
    """return the raw text, or None when this source doesn't have it (try the next)."""
    ...

  def materialize_scoped(self, file: str, value: str) -> tuple[dict, bytes]:
    """the source's scoped-store representation: the registry source entry and
    the bytes of the `file` it points at. `value` is the text to embed — the
    expanded host-resolved value, or the source's raw reference-preserving text
    when the resolve was un-cacheable (`build_scoped_store` picks). a stored
    source lands as a plain local entry over that value — the session never
    learns the host source type; a minting source ships its own config instead,
    so the session derives fresh values on read."""
    ...


class LocalSource:
  """reads `file` from the local search path (`_find_in_search_dirs`)."""

  TYPE = 'local'
  CACHEABLE: ClassVar[bool] = True

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

  def materialize_scoped(self, file: str, value: str) -> tuple[dict, bytes]:
    return {'file': file}, value.encode()

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
  CACHEABLE: ClassVar[bool] = True

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

  def materialize_scoped(self, file: str, value: str) -> tuple[dict, bytes]:
    return {'file': file}, value.encode()

  @classmethod
  def from_dict(cls, data: dict) -> SSMSource:
    return cls(data['parameter'], data['region'])


@dataclass(frozen=True)
class Minted:
  """one minted value with its expiry (timezone-aware)."""

  value: str
  expires_at: datetime


class MintingSource(ABC):
  """a source that derives short-lived values from a minting config file on the
  local search path: a self-contained json object holding whatever material the
  concrete type's `mint` needs. the source keeps the minted value with its
  expiry and re-mints once less than `EXPIRY_MARGIN` remains, so a caller that
  just resolved the secret gets at least that window to use the value. scoped
  stores ship the config file itself (`materialize_scoped`), so a bounded
  session re-derives fresh values on read.

  a concrete type names its registry `TYPE`, implements `mint` (validating its
  own config fields), and registers that type in `bro.credential_sources`."""

  TYPE: ClassVar[str]
  CACHEABLE: ClassVar[bool] = False

  # re-mint threshold: the remaining lifetime below which the held value is stale
  EXPIRY_MARGIN = timedelta(minutes=5)

  def __init__(self, file: str):
    self.file = file
    self._minted: Optional[Minted] = None

  @abstractmethod
  def mint(self, config: dict) -> Minted:
    """derive a fresh value from the parsed minting config."""
    ...

  def fetch(self) -> Optional[str]:
    config = self.config()
    if config is None:
      return None
    if self._minted is None or datetime.now(UTC) >= self._minted.expires_at - self.EXPIRY_MARGIN:
      self._minted = self.mint(config)
    return self._minted.value

  def config(self) -> Optional[dict]:
    """the parsed minting config, or None when the file is absent along the
    search path. a local read — nothing is minted."""
    text = self._config_text()
    if text is None:
      return None
    return self._parse_config(text)

  def materialize_scoped(self, file: str, value: str) -> tuple[dict, bytes]:
    text = self._config_text()
    if text is None:
      raise ValueError(f'{self.TYPE} config {self.file!r} disappeared during hydration')
    return {'type': self.TYPE, 'file': file}, text.encode()

  def _config_text(self) -> Optional[str]:
    path = _find_in_search_dirs(self.file)
    if path is None:
      return None
    return path.read_text()

  def _parse_config(self, text: str) -> dict:
    try:
      config = json.loads(text)
    except json.JSONDecodeError as e:
      raise ValueError(f'{self.TYPE} config {self.file!r} is not valid json') from e
    if not isinstance(config, dict):
      raise ValueError(f'{self.TYPE} config {self.file!r} is not a json object')
    return config

  @classmethod
  def from_dict(cls, data: dict) -> MintingSource:
    return cls(data['file'])


def _search_dirs() -> list[str]:
  # an explicit CREDENTIALS_REGISTRY carries its sibling files: a scoped store is
  # a registry plus the `{name}.cred` files it points at, materialized in one dir
  # (`build_scoped_store`), so that dir must be searched first for the store to
  # resolve wherever it lands — the container's ~/.bro needs no override, a host
  # session's store lives outside the standard dirs.
  directories: list[str] = []
  override = os.environ.get(REGISTRY_ENV)
  if override is not None and override != '':
    directories.append(str(Path(override).parent))
  if CONFIGS_DIR is not None:
    directories.append(CONFIGS_DIR)
  directories.append(BRO_DIR)
  return directories


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


def _referenced_names(text: str) -> set[str]:
  """every `$cred` target named by a json secret's reference nodes; empty for
  non-json text, which cannot carry references."""

  def walk(node: Any) -> set[str]:
    if isinstance(node, dict):
      if _REFERENCE_KEY in node:
        return {node[_REFERENCE_KEY]}
      return {name for value in node.values() for name in walk(value)}
    if isinstance(node, list):
      return {name for item in node for name in walk(item)}
    return set()

  try:
    tree = json.loads(text)
  except json.JSONDecodeError:
    return set()
  return walk(tree)


def _source_from_dict(data: dict) -> Source:
  """reconstruct a Source from its `type` discriminator; `type` defaults to local."""
  type_name = data.get('type', LocalSource.TYPE)
  if type_name == LocalSource.TYPE:
    return LocalSource.from_dict(data)
  if type_name == SSMSource.TYPE:
    return SSMSource.from_dict(data)
  entry_point = _entry_point(_CREDENTIAL_SOURCE_GROUP, type_name)
  if entry_point is None:
    known = sorted(
      {LocalSource.TYPE, SSMSource.TYPE}
      | {item.name for item in _entry_points(_CREDENTIAL_SOURCE_GROUP)}
    )
    raise ValueError(f'unknown credential source type {type_name!r}; known: {known}')
  source_class = entry_point.load()
  if not isinstance(source_class, type) or not issubclass(source_class, MintingSource):
    raise TypeError(
      f'{_CREDENTIAL_SOURCE_GROUP} entry point {type_name!r} must load a MintingSource class'
    )
  return source_class.from_dict(data)


class Secret:
  """one named credential: an ordered source list (the order is the resolution
  priority). the resolver treats the value as an opaque text blob — callers pick
  the shape, `get()` for the raw text or `get_json()` to parse it as a json object.

  `install` is an optional shell hook that wires the secret into the tool that
  consumes it from *outside* the resolver (git, the aws CLI, ...). The registry
  declares it as a `bro.base.template` text over the `#name` variable —
  `credentials get '{{insert #name}}'` — so one kind-level hook serves every
  instance of the kind. The raw template is kept (`install_template`) and
  rendered per name by `install_for`, because the name an entry resolves under
  is not always its own: a scoped store materializes a variant under its kind
  name. `install` is the hook rendered with the secret's own name, computed
  eagerly so a malformed template fails at registry load. The container
  entrypoint `eval`s the hook after hydration; it pulls the value via
  `credentials get` at eval time, so per-secret wiring lives in the registry
  with no interpolated path and the entrypoint stays generic."""

  def __init__(self, name: str, sources: Sequence[Source], *, install: Optional[str] = None):
    self.name = name
    self.sources = sources
    self.install_template = install
    self.install = self.install_for(name)

  def install_for(self, name: str) -> Optional[str]:
    """the install hook rendered with `#name` bound to `name`, or None when the
    secret declares no hook."""
    if self.install_template is None:
      return None
    return template.render(self.install_template, {'name': StringVariable(name)})

  @classmethod
  def from_dict(cls, name: str, data: dict) -> Secret:
    install = data.get('install')
    if install is not None and not isinstance(install, str):
      raise ValueError(
        f'secret {name!r}: install must be a string — file references resolve only '
        'in the built-in registry'
      )
    return cls(name, [_source_from_dict(s) for s in data['sources']], install=install)


def _parse_json_object(name: str, raw: str) -> dict:
  try:
    value = json.loads(raw)
  except json.JSONDecodeError as e:
    raise ValueError(f'secret {name!r} is not valid json') from e
  if not isinstance(value, dict):
    raise ValueError(f'secret {name!r} is not a json object')
  return value


class Store:
  """resolves secrets against a registry, caching resolved values for its
  lifetime — except values a source declares un-cacheable (`Source.CACHEABLE`,
  e.g. a minted short-lived token): those re-fetch on every read, the source
  owning its own refresh, and a secret whose reference expansion embedded one
  is re-expanded per read for the same reason. a json secret's `{"$cred": ...}`
  reference nodes are expanded during the resolve (module docstring), so cached
  and returned values are always the effective, self-contained text."""

  def __init__(self, registry: dict[str, Secret]):
    self._registry = registry
    self._cache: dict[str, str] = {}
    self._winners: dict[str, Source] = {}
    self._lock = threading.Lock()

  def try_get(self, name: str) -> Optional[str]:
    """kind-addressed `try_get_instance`: resolve a kind to its raw text
    (stripped), or None when no source yields a value — the non-raising
    primitive, for callers that treat a missing secret as an expected case.
    `get` is the strict wrapper that raises on None. a malformed value (a broken
    reference, a non-UTF-8 file) still raises: absence is expected, corruption
    is not."""
    _require_kind(name)
    return self.try_get_instance(name)

  def try_get_instance(self, name: str) -> Optional[str]:
    """storage-addressed read: resolve the registry entry stored under `name` —
    plain (the kind's default instance) or `kind+instance` — to its raw text
    (stripped), or None when no source yields a value. `get_instance` is the
    raising wrapper."""
    parse_name(name)
    resolved = self.resolve(name)
    return resolved[0] if resolved is not None else None

  def resolve(self, name: str) -> Optional[tuple[str, bool]]:
    """resolve a secret by its stored registry name to (value, cacheable), or
    None when no source yields a value. cacheable is False when the winning
    source — or any source behind the expanded references — declares its values
    un-cacheable, i.e. a later read may observe a different (re-derived) value;
    `try_get_instance`/`get_instance` are the value-only spellings."""
    # one lock around the whole resolve: a secret is fetched at most once even
    # under concurrent callers, and the store is read only a handful of times per
    # process (each value cached on first read), so a lock-free fast path buys
    # nothing.
    with self._lock:
      return self._resolve(name, chain=())

  def _resolve(self, name: str, chain: tuple[str, ...]) -> Optional[tuple[str, bool]]:
    """fetch, expand, and cache one secret; `chain` is the stack of secrets whose
    expansions are in progress, for cycle detection. returns (value, cacheable) —
    cacheable is False when the winning source, or any source behind the expanded
    references, declares its values un-cacheable. callers hold the lock."""
    cached = self._cache.get(name)
    if cached is not None:
      return cached, True
    secret = self._registry.get(name)
    if secret is None:
      return None
    for source in secret.sources:
      raw = source.fetch()
      if raw is not None:
        self._winners[name] = source
        value, references_cacheable = self._expand_references(raw.strip(), (*chain, name))
        cacheable = source.CACHEABLE and references_cacheable
        if cacheable:
          self._cache[name] = value
        return value, cacheable
    return None

  def winning_source(self, name: str) -> Source:
    """the source that produced `name`'s value in this store — recorded by the
    resolve, so call it only after a successful `get`/`try_get`; raises KeyError
    before one."""
    with self._lock:
      return self._winners[name]

  def sources(self, name: str) -> Sequence[Source]:
    """the ordered source list registered under `name`, empty when the registry
    lacks the name. a registry read — nothing is fetched or minted."""
    secret = self._registry.get(name)
    return secret.sources if secret is not None else ()

  def _expand_references(self, text: str, chain: tuple[str, ...]) -> tuple[str, bool]:
    """substitute every reference node in a json secret's tree; text that isn't
    json, or json with no reference nodes, passes through byte-identical. also
    returns whether every referenced secret allowed caching (vacuously True for
    text with no references)."""
    try:
      tree = json.loads(text)
    except json.JSONDecodeError:
      return text, True
    if not _contains_reference(tree):
      return text, True
    referenced_cacheable: list[bool] = []
    substituted = self._substitute_references(tree, chain, referenced_cacheable)
    return json.dumps(substituted), all(referenced_cacheable)

  def _substitute_references(
    self, node: Any, chain: tuple[str, ...], referenced_cacheable: list[bool]
  ) -> Any:
    if isinstance(node, dict):
      if _REFERENCE_KEY in node:
        return self._referenced_value(node, chain, referenced_cacheable)
      return {
        key: self._substitute_references(value, chain, referenced_cacheable)
        for key, value in node.items()
      }
    if isinstance(node, list):
      return [self._substitute_references(item, chain, referenced_cacheable) for item in node]
    return node

  def _referenced_value(
    self, node: dict, chain: tuple[str, ...], referenced_cacheable: list[bool]
  ) -> Any:
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
    resolved = self._resolve(target, chain)
    if resolved is None:
      raise ValueError(f'secret {referrer!r} references {target!r}, which does not resolve')
    value, cacheable = resolved
    referenced_cacheable.append(cacheable)
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
    """kind-addressed `get_instance`: resolve a kind to its raw text, raising
    `SecretNotFound` when no source yields a value."""
    _require_kind(name)
    return self.get_instance(name)

  def get_instance(self, name: str) -> str:
    """storage-addressed `try_get_instance` that raises `SecretNotFound` when no
    source yields a value."""
    value = self.try_get_instance(name)
    if value is not None:
      return value
    raise SecretNotFound(name)

  def available(self, name: str) -> bool:
    """whether the kind `name` resolves in this store. the predicate behind both
    the runtime capability gate and the credential template directives
    (llm/mcp.py)."""
    return self.try_get(name) is not None

  def known_names(self) -> frozenset[str]:
    """every secret name this store's registry knows, resolvable or not."""
    return frozenset(self._registry)

  def get_json(self, name: str) -> dict:
    """kind-addressed `get` parsed as a json object. raises if the text isn't
    valid json or isn't an object (e.g. a scalar token)."""
    return _parse_json_object(name, self.get(name))

  def get_instance_json(self, name: str) -> dict:
    """storage-addressed `get_instance` parsed as a json object."""
    return _parse_json_object(name, self.get_instance(name))


# every secret the project knows about, in the same shape as a generated
# `credentials.json` so it is constructed the same way (via `Secret.from_dict`).
_BUILTIN_REGISTRY_PATH = Path(__file__).with_name('registry.json')


def _contributed_registry_data() -> dict[str, dict]:
  data: dict[str, dict] = {}
  for entry_point in _entry_points(_CREDENTIAL_REGISTRY_GROUP):
    if entry_point.name in data:
      raise ValueError(f'duplicate {_CREDENTIAL_REGISTRY_GROUP} entry point {entry_point.name!r}')
    entry = entry_point.load()
    if not isinstance(entry, dict):
      raise TypeError(
        f'{_CREDENTIAL_REGISTRY_GROUP} entry point {entry_point.name!r} must load a dict'
      )
    data[entry_point.name] = dict(entry)
  return data


def _builtin_registry_data() -> dict:
  """the built-in registry merged with installed credential contributions.

  An `install` of the form `{"file": "<path>"}` loads a built-in hook relative
  to the registry. Contributed and generated registries carry hooks as strings.
  """
  data = json.loads(_BUILTIN_REGISTRY_PATH.read_text())
  for entry in data.values():
    install = entry.get('install')
    if isinstance(install, dict):
      entry['install'] = (_BUILTIN_REGISTRY_PATH.parent / install['file']).read_text().rstrip('\n')
  data.update(_contributed_registry_data())
  return data


def _registry_from_dict(data: dict) -> dict[str, Secret]:
  return {name: Secret.from_dict(name, spec) for name, spec in data.items()}


def _resolve_kinds(data: dict) -> dict:
  """validate every name against the grammar, resolve each kind entry's
  `instance` selector (`_select_default_instance`), and give each
  `kind+instance` variant its kind entry's install-hook template (instantiated
  per-entry by `Secret.from_dict`). the kind owns kind-level behavior, so a
  variant carrying its own `install` or `instance` — or naming a kind the
  registry lacks — is an error. only the built-in/host registries pass through
  here: a generated registry is self-contained, its variant entries already
  carrying their materialized hooks.
  """
  resolved: dict[str, dict] = {}
  for name, entry in data.items():
    kind, instance = parse_name(name)
    if instance is None:
      resolved[name] = _select_default_instance(name, entry, data) if 'instance' in entry else entry
      continue
    if 'install' in entry:
      raise ValueError(f'variant {name!r} declares an install hook; the kind entry owns it')
    if 'instance' in entry:
      raise ValueError(
        f'variant {name!r} declares an instance selector; only a kind entry selects '
        'its default instance'
      )
    kind_entry = data.get(kind)
    if kind_entry is None:
      raise ValueError(f'variant {name!r} has no kind entry {kind!r} in the registry')
    install = kind_entry.get('install')
    resolved[name] = entry if install is None else {**entry, 'install': install}
  return resolved


def _select_default_instance(kind: str, entry: dict, data: dict) -> dict:
  """resolve a kind entry's `instance` selector: `{"instance": "acme"}` makes
  `kind+acme` the kind's default instance — the kind entry borrows that
  variant's sources, keeping its own install hook."""
  instance = entry['instance']
  if not isinstance(instance, str):
    raise ValueError(f'kind {kind!r}: instance selector must be a string, got {instance!r}')
  if 'sources' in entry:
    raise ValueError(f'kind {kind!r} declares both an instance selector and its own sources')
  variant = f'{kind}+{instance}'
  parse_name(variant)
  variant_entry = data.get(variant)
  if variant_entry is None:
    raise ValueError(
      f'kind {kind!r} selects instance {instance!r}, but {variant!r} is not in the registry'
    )
  selected: dict = {'sources': variant_entry['sources']}
  install = entry.get('install')
  if install is not None:
    selected['install'] = install
  return selected


def default_registry() -> dict[str, Secret]:
  """the framework registry merged with installed credential contributions."""
  return _registry_from_dict(_resolve_kinds(_builtin_registry_data()))


def host_registry() -> dict[str, Secret]:
  """the built-in registry merged per-name with the host-local additions file —
  entries that never enter the repo: variants of a checked-in kind, or the
  host's sources for a kind whose checked-in entry declares none. an addition
  that doesn't declare `install` inherits the built-in entry's hook, so a
  sources-only override keeps the kind's checked-in wiring. the additions file
  follows the local search path, like any secret file. kind resolution runs
  after the merge, so a variant picks up its kind's hook even when an addition
  overrides the kind, and after `select_instances`, whose selections replace the
  sources of the kinds they name."""
  data = _builtin_registry_data()
  additions_path = _find_in_search_dirs(HOST_REGISTRY_FILE)
  if additions_path is not None:
    for name, entry in json.loads(additions_path.read_text()).items():
      builtin = data.get(name)
      if builtin is not None and 'install' in builtin and 'install' not in entry:
        entry = {**entry, 'install': builtin['install']}
      data[name] = entry
  for kind, instance in _selected_instances.items():
    data[kind] = _selected_entry(kind, instance, data)
  return _registry_from_dict(_resolve_kinds(data))


def _selected_entry(kind: str, instance: Optional[str], data: dict) -> dict:
  """the kind's entry under a `select_instances` binding: an instance replaces
  the kind's sources with a selector `_select_default_instance` then follows,
  while None reads the entry's own sources — which a kind entry selecting an
  instance of its own does not have."""
  entry = data.get(kind)
  if entry is None:
    raise ValueError(f'instance selected for kind {kind!r}, which the registry does not carry')
  without_selection = {key: value for key, value in entry.items() if key != 'instance'}
  if instance is None:
    if 'sources' not in without_selection:
      raise ValueError(
        f'kind {kind!r} is selected as its own entry, but the registry gives it no sources '
        'of its own — name an instance instead'
      )
    return without_selection
  without_selection.pop('sources', None)
  return {**without_selection, 'instance': instance}


# the process-scoped instance selection layered onto the host registry by
# `select_instances`; empty until a caller binds one.
_selected_instances: dict[str, Optional[str]] = {}


def select_instances(selection: dict[str, Optional[str]]) -> None:
  """bind this process's resolution to one instance per named kind: `{'brog':
  'github'}` makes `brog+github` the kind's default here, as a registry
  kind-entry `instance` selector does durably for the whole host, and a None
  value names the kind's own entry, which the registry must then give sources of
  its own.

  the binding replaces any earlier one and drops the cached default store, so
  every later read — a kind-addressed `get`, a capability probe, a scoped-store
  build — sees the selected instances. it reaches only the host registry: a
  generated registry (a session's scoped store) is self-contained and already
  carries the instance its launch selected."""
  global _selected_instances, _default_store
  for kind, instance in selection.items():
    if parse_name(kind)[1] is not None:
      raise ValueError(f'instance selection is keyed by kind alone, got {kind!r}')
    if instance is not None:
      parse_name(f'{kind}+{instance}')
  with _default_store_lock:
    _selected_instances = dict(selection)
    _default_store = None


def _load_registry() -> dict[str, Secret]:
  # CREDENTIALS_REGISTRY points the process at an explicit registry file, above
  # every other source of one — for a run that must resolve against a specific
  # registry, such as an integration harness using a service-specific registry. a bad path raises rather than falling through: an
  # explicit override that silently degraded to the built-in would resolve
  # against the wrong secret set.
  override = os.environ.get(REGISTRY_ENV)
  if override is not None and override != '':
    return _registry_from_dict(json.loads(Path(override).read_text()))
  # a generated registry file in either search dir (`BRO_CONFIGS_DIR` for a
  # deployed service, `~/.bro` for a scoped per-container store) overrides the
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
  """resolve a kind to its raw text via the process-wide default store."""
  return default_store().get(name)


def try_get(name: str) -> Optional[str]:
  """resolve a kind to its raw text via the process-wide default store, or None
  when no source yields a value — the non-raising sibling of `get`."""
  return default_store().try_get(name)


def get_json(name: str) -> dict:
  """resolve a kind and parse it as a json object via the process-wide default store."""
  return default_store().get_json(name)


def available(name: str) -> bool:
  """whether the kind `name` resolves in the process-wide default store, without
  raising."""
  return default_store().available(name)


def known_names() -> frozenset[str]:
  """every secret name the process-wide default store's registry knows."""
  return default_store().known_names()


def _require_one_instance_per_kind(names: Iterable[str]) -> None:
  by_kind: dict[str, list[str]] = {}
  for name in sorted(set(names)):
    kind, _ = parse_name(name)
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
  hydrated secret holding its resolved raw text (or, for a minting-sourced
  secret, its minting config, so the session mints fresh tokens on read), plus a
  generated `credentials.json` registry covering exactly those secrets and
  pointing each at its `{name}.cred`. materialising this map as the container's
  `~/.bro` then bounds the container to this set; any other secret resolves to a
  clean `SecretNotFound`. The bytes never touch a host file — `cw` packs them
  into a tar and `docker cp`s them straight into the container.

  every secret resolves fully on the host first — launch-time validation stays
  strict, and a minting chain mints once here, failing loudly before the session
  exists. resolution — reference expansion included — runs against the registry
  overlaid with the scope's own kind binding, so a kind-level `$cred` node
  targeting a kind in scope picks up the launch-selected instance (the value the
  session's own `.cred` for that kind carries), while one outside the scope
  falls through to the registry's own entry. a cacheable resolve embeds the
  expanded, self-contained value. an un-cacheable one — the winning source, or a
  `$cred` reference chain, reaching a minting source — must not freeze a
  short-lived value into the store, so the winning source materializes its raw
  text with references intact and the session re-expands (and re-mints) per read
  against the scoped registry. each such reference must be spelled at kind level
  and land on a kind hydrated into the scoped set (the scoped namespace is
  kinds-only) — one outside the scope fails the build.

  a `kind+instance` name materializes under its kind name: the scoped registry
  entry and its `.cred` file are named by the kind, hydrated from the variant's
  sources, with the install hook rendered for the kind name. The scoped
  namespace is therefore kinds-only by construction — readers, `available()`,
  `known_names()` all see the kind, and never need to know which instance the
  launch selected.

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
  `github` and `github+alice`, in whichever tiers — raises `ValueError`. The
  check runs over the declared union up front, so an unresolvable optional name
  cannot flap the outcome.
  """
  registry = _load_registry()
  selection = _scoped_selection(set(names), set(optional), registry)
  store = Store({**registry, **{parse_name(name)[0]: secret for name, secret, _ in selection}})
  files: dict[str, bytes] = {}
  scoped: dict[str, dict] = {}
  shipped_references: dict[str, set[str]] = {}

  def materialize(name: str, value: str, cacheable: bool, secret: Secret) -> None:
    # resolve generically on the host (doubling as launch-time validation), then
    # let the winning source pick its scoped representation under a uniform
    # `{kind}.cred` — Source.materialize_scoped owns the per-source semantics
    kind, _ = parse_name(name)
    file = f'{kind}.cred'
    source = store.winning_source(name)
    if not cacheable:
      # an expansion that reached a minting source: materialize the winning
      # source's raw text, references intact, so the session re-expands — and
      # re-mints — per read; _require_references_in_scope covers the references
      raw = source.fetch()
      if raw is None:
        raise ValueError(f'secret {name!r} disappeared during hydration')
      value = raw.strip()
      shipped_references[name] = _referenced_names(value)
    entry_source, content = source.materialize_scoped(file, value)
    files[file] = content
    entry: dict = {'sources': [entry_source]}
    install = secret.install_for(kind)
    if install is not None:
      entry['install'] = install
    scoped[kind] = entry

  for name, secret, required in selection:
    resolved = store.resolve(name)
    if resolved is None:
      if required:  # strict: a declared name with no value fails the build
        raise SecretNotFound(name)
      log.debug('optional secret %r unresolvable; skipping', name)
      continue
    value, cacheable = resolved
    materialize(name, value, cacheable, secret)
  _require_references_in_scope(shipped_references, set(scoped))
  files[REGISTRY_FILE] = json.dumps(scoped).encode()
  return files


def _scoped_selection(
  required: set[str], optional: set[str], registry: dict[str, Secret]
) -> list[tuple[str, Secret, bool]]:
  """the (name, entry, required) rows a finalized scope selects from a registry,
  validated for the kinds-only session namespace: at most one instance per kind
  across both tiers, an unknown required name raises, an unknown optional name
  is skipped."""
  _require_one_instance_per_kind(required | optional)
  selection: list[tuple[str, Secret, bool]] = []
  for name in sorted(required):
    secret = registry.get(name)
    if secret is None:
      raise ValueError(f'unknown secret {name!r} declared in manifest; not in the registry')
    selection.append((name, secret, True))
  for name in sorted(optional - required):
    secret = registry.get(name)
    if secret is None:
      log.debug('optional secret %r not in the registry; skipping', name)
      continue
    selection.append((name, secret, False))
  return selection


def scoped_view_store(names: Iterable[str], *, optional: Iterable[str] = ()) -> Store:
  """a lazy, kinds-only Store over a finalized scope: each selected name's
  registry entry keyed by its kind, so a `kind+instance` selection reads under
  the kind name — the namespace a session's materialized store serves — and a
  read returns the value hydration would materialize from that entry.

  nothing is fetched or minted up front: each read resolves on demand through
  the entry's own sources, and a name that cannot resolve surfaces as
  `SecretNotFound` at that read rather than failing a launch. registry-level
  strictness matches `build_scoped_store` (`_scoped_selection`). for host-side
  code that reads a credential on a launch's behalf without hydrating the
  session's store.
  """
  selection = _scoped_selection(set(names), set(optional), _load_registry())
  return Store({parse_name(name)[0]: secret for name, secret, _ in selection})


def _require_references_in_scope(shipped: dict[str, set[str]], scoped_kinds: set[str]) -> None:
  """reject a reference-preserving materialization whose in-session expansion
  could not resolve: every `$cred` target of a raw-shipped secret must name a
  kind hydrated into the scoped set (the scoped namespace is kinds-only)."""
  for name in sorted(shipped):
    for reference in sorted(shipped[name]):
      kind, instance = parse_name(reference)
      if instance is not None:
        raise ValueError(
          f'secret {name!r} ships reference-preserving text; reference {reference!r} '
          f'must be spelled at kind level ({kind!r}) — the scoped namespace is kinds-only'
        )
      if kind not in scoped_kinds:
        raise ValueError(
          f'secret {name!r} ships reference-preserving text; referenced kind '
          f'{reference!r} is not in the scoped set'
        )


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


def _get(name: str, field: Optional[str], as_json: bool, instance: bool) -> Optional[int]:
  store = default_store()
  try:
    # a bare get prints the raw text; --field / --json need the parsed object.
    if field is None and not as_json:
      print(store.get_instance(name) if instance else store.get(name))
      return None
    data = store.get_instance_json(name) if instance else store.get_json(name)
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


def _list_available(instance: bool) -> None:
  store = default_store()
  for name in sorted(store.known_names()):
    if instance:
      if store.try_get_instance(name) is not None:
        print(name)
    elif parse_name(name)[1] is None and store.available(name):
      print(name)


def _print_hooks() -> None:
  print(install_hooks())


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description='resolve credentials from the default store')
  subparser = parser.add_subparsers(dest='action', required=True)
  get_parser = subparser.add_parser('get', help='resolve a secret and print it')
  get_parser.add_argument(
    'name',
    help='secret kind (e.g. anthropic, notion); with --instance, a storage name '
    '(kind or kind+instance)',
  )
  get_parser.add_argument('--field', help='for a json secret, print only this field')
  get_parser.add_argument(
    '--json', dest='as_json', action='store_true', help='parse as json and pretty-print (indent=2)'
  )
  get_parser.add_argument(
    '--instance',
    '-i',
    action='store_true',
    help='address the registry by storage name instead of kind',
  )
  get_parser.set_handler(_get)
  list_parser = subparser.add_parser(
    'list', help='list credential kinds that resolve in the default store'
  )
  list_parser.add_argument(
    '--instance',
    '-i',
    action='store_true',
    help='list resolvable storage names (kind+instance entries included) instead of kinds',
  )
  list_parser.set_handler(_list_available)
  subparser.add_parser(
    'install-hooks', help='print shell install hooks for the container entrypoint to eval'
  ).set_handler(_print_hooks)
  return parser.dispatch(argv)
