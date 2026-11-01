import importlib
import importlib.metadata
from typing import Optional

from bro.bro import BaseBro
from bro.llm.llm import Recipe

_REGISTRY: dict[str, type[BaseBro]] = {}
# when True (the default), a lookup miss imports the matching bro module on
# demand. tests flip it off to isolate the registry to whatever they register by
# hand, so the real bros never bleed into list_classes().
_autoload = True

_ENTRY_POINT_PREFIX = 'bro.bros.'


def _entry_points() -> tuple[importlib.metadata.EntryPoint, ...]:
  entries = importlib.metadata.entry_points()
  return tuple(
    entry
    for group in sorted(entries.groups)
    if group.startswith(_ENTRY_POINT_PREFIX)
    for entry in entries.select(group=group)
  )


def _declarations() -> dict[str, importlib.metadata.EntryPoint]:
  from bro.workspace.installation import validate_home

  declarations: dict[str, importlib.metadata.EntryPoint] = {}
  for entry in _entry_points():
    validate_home(entry.group.removeprefix(_ENTRY_POINT_PREFIX))
    _validate_name(f'entry point {entry.value}', entry.name)
    if entry.name in declarations:
      previous = declarations[entry.name]
      assert previous.dist is not None and entry.dist is not None
      raise ValueError(
        f'duplicate bro {entry.name!r}: '
        f'{previous.dist.metadata["Name"]} ({previous.group}: {previous.value}) vs '
        f'{entry.dist.metadata["Name"]} ({entry.group}: {entry.value})'
      )
    declarations[entry.name] = entry
  return declarations


def declared_specs() -> dict[str, str]:
  """Read installed bro names and class references without importing their modules."""
  return {name: entry.value for name, entry in _declarations().items()}


def home(name: str) -> str | None:
  """The home declaring `name`, or None for a class registered only in-process."""
  entry = _declarations().get(name)
  return None if entry is None else entry.group.removeprefix(_ENTRY_POINT_PREFIX)


MAX_NAME_LENGTH = 64


def _validate_name(owner: str, name: str) -> None:
  if len(name) == 0 or len(name) > MAX_NAME_LENGTH:
    raise ValueError(f'{owner} name {name!r} must be 1 to {MAX_NAME_LENGTH} characters')


def register(bro_cls: type[BaseBro]) -> None:
  name = getattr(bro_cls, 'name', None)
  if not isinstance(name, str):
    raise ValueError(f'{bro_cls.__name__} must declare a `name` class attribute')
  _validate_name(bro_cls.__name__, name)
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


def create_bro(name: str, llm_spec: Optional[Recipe] = None) -> BaseBro:
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
