import json
from copy import deepcopy
from pathlib import Path

import httpx2
import pytest

from bro.llm import wires
from bro.llm.records import Call
from bro.llm.wire import File, Prefix, SystemPrompt, Text, UserMessage
from bro.native.wire_fixtures_test_helper import RESTORE_ITEMS, capture_requests, cases
from bro.native_trails.format import BRO_FORMAT
from bro.trails.model import RecordedTrail, Step, Trail

_CASES = cases()


@pytest.fixture
def transport(monkeypatch):
  sent: list[dict] = []
  replies: list[dict] = []

  async def send(client, request, **kwargs):
    sent.append(json.loads(await request.aread()))
    return httpx2.Response(200, request=request, json=deepcopy(replies.pop(0)))

  monkeypatch.setattr(httpx2.AsyncClient, 'send', send)
  capture_requests(monkeypatch, sent)
  return sent, replies


def test_every_installed_client_has_conformance_fixtures():
  assert set(_CASES) == set(wires.client_names())


@pytest.mark.asyncio
@pytest.mark.parametrize(
  'name,case', [(name, case) for name, cases in _CASES.items() for case in cases]
)
async def test_exchange_conformance(name, case, transport):
  sent, replies = transport
  canned = _CASES[name][case]
  replies.append(canned.response)
  wire = wires.open(canned.recipe, {'api_key': 'fixture-key'})
  leg = await wire.exchange([SystemPrompt('system'), UserMessage((Text('reply'),))], [], 2.0)
  recorded = wires.decode(name, leg.record['response'])
  assert recorded.parts == canned.parts
  assert recorded.stop == canned.stop
  assert leg.wire == wire.NAME == name
  assert leg.parts == canned.parts
  assert leg.stop == canned.stop
  assert leg.counts == canned.counts
  assert leg.record['request']['tools'] == []
  assert leg.reply == wires.decode(name, canned.response).reply
  assert leg.parts == wires.decode(name, canned.response).parts

  step = _step('llm_call', leg.record, 2, wire=name, **leg.extras)
  events = BRO_FORMAT.project(
    {'kind': step.kind, 'body': step.body, 'step_id': step.step_id, **step.extras}
  )
  for part in leg.parts:
    if isinstance(part, Call):
      projected = [event for event in events if event['type'] == 'tool_call']
      assert [(event['source']['index'], event['call_id']) for event in projected] == [
        (part.index, part.call_id)
      ]


@pytest.mark.asyncio
@pytest.mark.parametrize('name', _CASES)
async def test_record_restore_and_next_request(name, transport):
  sent, replies = transport
  canned = _CASES[name]['plain']
  wire = wires.open(canned.recipe, {'api_key': 'fixture-key'})
  replies.extend([canned.response, canned.response, canned.response])
  first = await wire.exchange([SystemPrompt('system'), UserMessage((Text('reply'),))], [], None)
  second = await wire.exchange([UserMessage((Text('next'),))], [], None)
  recorded = RecordedTrail(
    _header(canned.recipe.dump()),
    [
      _step('user_input', 'reply', 1),
      _step('llm_call', first.record, 2, wire=name),
    ],
  )
  restored = wires.open(canned.recipe, {'api_key': 'fixture-key'})
  restored.restore(Prefix('system', tuple(recorded.steps)))
  third = await restored.exchange([UserMessage((Text('next'),))], [], None)
  fixture = RESTORE_ITEMS[name]
  field = fixture['field']
  assert first.record['request'][field] == fixture['initial']
  assert second.record['request'][field] == third.record['request'][field] == fixture['next']
  if wire.CHAINING == 'server':
    assert sent[1]['previous_response_id'] == first.extras['response_id']
    assert 'previous_response_id' not in sent[2]
    assert sent[2][field] == fixture['initial'] + fixture['assistant'] + fixture['next']
  else:
    expected = fixture['initial'] + fixture['assistant'] + fixture['next']
    assert sent[1][field] == sent[2][field] == expected
    with pytest.raises(ValueError, match='does not support server chains'):
      restored.seed_chain('not-a-chain')


