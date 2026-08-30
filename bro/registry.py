import importlib
import importlib.metadata
from typing import Optional

from bro.bro import BaseBro
from bro.llm.llm import NativeLLMSpec

_REGISTRY: dict[str, type[BaseBro]] = {}
# when True (the default), a lookup miss imports the matching bro module on
# demand. tests flip it off to isolate the registry to whatever they register by
# hand, so the real bros never bleed into list_classes().
_autoload = True

# every bro registers through this entry-point group, the framework's own no
# differently from a consuming project's: a distribution declares
# `[project.entry-points.bro] <name> = "<module>:<ClassName>"` and its bros
# resolve wherever that venv is active.
_ENTRY_POINT_GROUP = 'bro'


def _entry_points() -> tuple[importlib.metadata.EntryPoint, ...]:
  return tuple(importlib.metadata.entry_points(group=_ENTRY_POINT_GROUP))


def declared_specs() -> dict[str, str]:
  """name -> "module:ClassName" for every bro the installed distributions declare.

  Reading the metadata imports no bro module, so a caller can learn which names
  resolve without paying for any of them. Two distributions claiming one name
  raise rather than letting import order pick a winner.
  """
  specs: dict[str, str] = {}
  for entry_point in _entry_points():
    if entry_point.name in specs:
      raise ValueError(
        f'duplicate bro {entry_point.name!r}: {specs[entry_point.name]} vs {entry_point.value}'
      )
    specs[entry_point.name] = entry_point.value
  return specs


def register(bro_cls: type[BaseBro]) -> None:
  name = getattr(bro_cls, 'name', None)
  if not isinstance(name, str):
    raise ValueError(f'{bro_cls.__name__} must declare a `name` class attribute')
  if name in _REGISTRY:
    raise ValueError(f'duplicate bro name: {name!r}')
  _REGISTRY[name] = bro_cls


def _autoload_class(name: str) -> Optional[type[BaseBro]]:
  # import only the single bro module that declares `name` and register it; None
  # if `name` is not a known bro. importing one bro instead of all of them keeps
  # `create_bro('dev')` from dragging in every other bro's dependency graph.
  spec = declared_specs().get(name)
  if spec is None:
    return None
  module_path, class_name = spec.split(':')
  cls = getattr(importlib.import_module(module_path), class_name)
  if cls.name != name:
    raise ValueError(f'bro class {class_name} declares name {cls.name!r}, expected {name!r}')
  if name not in _REGISTRY:
    register(cls)
  return _REGISTRY[name]


def get_class(name: str) -> type[BaseBro]:
  cls = _REGISTRY.get(name)
  if cls is not None:
    return cls
  if _autoload:
    loaded = _autoload_class(name)
    if loaded is not None:
      return loaded
  raise KeyError(f'unknown bro: {name!r}')


def create_bro(name: str, llm_spec: Optional[NativeLLMSpec] = None) -> BaseBro:
  """instantiate the registered bro by name. returns a fresh instance every
  call — construction walks the MRO, materialises MCP servers, and renders the
  system prompt, so callers that need the same instance across requests should
  cache the return value themselves."""
  cls = get_class(name)
  if llm_spec is not None:
    return cls.create(llm_spec)
  return cls()


def known_names() -> set[str]:
  """every name get_class can resolve right now, without importing any bro
  module: hand-registered classes plus, when autoload is on, every declared
  bro."""
  names = set(_REGISTRY)
  if _autoload:
    names |= set(declared_specs())
  return names


def list_classes() -> list[type[BaseBro]]:
  if _autoload:
    for name in declared_specs():
      _autoload_class(name)
  return list(_REGISTRY.values())


def lineage(name: str) -> tuple[str, ...]:
  """the bro names `name` answers to: its own first, then every registered bro
  it derives from, base-ward.

  A name no installed distribution declares answers to itself alone rather than
  raising: this reports what a name answers to, it does not gate whether the
  name resolves (`known_names`).
  """
  known = known_names()
  if name not in known:
    return (name,)
  names: list[str] = []
  for ancestor in get_class(name).__mro__:
    declared = ancestor.__dict__.get('name')
    if isinstance(declared, str) and declared in known and declared not in names:
      names.append(declared)
  return tuple(names)
