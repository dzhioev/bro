#!/usr/bin/env python
"""Resolve credentials from one convention-based store.

The code registry declares credential kinds, descriptions, and optional install
hooks.
A store supplies material independently:
`creds/<name>.cred` is the material for a kind or `kind+instance`, while
`creds.json` optionally annotates a name with one typed source.
`BRO_STORE` selects the process store and otherwise defaults to `~/.bro`.

Kind-addressed reads apply the store's explicit kind-to-instance selection.
Storage-addressed reads use the name exactly as written.
A JSON value may contain `{"$cred": "<name>"}` reference nodes, optionally with
`"field": "<key>"`.
A kind-spelled reference applies the same selection and an instance-spelled
reference reads that stored instance directly.
References are expanded before values reach consumers.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import shlex
import shutil
import sys
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, Optional, Protocol

from bro.base import configs, log, template
from bro.base.args import Parser
from bro.base.condition import StringVariable

__cli_name__ = 'credentials'

STORE_DIR = configs.STORE_DIR
SOURCES_FILE = 'creds.json'
MATERIAL_DIR = 'creds'
MATERIAL_SUFFIX = '.cred'
MINTED_SUFFIX = '.minted'

LEGACY_REGISTRY_ENV = 'CREDENTIALS_REGISTRY'
LEGACY_CONFIGS_ENV = 'BRO_CONFIGS_DIR'
LEGACY_REGISTRY_FILE = 'registry.json'
LEGACY_CREDENTIALS_FILE = 'credentials.json'

_CREDENTIAL_SOURCE_GROUP = 'bro.credential_sources'
_CREDENTIAL_REGISTRY_GROUP = 'bro.credentials'
_BUILTIN_REGISTRY_PATH = Path(__file__).with_name('registry.json')


def _entry_points(group: str) -> tuple[importlib.metadata.EntryPoint, ...]:
  return tuple(importlib.metadata.entry_points(group=group))


def _entry_point(group: str, name: str) -> Optional[importlib.metadata.EntryPoint]:
  matches = [entry_point for entry_point in _entry_points(group) if entry_point.name == name]
  if len(matches) > 1:
    values = ', '.join(entry_point.value for entry_point in matches)
    raise ValueError(f'duplicate {group} entry point {name!r}: {values}')
  return matches[0] if len(matches) == 1 else None


_NAME_GRAMMAR = re.compile(r'([a-z0-9_]+)(?:\+([a-z0-9_-]+))?')


def parse_name(name: str) -> tuple[str, Optional[str]]:
  match = _NAME_GRAMMAR.fullmatch(name)
  if match is None:
    raise ValueError(f'malformed secret name {name!r}; expected kind or kind+instance')
  return match.group(1), match.group(2)


def _require_kind(name: str) -> None:
  kind, instance = parse_name(name)
  if instance is not None:
    raise ValueError(
      f'secret {name!r} names a storage instance; this read is kind-addressed — '
      f'read the kind {kind!r}, or address storage via the get_instance family '
      f'(--instance in the CLI)'
    )


_REFERENCE_KEY = '$cred'
_REFERENCE_FIELD = 'field'


class SecretNotFound(Exception):
  def __init__(self, name: str):
    super().__init__(f'secret {name!r} not found')
    self.name = name


@dataclass(frozen=True)
class _Unresolved:
  name: str


_Resolution = tuple[str, bool] | _Unresolved


class Source(Protocol):
  """One source annotation bound to a convention-named material path by Store."""

  CACHEABLE: ClassVar[bool]

  def fetch(self, material_path: Path) -> Optional[str]: ...

  def materialize_scoped(self, material_path: Path, value: str) -> tuple[Optional[dict], bytes]: ...


class LocalSource:
  """Read the convention-named material file."""

  TYPE = 'local'
  CACHEABLE: ClassVar[bool] = True

  def fetch(self, material_path: Path) -> Optional[str]:
    if not material_path.is_file():
      return None
    try:
      text = material_path.read_text()
    except UnicodeDecodeError as error:
      raise ValueError(f'credential file {material_path} is not valid UTF-8 text') from error
    if '\x00' in text:
      raise ValueError(f'credential file {material_path} contains null bytes')
    return text

  def materialize_scoped(self, material_path: Path, value: str) -> tuple[Optional[dict], bytes]:
    return None, value.encode()

  @classmethod
  def from_dict(cls, data: dict) -> LocalSource:
    if len(data) > 0:
      raise ValueError(f'local credential source declares unknown fields: {sorted(data)}')
    return cls()


class SSMSource:
  """Read one decrypted AWS SSM parameter."""

  TYPE = 'ssm'
  CACHEABLE: ClassVar[bool] = True

  def __init__(self, parameter: str, region: Optional[str] = None):
    if not isinstance(parameter, str) or parameter == '':
      raise ValueError('ssm credential source parameter must be a non-empty string')
    if region is not None and (not isinstance(region, str) or region == ''):
      raise ValueError('ssm credential source region must be a non-empty string')
    self.parameter = parameter
    self.region = region

  def fetch(self, material_path: Path) -> Optional[str]:
    import boto3

    client = boto3.client('ssm', region_name=self.region)
    try:
      response = client.get_parameter(Name=self.parameter, WithDecryption=True)
    except client.exceptions.ParameterNotFound:
      return None
    return response['Parameter']['Value']

  def materialize_scoped(self, material_path: Path, value: str) -> tuple[Optional[dict], bytes]:
    return None, value.encode()

  @classmethod
  def from_dict(cls, data: dict) -> SSMSource:
    unknown = sorted(set(data) - {'parameter', 'region'})
    if len(unknown) > 0:
      raise ValueError(f'ssm credential source declares unknown fields: {unknown}')
    if 'parameter' not in data:
      raise ValueError("ssm credential source is missing 'parameter'")
    return cls(data['parameter'], data.get('region'))


@dataclass(frozen=True)
class Minted:
  value: str
  expires_at: datetime


class MintingSource(ABC):
  """Derive short-lived values from the convention-named JSON material file."""

  TYPE: ClassVar[str]
  CACHEABLE: ClassVar[bool] = False
  EXPIRY_MARGIN = timedelta(minutes=5)

  def __init__(self, **parameters: object):
    self._source_parameters = dict(parameters)
    self._minted: Optional[Minted] = None

  @abstractmethod
  def mint(self, config: dict) -> Minted: ...

  def fetch(self, material_path: Path) -> Optional[str]:
    config = self.config(material_path)
    if config is None:
      return None
    if self._minted is None or datetime.now(UTC) >= self._minted.expires_at - self.EXPIRY_MARGIN:
      self._minted = self.mint(config)
    return self._minted.value

  def config(self, material_path: Path) -> Optional[dict]:
    text = self._config_text(material_path)
    if text is None:
      return None
    return self._parse_config(material_path, text)

  def materialize_scoped(self, material_path: Path, value: str) -> tuple[Optional[dict], bytes]:
    text = self._config_text(material_path)
    if text is None:
      raise ValueError(f'{self.TYPE} config {material_path} disappeared during hydration')
    return {'type': self.TYPE, **self._source_parameters}, text.encode()

  def _config_text(self, material_path: Path) -> Optional[str]:
    if not material_path.is_file():
      return None
    try:
      return material_path.read_text()
    except UnicodeDecodeError as error:
      raise ValueError(f'{self.TYPE} config {material_path} is not valid UTF-8 text') from error

  def _parse_config(self, material_path: Path, text: str) -> dict:
    try:
      config = json.loads(text)
    except json.JSONDecodeError as error:
      raise ValueError(f'{self.TYPE} config {material_path} is not valid json') from error
    if not isinstance(config, dict):
      raise ValueError(f'{self.TYPE} config {material_path} is not a json object')
    return config

  @classmethod
  def from_dict(cls, data: dict) -> MintingSource:
    return cls(**data)


def _source_from_dict(data: dict) -> Source:
  if not isinstance(data, dict):
    raise ValueError(f'credential source annotation must be an object, got {type(data).__name__}')
  type_name = data.get('type')
  if not isinstance(type_name, str) or type_name == '':
    raise ValueError("credential source annotation must carry a non-empty string 'type'")
  parameters = {key: value for key, value in data.items() if key != 'type'}
  if type_name == LocalSource.TYPE:
    return LocalSource.from_dict(parameters)
  if type_name == SSMSource.TYPE:
    return SSMSource.from_dict(parameters)
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
  return source_class.from_dict(parameters)


class CredentialKind:
  """Code-owned metadata for one credential kind."""

  def __init__(self, name: str, description: str, *, install: Optional[dict] = None):
    _require_kind(name)
    if (
      not isinstance(description, str) or description.strip() != description or '\n' in description
    ):
      raise ValueError(f'credential kind {name!r}: description must be one trimmed line')
    if description == '':
      raise ValueError(f'credential kind {name!r}: description must not be empty')
    if install is not None and not isinstance(install, dict):
      raise ValueError(
        f'credential kind {name!r}: install must be an object, got {type(install).__name__}'
      )
    self.name = name
    self.description = description
    self.install_template = install
    self.install = self.install_for(name)

  def install_for(self, name: str) -> Optional[dict]:
    if self.install_template is None:
      return None
    install = _render_install(self.install_template, name)
    _validate_install(name, install)
    return install

  @classmethod
  def from_dict(cls, name: str, data: dict) -> CredentialKind:
    if not isinstance(data, dict):
      raise ValueError(f'credential registry entry {name!r} must be an object')
    unknown = sorted(set(data) - {'description', 'install'})
    if len(unknown) > 0:
      raise ValueError(
        f'credential registry entry {name!r} carries retired or unknown fields '
        f'{unknown}; move source configuration to {SOURCES_FILE} and material to '
        f'{MATERIAL_DIR}/<name>{MATERIAL_SUFFIX}'
      )
    if 'description' not in data:
      raise ValueError(f'credential registry entry {name!r} is missing required description')
    return cls(name, data['description'], install=data.get('install'))


def _render_install(install: dict, name: str) -> dict:
  variables = {'name': StringVariable(name)}

  def render(value: object) -> object:
    if isinstance(value, str):
      return template.render(value, variables)
    if isinstance(value, dict):
      return {key: render(item) for key, item in value.items()}
    raise ValueError(f'install hook for {name!r} carries an unexpected {type(value).__name__}')

  return {section: render(value) for section, value in install.items()}


def _parse_json_object(name: str, raw: str) -> dict:
  try:
    value = json.loads(raw)
  except json.JSONDecodeError as error:
    raise ValueError(f'secret {name!r} is not valid json') from error
  if not isinstance(value, dict):
    raise ValueError(f'secret {name!r} is not a json object')
  return value


def _contains_reference(node: Any) -> bool:
  if isinstance(node, dict):
    return _REFERENCE_KEY in node or any(_contains_reference(value) for value in node.values())
  if isinstance(node, list):
    return any(_contains_reference(item) for item in node)
  return False


def _referenced_names(text: str) -> set[str]:
  def walk(node: Any) -> set[str]:
    if isinstance(node, dict):
      if _REFERENCE_KEY in node:
        target = node[_REFERENCE_KEY]
        return {target} if isinstance(target, str) else set()
      return {name for value in node.values() for name in walk(value)}
    if isinstance(node, list):
      return {name for item in node for name in walk(item)}
    return set()

  try:
    tree = json.loads(text)
  except json.JSONDecodeError:
    return set()
  return walk(tree)


class Store:
  """Resolve one code registry against one exclusive directory and selection."""

  def __init__(
    self,
    registry: Mapping[str, CredentialKind],
    store_dir: str | Path,
    selection: Mapping[str, Optional[str]],
    *,
    _readable: Optional[Iterable[str]] = None,
  ):
    self.registry = dict(registry)
    self.store_dir = Path(store_dir)
    self.selection = _validate_selection(selection, self.registry)
    self._readable = None if _readable is None else frozenset(_readable)
    if self._readable is not None:
      unknown = sorted(self._readable - self.registry.keys())
      if len(unknown) > 0:
        raise ValueError(f'readable kinds outside the registry: {unknown}')
    self._sources = self._load_sources()
    self._cache: dict[str, str] = {}
    self._winners: dict[str, Source] = {}
    self._lock = threading.Lock()

  def _load_sources(self) -> dict[str, Source]:
    path = self.store_dir / SOURCES_FILE
    if not path.is_file():
      return {}
    try:
      data = json.loads(path.read_text())
    except json.JSONDecodeError as error:
      raise ValueError(f'credential source file {path} is not valid json') from error
    if not isinstance(data, dict):
      raise ValueError(f'credential source file {path} must be a json object')
    sources: dict[str, Source] = {}
    for name, annotation in data.items():
      kind, _ = parse_name(name)
      if not isinstance(annotation, dict):
        raise ValueError(f'credential source annotation {name!r} must be an object')
      type_name = annotation.get('type')
      if not isinstance(type_name, str) or type_name == '':
        raise ValueError(
          f'credential source annotation {name!r} must carry a non-empty string type'
        )
      if kind not in self.registry:
        if (
          type_name in {LocalSource.TYPE, SSMSource.TYPE}
          or _entry_point(_CREDENTIAL_SOURCE_GROUP, type_name) is not None
        ):
          _source_from_dict(annotation)
        continue
      sources[name] = _source_from_dict(annotation)
    return sources

  def _material_path(self, storage_name: str) -> Path:
    return self.store_dir / MATERIAL_DIR / f'{storage_name}{MATERIAL_SUFFIX}'

  def selected_name(self, name: str) -> str:
    kind, instance = parse_name(name)
    if instance is not None:
      return name
    selected = self.selection.get(kind)
    return kind if selected is None else f'{kind}+{selected}'

  def _source(self, storage_name: str) -> Source:
    return self._sources.get(storage_name, LocalSource())

  def resolve(self, name: str) -> Optional[tuple[str, bool]]:
    storage_name = self.selected_name(name)
    resolved = self._resolution(storage_name, requested=name)
    return None if isinstance(resolved, _Unresolved) else resolved

  def resolve_instance(self, name: str) -> Optional[tuple[str, bool]]:
    parse_name(name)
    resolved = self._resolution(name, requested=name)
    return None if isinstance(resolved, _Unresolved) else resolved

  def _resolution(self, storage_name: str, *, requested: str) -> _Resolution:
    kind, _ = parse_name(storage_name)
    if kind not in self.registry or (self._readable is not None and kind not in self._readable):
      return _Unresolved(requested)
    with self._lock:
      return self._resolve(storage_name, chain=())

  def _resolve(self, storage_name: str, chain: tuple[str, ...]) -> _Resolution:
    cached = self._cache.get(storage_name)
    if cached is not None:
      return cached, True
    kind, _ = parse_name(storage_name)
    if kind not in self.registry or (self._readable is not None and kind not in self._readable):
      return _Unresolved(storage_name)
    source = self._source(storage_name)
    raw = source.fetch(self._material_path(storage_name))
    if raw is None:
      return _Unresolved(storage_name)
    expanded = self._expand_references(raw.strip(), (*chain, storage_name))
    if isinstance(expanded, _Unresolved):
      return expanded
    value, references_cacheable = expanded
    self._winners[storage_name] = source
    cacheable = source.CACHEABLE and references_cacheable
    if cacheable:
      self._cache[storage_name] = value
    return value, cacheable

  def _expand_references(self, text: str, chain: tuple[str, ...]) -> _Resolution:
    try:
      tree = json.loads(text)
    except json.JSONDecodeError:
      return text, True
    if not _contains_reference(tree):
      return text, True
    referenced_cacheable: list[bool] = []
    unresolved: list[_Unresolved] = []
    substituted = self._substitute_references(tree, chain, referenced_cacheable, unresolved)
    if len(unresolved) > 0:
      return unresolved[0]
    return json.dumps(substituted), all(referenced_cacheable)

  def _substitute_references(
    self,
    node: Any,
    chain: tuple[str, ...],
    referenced_cacheable: list[bool],
    unresolved: list[_Unresolved],
  ) -> Any:
    if isinstance(node, dict):
      if _REFERENCE_KEY in node:
        return self._referenced_value(node, chain, referenced_cacheable, unresolved)
      return {
        key: self._substitute_references(value, chain, referenced_cacheable, unresolved)
        for key, value in node.items()
      }
    if isinstance(node, list):
      return [
        self._substitute_references(item, chain, referenced_cacheable, unresolved) for item in node
      ]
    return node

  def _referenced_value(
    self,
    node: dict,
    chain: tuple[str, ...],
    referenced_cacheable: list[bool],
    unresolved: list[_Unresolved],
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
    target_storage = self.selected_name(target)
    if target_storage in chain:
      raise ValueError(f'credential reference cycle: {" -> ".join((*chain, target_storage))}')
    resolved = self._resolve(target_storage, chain)
    if isinstance(resolved, _Unresolved):
      unresolved.append(resolved)
      return None
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

  def try_get(self, name: str) -> Optional[str]:
    _require_kind(name)
    resolved = self.resolve(name)
    return None if resolved is None else resolved[0]

  def try_get_instance(self, name: str) -> Optional[str]:
    resolved = self.resolve_instance(name)
    return None if resolved is None else resolved[0]

  def get(self, name: str) -> str:
    _require_kind(name)
    resolved = self._resolution(self.selected_name(name), requested=name)
    if isinstance(resolved, _Unresolved):
      raise SecretNotFound(resolved.name)
    return resolved[0]

  def get_instance(self, name: str) -> str:
    parse_name(name)
    resolved = self._resolution(name, requested=name)
    if isinstance(resolved, _Unresolved):
      raise SecretNotFound(resolved.name)
    return resolved[0]

  def get_json(self, name: str) -> dict:
    return _parse_json_object(name, self.get(name))

  def get_instance_json(self, name: str) -> dict:
    return _parse_json_object(name, self.get_instance(name))

  def available(self, name: str) -> bool:
    return self.try_get(name) is not None

  def available_instance(self, name: str) -> bool:
    return self.try_get_instance(name) is not None

  def known_names(self) -> frozenset[str]:
    return frozenset(self.registry)

  def instance_names(self) -> frozenset[str]:
    names = set(self._sources)
    material_dir = self.store_dir / MATERIAL_DIR
    if material_dir.is_dir():
      for path in material_dir.glob(f'*{MATERIAL_SUFFIX}'):
        names.add(path.name.removesuffix(MATERIAL_SUFFIX))
    accepted: set[str] = set()
    for name in names:
      kind, _ = parse_name(name)
      if kind in self.registry:
        accepted.add(name)
    return frozenset(accepted)

  def winning_source(self, name: str) -> Source:
    return self._winners[self.selected_name(name)]

  def material_path(self, name: str) -> Path:
    return self._material_path(self.selected_name(name))


def _validate_selection(
  selection: Mapping[str, Optional[str]], registry: Mapping[str, CredentialKind]
) -> dict[str, Optional[str]]:
  result: dict[str, Optional[str]] = {}
  for kind, instance in selection.items():
    _require_kind(kind)
    if kind not in registry:
      raise ValueError(f'instance selected for unknown credential kind {kind!r}')
    if instance is not None:
      if not isinstance(instance, str):
        raise ValueError(f'credential instance for {kind!r} must be a string or null')
      parse_name(f'{kind}+{instance}')
    result[kind] = instance
  return result


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


def default_registry() -> dict[str, CredentialKind]:
  data = json.loads(_BUILTIN_REGISTRY_PATH.read_text())
  if not isinstance(data, dict):
    raise ValueError(f'credential registry {_BUILTIN_REGISTRY_PATH} must be a json object')
  for name, entry in _contributed_registry_data().items():
    if name in data:
      raise ValueError(f'credential registry contribution duplicates kind {name!r}')
    data[name] = entry
  return {name: CredentialKind.from_dict(name, entry) for name, entry in data.items()}


def _reject_legacy_configuration() -> None:
  for variable in (LEGACY_REGISTRY_ENV, LEGACY_CONFIGS_ENV):
    if variable in os.environ:
      raise ValueError(
        f'{variable} is retired; set BRO_STORE to the exclusive credential store directory'
      )
  for filename in (LEGACY_REGISTRY_FILE, LEGACY_CREDENTIALS_FILE):
    path = Path(STORE_DIR) / filename
    if path.exists():
      raise ValueError(
        f'legacy credential file {path} is retired; migrate material to '
        f'{Path(STORE_DIR) / MATERIAL_DIR}/<name>{MATERIAL_SUFFIX} and typed sources to '
        f'{Path(STORE_DIR) / SOURCES_FILE}'
      )


_default_store: Optional[Store] = None
_default_store_lock = threading.Lock()


def default_store() -> Store:
  global _default_store
  if _default_store is None:
    with _default_store_lock:
      if _default_store is None:
        _reject_legacy_configuration()
        _default_store = Store(default_registry(), STORE_DIR, {})
  return _default_store


def get(name: str) -> str:
  return default_store().get(name)


def try_get(name: str) -> Optional[str]:
  return default_store().try_get(name)


def get_json(name: str) -> dict:
  return default_store().get_json(name)


def available(name: str) -> bool:
  return default_store().available(name)


def known_names() -> frozenset[str]:
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


def _scoped_selection(
  store: Store, required: set[str], optional: set[str]
) -> list[tuple[str, bool]]:
  _require_one_instance_per_kind(required | optional)
  selection: list[tuple[str, bool]] = []
  for name in sorted(required):
    kind, _ = parse_name(name)
    if kind not in store.registry:
      raise ValueError(f'unknown secret {name!r} declared in manifest; not in the registry')
    selection.append((name, True))
  for name in sorted(optional - required):
    kind, _ = parse_name(name)
    if kind not in store.registry:
      log.debug('optional secret %r not in the registry; skipping', name)
      continue
    selection.append((name, False))
  return selection


def _require_kind_level(name: str, reference: str) -> None:
  kind, instance = parse_name(reference)
  if instance is not None:
    raise ValueError(
      f'secret {name!r} ships reference-preserving text; reference {reference!r} '
      f'must be spelled at kind level ({kind!r}) — the scoped namespace is kinds-only'
    )


def build_scoped_store(
  store: Store, names: Iterable[str], *, optional: Iterable[str] = ()
) -> tuple[dict[str, bytes], frozenset[str]]:
  """Hydrate a kinds-only store and report the declared kinds that resolved."""
  selection = _scoped_selection(store, set(names), set(optional))
  files: dict[str, bytes] = {}
  typed_sources: dict[str, dict] = {}
  scoped: set[str] = set()
  pending_references: list[tuple[str, str]] = []
  declared_hydrated: set[str] = set()

  def materialize(name: str, value: str, cacheable: bool) -> None:
    kind, _ = parse_name(name)
    source = store.winning_source(name)
    material_path = store.material_path(name)
    if not cacheable:
      raw = source.fetch(material_path)
      if raw is None:
        raise ValueError(f'secret {name!r} disappeared during hydration')
      value = raw.strip()
      for reference in sorted(_referenced_names(value)):
        _require_kind_level(name, reference)
        pending_references.append((name, reference))
    annotation, content = source.materialize_scoped(material_path, value)
    files[f'{MATERIAL_DIR}/{kind}{MATERIAL_SUFFIX}'] = content
    if annotation is not None:
      typed_sources[kind] = annotation
    scoped.add(kind)

  for name, required in selection:
    resolved = store.resolve(name)
    if resolved is None:
      if required:
        raise SecretNotFound(store.selected_name(name))
      log.debug('optional secret %r unresolvable; skipping', name)
      continue
    value, cacheable = resolved
    materialize(name, value, cacheable)
    declared_hydrated.add(parse_name(name)[0])

  while len(pending_references) > 0:
    referrer, reference = pending_references.pop(0)
    if reference in scoped:
      continue
    resolved = store.resolve(reference)
    if resolved is None:
      raise SecretNotFound(store.selected_name(reference))
    log.info('hydrating %r into the scope: referenced by %r', reference, referrer)
    materialize(reference, *resolved)

  files[SOURCES_FILE] = json.dumps(typed_sources).encode()
  return files, frozenset(declared_hydrated)


def scoped_view_store(store: Store, names: Iterable[str], *, optional: Iterable[str] = ()) -> Store:
  selection = _scoped_selection(store, set(names), set(optional))
  view_selection = dict(store.selection)
  readable: set[str] = set()
  for name, _ in selection:
    kind, instance = parse_name(name)
    readable.add(kind)
    if instance is not None:
      view_selection[kind] = instance
  return Store(store.registry, store.store_dir, view_selection, _readable=readable)


def apply_grant_revoke(
  computed: Iterable[str],
  *,
  grant: Iterable[str] = (),
  revoke: Iterable[str] = (),
  subject: str = 'set',
) -> set[str]:
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


_INSTALL_SECTIONS = ('files', 'env', 'commands')
_INSTALL_VALUE_KEYS = ('path', 'secret')


def _validate_install(name: str, install: dict) -> None:
  unknown = sorted(set(install) - set(_INSTALL_SECTIONS))
  if len(unknown) > 0:
    raise ValueError(
      f'secret {name!r}: install declares unknown section(s) {", ".join(unknown)}; '
      f'known: {", ".join(_INSTALL_SECTIONS)}'
    )
  for section in _INSTALL_SECTIONS:
    if section in install and not isinstance(install[section], dict):
      raise ValueError(f'secret {name!r}: install {section} must be an object')
  for path, value in install.get('files', {}).items():
    _validate_install_path(name, path)
    _validate_install_value(name, value)
  for value in install.get('env', {}).values():
    _validate_install_value(name, value)
  for command, spec in install.get('commands', {}).items():
    if len(command) == 0 or '/' in command:
      raise ValueError(f'secret {name!r}: install shadows a malformed command {command!r}')
    if not isinstance(spec, dict) or set(spec) - {'env'} != set():
      raise ValueError(f'secret {name!r}: install command {command!r} declares only env')
    if 'env' in spec and not isinstance(spec['env'], dict):
      raise ValueError(f'secret {name!r}: install command {command!r} env must be an object')
    for value in spec.get('env', {}).values():
      _validate_install_value(name, value)


def _validate_install_path(name: str, path: str) -> None:
  if not isinstance(path, str):
    raise ValueError(f'secret {name!r}: install path must be a string')
  candidate = PurePosixPath(path)
  if candidate.is_absolute() or '..' in candidate.parts or len(candidate.parts) == 0:
    raise ValueError(
      f'secret {name!r}: install path {path!r} must be relative to the install directory'
    )


def _validate_install_value(name: str, value: object) -> None:
  if isinstance(value, str):
    return
  if not isinstance(value, dict) or len(value) != 1 or set(value) - set(_INSTALL_VALUE_KEYS):
    raise ValueError(
      f'secret {name!r}: install value must be text or one of '
      f'{", ".join("{" + key + ": ...}" for key in _INSTALL_VALUE_KEYS)}, got {value!r}'
    )
  ((key, target),) = value.items()
  if not isinstance(target, str):
    raise ValueError(f'secret {name!r}: install {key} must name a string, got {target!r}')
  if key == 'path':
    _validate_install_path(name, target)
  else:
    parse_name(target)


def install_hooks(
  registry: Mapping[str, CredentialKind],
  kinds: Iterable[str],
  store: Store,
  directory: Path,
  env: Mapping[str, str],
) -> dict[str, str]:
  hooks: dict[str, dict] = {}
  for kind in sorted(set(kinds)):
    _require_kind(kind)
    entry = registry.get(kind)
    if entry is None:
      raise ValueError(f'install hook requested for unknown credential kind {kind!r}')
    store.get(kind)
    if entry.install is not None:
      hooks[kind] = entry.install
  if directory.exists():
    shutil.rmtree(directory)
  directory.mkdir(parents=True)
  directory.chmod(0o700)
  if len(hooks) == 0:
    return {}
  log.verbose('installing credential hooks into %s', directory)
  exported: dict[str, str] = {}
  owners: dict[str, str] = {}
  binaries = directory / 'bin'
  for name, hook in hooks.items():
    for path, value in hook.get('files', {}).items():
      _claim(owners, f'file {path}', name)
      file = directory / path
      file.parent.mkdir(parents=True, exist_ok=True)
      file.write_text(_install_value(value, directory, store))
      file.chmod(0o600)
    for variable, value in hook.get('env', {}).items():
      _claim(owners, f'variable {variable}', name)
      exported[variable] = _install_value(value, directory, store)
    for command, spec in hook.get('commands', {}).items():
      _claim(owners, f'command {command}', name)
      binaries.mkdir(exist_ok=True)
      wrapper = binaries / command
      wrapper.write_text(
        _command_wrapper(name, command, spec.get('env', {}), directory, env, store)
      )
      wrapper.chmod(0o700)
  if binaries.is_dir():
    exported['PATH'] = os.pathsep.join([str(binaries), env.get('PATH', '')])
  return exported


def _claim(owners: dict[str, str], subject: str, name: str) -> None:
  owner = owners.setdefault(subject, name)
  if owner != name:
    raise ValueError(f'install hooks for {owner!r} and {name!r} both declare {subject}')


def _install_value(value: object, directory: Path, store: Store) -> str:
  if isinstance(value, str):
    return value
  assert isinstance(value, dict)
  ((key, target),) = value.items()
  assert isinstance(target, str)
  if key == 'path':
    return str(directory / target)
  return store.get_instance(target) if parse_name(target)[1] is not None else store.get(target)


def _command_wrapper(
  name: str,
  command: str,
  variables: Mapping[str, object],
  directory: Path,
  env: Mapping[str, str],
  store: Store,
) -> str:
  target = shutil.which(command, path=env.get('PATH'))
  if target is None:
    raise RuntimeError(
      f'the install hook for {name!r} shadows {command!r}, which is not on the '
      f"session's PATH — install it, or revoke {name!r} for this launch"
    )
  assignments = []
  for variable, value in variables.items():
    if isinstance(value, dict) and 'secret' in value:
      assignments.append(f'{variable}="$(credentials get {value["secret"]})"')
    else:
      assignments.append(f'{variable}={shlex.quote(_install_value(value, directory, store))}')
  run = ' '.join([*assignments, 'exec', shlex.quote(target), '"$@"'])
  return f'#!/usr/bin/env bash\n{run}\n'


def _get(name: str, field: Optional[str], as_json: bool, instance: bool) -> Optional[int]:
  store = default_store()
  try:
    if field is None and not as_json:
      print(store.get_instance(name) if instance else store.get(name))
      return None
    data = store.get_instance_json(name) if instance else store.get_json(name)
  except (SecretNotFound, ValueError) as error:
    print(str(error), file=sys.stderr)
    return 1
  value: dict | str = data
  if field is not None:
    if field not in data:
      print(f'secret {name!r} has no field {field!r}', file=sys.stderr)
      return 1
    value = data[field]
  print(
    json.dumps(value, indent=2)
    if as_json
    else value
    if isinstance(value, str)
    else json.dumps(value)
  )
  return None


def _list_available(instance: bool) -> None:
  store = default_store()
  if instance:
    for name in sorted(store.instance_names()):
      print(name)
    return
  for name in sorted(store.known_names()):
    print(f'{name}: {store.registry[name].description}')


def _install_hooks(directory: str, kinds: list[str]) -> None:
  store = default_store()
  exported = install_hooks(store.registry, kinds, store, Path(directory), os.environ)
  for name in sorted(exported):
    print(f'export {name}={shlex.quote(exported[name])}')


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description='resolve credentials from the default store')
  subparser = parser.add_subparsers(dest='action', required=True)
  get_parser = subparser.add_parser('get', help='resolve a secret and print it')
  get_parser.add_argument(
    'name',
    help='secret kind; with --instance, a storage name (kind or kind+instance)',
  )
  get_parser.add_argument('--field', help='for a json secret, print only this field')
  get_parser.add_argument(
    '--json', dest='as_json', action='store_true', help='parse as json and pretty-print (indent=2)'
  )
  get_parser.add_argument(
    '--instance', '-i', action='store_true', help='address storage by exact name instead of kind'
  )
  get_parser.set_handler(_get)
  list_parser = subparser.add_parser('list', help='list credential kinds and their descriptions')
  list_parser.add_argument(
    '--instance', '-i', action='store_true', help='list names present in the store instead of kinds'
  )
  list_parser.set_handler(_list_available)
  hooks_parser = subparser.add_parser(
    'install-hooks',
    help='apply named credential hooks and print the environment they export',
  )
  hooks_parser.add_argument('directory', help='directory the hooks may write into')
  hooks_parser.add_argument('kinds', nargs='*', help='hydrated credential kinds whose hooks apply')
  hooks_parser.set_handler(_install_hooks)
  return parser.dispatch(argv)
