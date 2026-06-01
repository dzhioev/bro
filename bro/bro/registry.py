from bro.bros.bro import Bro
from llm.llm import LLMSpec

_REGISTRY: dict[str, type[Bro]] = {}
_initialized = False


def _ensure_initialized() -> None:
  global _initialized
  if _initialized:
    return
  _initialized = True
  from bro.bros import init

  init()


def register(bro_cls: type[Bro]) -> None:
  name = getattr(bro_cls, 'name', None)
  if not isinstance(name, str):
    raise ValueError(f'{bro_cls.__name__} must declare a `name` class attribute')
  if name in _REGISTRY:
    raise ValueError(f'duplicate bro name: {name!r}')
  _REGISTRY[name] = bro_cls


def get_class(name: str) -> type[Bro]:
  _ensure_initialized()
  cls = _REGISTRY.get(name)
  if cls is None:
    raise KeyError(f'unknown bro: {name!r}')
  return cls


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
  _ensure_initialized()
  return list(_REGISTRY.values())
