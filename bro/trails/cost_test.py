from decimal import Decimal

import pytest

from bro.trails.cost import UnpricedCallError, strict_trail_cost, trail_cost
from bro.trails.local import LocalStore
from bro.trails.model import BlazeRequest
from bro.trails.record.spine import Recording


def _request(model: str = 'gpt-5.6-terra', *, summoned_by=None) -> BlazeRequest:
  return BlazeRequest(
    harness='bro',
    bro='dev',
    version='test',
    native={'llm': {'type': 'openai', 'model': model}},
    body={'records': [{'kind': 'system_prompt', 'body': 'prompt'}]},
    interactive=False,
    surface='test',
    summoned_by=summoned_by,
  )


def _call(model: str = 'gpt-5.6-terra', *, service_tier: str = 'default') -> dict:
  return {
    'kind': 'llm_call',
    'body': {
      'request': {'model': model},
      'response': {
        'model': model,
        'service_tier': service_tier,
        'usage': {'input_tokens': 1, 'output_tokens': 1},
        'output': [],
      },
    },
  }


def _recording(store: LocalStore, model: str = 'gpt-5.6-terra', *, summoned_by=None):
  return Recording.create(store, _request(model, summoned_by=summoned_by))


def test_trail_cost_sums_raw_calls_and_service_tiers(tmp_path):
  store = LocalStore(tmp_path)
  recording = _recording(store)
  recording.append([_call(), _call(service_tier='priority')])
  recording.end('ok')

  assert trail_cost(store, recording.trail_id) == Decimal('0.000042')
  assert strict_trail_cost(store, recording.trail_id) == Decimal('0.000042')


def test_an_unpriced_call_invalidates_the_whole_trail(tmp_path):
  store = LocalStore(tmp_path)
  recording = _recording(store, 'unknown')
  recording.append([_call('unknown')])

  assert trail_cost(store, recording.trail_id) is None
  with pytest.raises(UnpricedCallError, match='unknown'):
    strict_trail_cost(store, recording.trail_id)


def test_trail_cost_walks_no_summon_closure(tmp_path):
  store = LocalStore(tmp_path)
  parent = _recording(store)
  parent.append([_call()])
  child = _recording(
    store,
    summoned_by={'trail_id': parent.trail_id, 'step_id': 1, 'index': 0},
  )
  child.append([_call(service_tier='priority')])

  assert trail_cost(store, parent.trail_id) == Decimal('0.000014')
  assert trail_cost(store, child.trail_id) == Decimal('0.000028')


def test_a_trail_without_llm_calls_costs_zero(tmp_path):
  store = LocalStore(tmp_path)
  recording = _recording(store)

  assert trail_cost(store, recording.trail_id) == Decimal(0)
