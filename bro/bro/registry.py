from bro.bro import BaseBro

_REGISTRY: dict[str, BaseBro] = {}
_initialized = False


def _ensure_initialized() -> None:
  global _initialized
  if _initialized:
    return
  _initialized = True
  from bro.bros import init

  init()


def register(bro_cls: type[BaseBro]) -> None:
  instance = bro_cls()
  if instance.name in _REGISTRY:
    raise ValueError(f'duplicate bro name: {instance.name!r}')
  _REGISTRY[instance.name] = instance


def get_bro(name: str) -> BaseBro:
  _ensure_initialized()
  bro = _REGISTRY.get(name)
  if bro is None:
    raise KeyError(f'unknown bro: {name!r}')
  return bro


def list_bros() -> list[BaseBro]:
  _ensure_initialized()
  return list(_REGISTRY.values())
