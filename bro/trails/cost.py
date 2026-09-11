"""Post-run pricing over provider-raw trail messages."""

from decimal import Decimal
from typing import Any

from bro.llm import providers
from bro.trails.store import TrailsStore


class UnpricedCallError(ValueError):
  def __init__(self, provider: str, model: str, service_tier: str | None):
    tier = '' if service_tier is None else f' at service tier {service_tier!r}'
    super().__init__(f'no {provider} price for model {model!r}{tier}')
    self.provider = provider
    self.model = model
    self.service_tier = service_tier


def _required_string(value: Any, field: str) -> str:
  if not isinstance(value, str) or len(value) == 0:
    raise ValueError(f'{field} must be a non-empty string')
  return value


def _provider(header: dict[str, Any]) -> str:
  native = header.get('native')
  if not isinstance(native, dict):
    raise ValueError('trail header native must be an object')
  llm = native.get('llm')
  if not isinstance(llm, dict):
    raise ValueError('trail header native.llm must be an object')
  return _required_string(llm.get('type'), 'trail header native.llm.type')


def _call(message: dict[str, Any]) -> tuple[str, dict[str, Any], str | None]:
  model = _required_string(message.get('model'), 'llm_call model')
  usage = message.get('usage')
  if not isinstance(usage, dict):
    raise ValueError('llm_call usage must be an object')
  service_tier = message.get('service_tier')
  if service_tier is not None and not isinstance(service_tier, str):
    raise ValueError('llm_call service_tier must be a string or null')
  return model, usage, service_tier


def _trail_cost(store: TrailsStore, trail_id: str, *, strict: bool) -> Decimal | None:
  provider = _provider(store.get_trail(trail_id))
  total = Decimal(0)
  for message in store.iter_messages(trail_id, types={'llm_call'}):
    model, usage, service_tier = _call(message)
    cost = providers.price(provider, model, usage, service_tier)
    if cost is None:
      if strict:
        raise UnpricedCallError(provider, model, service_tier)
      return None
    total += cost
  return total


def trail_cost(store: TrailsStore, trail_id: str) -> Decimal | None:
  """Sum one trail without following forks or summons; return None if any call is unpriced."""
  return _trail_cost(store, trail_id, strict=False)


def strict_trail_cost(store: TrailsStore, trail_id: str) -> Decimal:
  """Sum one trail, raising with the first unpriced model instead of returning a partial cost."""
  cost = _trail_cost(store, trail_id, strict=True)
  assert cost is not None
  return cost
