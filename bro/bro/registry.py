import importlib

from bro.bros.bro import Bro
from llm.llm import LLMSpec

_REGISTRY: dict[str, type[Bro]] = {}
# when True (the default), a lookup miss imports the matching bro module from
# BRO_SPECS on demand. tests flip it off to isolate the registry to whatever they
# register by hand, so the real bros never bleed into list_classes().
_autoload = True


def register(bro_cls: type[Bro]) -> None:
  name = getattr(bro_cls, 'name', None)
  if not isinstance(name, str):
    raise ValueError(f'{bro_cls.__name__} must declare a `name` class attribute')
  if name in _REGISTRY:
    raise ValueError(f'duplicate bro name: {name!r}')
  _REGISTRY[name] = bro_cls


def _autoload_class(name: str) -> type[Bro] | None:
  # import only the single bro module that declares `name` and register it; None
  # if `name` is not a known bro. importing one bro instead of all of them keeps
  # `create_bro('pm')` from dragging in every other bro's dependency graph.
  from bro.bros import BRO_SPECS

  spec = BRO_SPECS.get(name)
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


def create_bro(name: str, llm_spec: LLMSpec | None = None) -> Bro:
  """instantiate the registered bro by name. returns a fresh instance every
  call — construction walks the MRO, materialises MCP servers, and renders the
  system prompt, so callers that need the same instance across requests should
  cache the return value themselves."""
  cls = get_class(name)
  if llm_spec is not None:
    return cls.create(llm_spec)
  return cls()


def list_classes() -> list[type[Bro]]:
  if _autoload:
    from bro.bros import BRO_SPECS

    for name in BRO_SPECS:
      _autoload_class(name)
  return list(_REGISTRY.values())
