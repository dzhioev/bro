import importlib
import importlib.metadata
from typing import Optional

from bro.bros.bro import Bro
from llm.llm import LLMSpec

_REGISTRY: dict[str, type[Bro]] = {}
# when True (the default), a lookup miss imports the matching bro module from
# BRO_SPECS on demand. tests flip it off to isolate the registry to whatever they
# register by hand, so the real bros never bleed into list_classes().
_autoload = True

# out-of-tree bros register through this entry-point group: a bro-framework
# user project declares `[project.entry-points.bro]
# <name> = "<module>:<ClassName>"` and its bros resolve wherever that venv is
# active — no edit to BRO_SPECS.
_ENTRY_POINT_GROUP = 'bro'


def _entry_points() -> tuple[importlib.metadata.EntryPoint, ...]:
  return tuple(importlib.metadata.entry_points(group=_ENTRY_POINT_GROUP))


def _external_specs() -> dict[str, str]:
  """name -> "module:ClassName" declared by installed distributions' entry
  points — the out-of-tree counterpart of BRO_SPECS. reading the metadata never
  imports a bro module; collisions (with a built-in or between externals) raise."""
  from bro.bros import BRO_SPECS

  specs: dict[str, str] = {}
  for entry_point in _entry_points():
    if entry_point.name in BRO_SPECS:
      raise ValueError(
        f'external bro {entry_point.name!r} ({entry_point.value}) shadows a built-in bro'
      )
    if entry_point.name in specs:
      raise ValueError(
        f'duplicate external bro {entry_point.name!r}: '
        f'{specs[entry_point.name]} vs {entry_point.value}'
      )
    specs[entry_point.name] = entry_point.value
  return specs


def register(bro_cls: type[Bro]) -> None:
  name = getattr(bro_cls, 'name', None)
  if not isinstance(name, str):
    raise ValueError(f'{bro_cls.__name__} must declare a `name` class attribute')
  if name in _REGISTRY:
    raise ValueError(f'duplicate bro name: {name!r}')
  _REGISTRY[name] = bro_cls


def _autoload_class(name: str) -> Optional[type[Bro]]:
  # import only the single bro module that declares `name` and register it; None
  # if `name` is not a known bro. importing one bro instead of all of them keeps
  # `create_bro('pm')` from dragging in every other bro's dependency graph.
  from bro.bros import BRO_SPECS

  spec = BRO_SPECS.get(name)
  if spec is None:
    spec = _external_specs().get(name)
  if spec is None:
    return None
  module_path, class_name = spec.split(':')
  cls = getattr(importlib.import_module(module_path), class_name)
  if cls.name != name:
    raise ValueError(f'bro class {class_name} declares name {cls.name!r}, expected {name!r}')
  if name not in _REGISTRY:
    register(cls)
  return _REGISTRY[name]


def get_class(name: str) -> type[Bro]:
  cls = _REGISTRY.get(name)
  if cls is not None:
    return cls
  if _autoload:
    loaded = _autoload_class(name)
    if loaded is not None:
      return loaded
  raise KeyError(f'unknown bro: {name!r}')


def create_bro(name: str, llm_spec: Optional[LLMSpec] = None) -> Bro:
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
  module: hand-registered classes plus, when autoload is on, the built-in map
  and the entry-point externals."""
  names = set(_REGISTRY)
  if _autoload:
    from bro.bros import BRO_SPECS

    names |= set(BRO_SPECS) | set(_external_specs())
  return names


def list_classes() -> list[type[Bro]]:
  if _autoload:
    from bro.bros import BRO_SPECS

    for name in BRO_SPECS:
      _autoload_class(name)
    for name in _external_specs():
      _autoload_class(name)
  return list(_REGISTRY.values())