def _header(recipe: dict) -> Trail:
  return Trail(
    id='conformance',
    harness='bro',
    bro='bro',
    version='1',
    native={'llm': recipe},
    started_at='2026-01-01T00:00:00Z',
    interactive=False,
    surface='test',
    forked_from=None,
  )


def _step(kind, body, step_id, **extras) -> Step:
  return Step(
    trail_id='conformance',
    step_id=step_id,
    ts='2026-01-01T00:00:00Z',
    kind=kind,
    body=body,
    extras=extras,
  )


def test_loop_and_fork_do_not_name_providers():
  import ast

  import bro.fork
  import bro.native.loop
  from bro.llm import providers

  names = set(providers.known_names())
  for module in (bro.fork, bro.native.loop):
    assert module.__file__ is not None
    tree = ast.parse(Path(module.__file__).read_text())
    for node in ast.walk(tree):
      if isinstance(node, ast.Constant) and isinstance(node.value, str):
        assert node.value not in names
      elif isinstance(node, (ast.Import, ast.ImportFrom)):
        modules = (
          [node.module or '']
          if isinstance(node, ast.ImportFrom)
          else [alias.name for alias in node.names]
        )
        assert all(not any(name in module.split('.') for name in names) for module in modules)


@pytest.mark.asyncio
@pytest.mark.parametrize('name', _CASES)
async def test_unsupported_content_is_refused_before_sending(name, transport, monkeypatch):
  from bro.llm.llm import UnsupportedContent

  recipe = _CASES[name]['plain'].recipe
  monkeypatch.setattr(type(recipe), 'accepts', lambda self, mime_type: False)
  sent, replies = transport
  wire = wires.open(recipe, {'api_key': 'fixture-key'})
  with pytest.raises(UnsupportedContent, match='image.*not supported'):
    await wire.exchange([UserMessage((File(b'image', 'image/png'),))], [], None)
  assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize('name', _CASES)
async def test_model_gate_checks_all_files_before_encoding(name, transport, monkeypatch):
  from bro.llm.llm import UnsupportedContent

  recipe = _CASES[name]['plain'].recipe
  monkeypatch.setattr(
    type(recipe), 'MODEL_INPUTS', {recipe.selection()['model']: frozenset({'application/pdf'})}
  )
  wire = wires.open(recipe, {'api_key': 'fixture-key'})
  with pytest.raises(UnsupportedContent, match='image/png.*not supported'):
    await wire.exchange(
      [UserMessage((File(b'pdf', 'application/pdf'), File(b'png', 'image/png')))], [], None
    )
  assert transport[0] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['chat', 'echo'])
async def test_protocol_gate_refuses_a_model_accepted_document(name, transport, monkeypatch):
  from bro.llm.llm import UnsupportedContent

  recipe = _CASES[name]['plain'].recipe
  monkeypatch.setattr(
    type(recipe), 'MODEL_INPUTS', {recipe.selection()['model']: frozenset({'application/pdf'})}
  )
  wire = wires.open(recipe, {'api_key': 'fixture-key'})
  with pytest.raises(UnsupportedContent, match='cannot carry'):
    await wire.exchange([UserMessage((File(b'pdf', 'application/pdf'),))], [], None)
  assert transport[0] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('name', _CASES)
async def test_multiple_text_items_use_protocol_content_list(name, transport):
  _, replies = transport
  canned = _CASES[name]['plain']
  replies.append(canned.response)
  wire = wires.open(canned.recipe, {'api_key': 'fixture-key'})
  leg = await wire.exchange([UserMessage((Text('first'), Text('second')))], [], None)
  field = RESTORE_ITEMS[name]['field']
  encoded = leg.record['request'][field][0]
  if name == 'echo':
    assert encoded['text'] == 'first\nsecond'
  else:
    kind = 'input_text' if name == 'responses' else 'text'
    assert encoded['content'] == [{'type': kind, 'text': 'first'}, {'type': kind, 'text': 'second'}]
