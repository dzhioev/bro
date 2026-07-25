from datetime import UTC, datetime
from typing import Any, Optional, cast
from unittest.mock import patch

import pytest

from bro.launch.resume import (
  RESUME_LATEST,
  HistoryMessage,
  conversation_history,
  find_latest_call_trail,
  resume,
)
from llm.llms.chat_gpt import LLMSpec

_SPILL_KEYS = {'s3', 'url', 'size'}


class FakeTrailsClient:
  """dict-backed stand-in for `trails.client.TrailsClient`'s read surface."""

  def __init__(
    self,
    headers: list[dict],
    steps: dict[str, list[dict]],
    spilled: Optional[dict[str, Any]] = None,
  ):
    # newest-first, like the real listing
    self._headers = headers
    self._steps = steps
    self._spilled = spilled if spilled is not None else {}

  def iter_trails(self, *, bro=None, max_items=None, **_query):
    yielded = 0
    for header in self._headers:
      if bro is not None and header['bro'] != bro:
        continue
      yield header
      yielded += 1
      if max_items is not None and yielded >= max_items:
        return

  def get_trail(self, trail_id: str) -> dict:
    for header in self._headers:
      if header['id'] == trail_id:
        return header
    raise KeyError(trail_id)

  def iter_steps(self, trail_id: str):
    yield from self._steps[trail_id]

  def resolve_body(self, body: Any) -> Any:
    if isinstance(body, dict) and set(body.keys()) == _SPILL_KEYS:
      return self._spilled[body['url']]
    return body


def _header(
  trail_id: str,
  *,
  bro: str = 'record',
  surface: str = 'call',
  forked_from: Optional[dict] = None,
) -> dict:
  return {
    'id': trail_id,
    'harness': 'bro',
    'bro': bro,
    'version': '1',
    'native': {'llm': {'type': 'chat_gpt', 'model': 'gpt-5'}},
    'started_at': '2026-06-07T00:00:00.000000Z',
    'interactive': True,
    'surface': surface,
    'forked_from': forked_from,
  }


def _row(trail_id: str, step_id: str, kind: str, body: Any, **extras: Any) -> dict:
  return {
    'trail_id': trail_id,
    'step_id': step_id,
    'ts': '2026-06-07T10:00:00.000000Z',
    'kind': kind,
    'body': body,
    **extras,
  }


def _llm_call_body(*output_items: dict) -> dict:
  return {'request': {}, 'response': {'id': 'resp', 'output': list(output_items)}}


def _output_message(text: str) -> dict:
  return {
    'type': 'message',
    'role': 'assistant',
    'content': [{'type': 'output_text', 'text': text}],
  }


def _forked_from_steps() -> list[dict]:
  return [
    _row('trail-1', 's0', 'system_prompt', 'prompt'),
    _row('trail-1', 'u0', 'user_input', 'hello'),
    _row(
      'trail-1',
      'c1',
      'llm_call',
      _llm_call_body(_output_message('hi back')),
      response_id='r1',
    ),
    _row('trail-1', 'a1', 'assistant', 'hi back', terminal=True),
  ]


class TestFindLatestCallTrail:
  def test_picks_the_newest_call_trail_skipping_other_surfaces(self):
    client = FakeTrailsClient(
      headers=[
        _header('trail-3', surface='fork'),
        _header('trail-2'),
        _header('trail-1'),
      ],
      steps={},
    )
    assert find_latest_call_trail(cast(Any, client), 'record') == 'trail-2'

  def test_returns_none_when_the_bro_has_no_call_trails(self):
    client = FakeTrailsClient(headers=[_header('trail-1', surface='fork')], steps={})
    assert find_latest_call_trail(cast(Any, client), 'record') is None


class TestConversationHistory:
  def test_collects_user_inputs_and_terminal_replies(self):
    steps = _forked_from_steps() + [
      _row('trail-1', 'u1', 'user_input', 'and then?'),
      _row(
        'trail-1',
        'c2',
        'llm_call',
        _llm_call_body(_output_message('interim'), _output_message('final')),
        response_id='r2',
      ),
      _row('trail-1', 'a2', 'assistant', 'interim', terminal=False),
      _row('trail-1', 'a3', 'assistant', 'final', terminal=True),
    ]
    client = FakeTrailsClient(headers=[_header('trail-1')], steps={'trail-1': steps})
    history = conversation_history(cast(Any, client), 'trail-1')
    assert [(m.by_user, m.text) for m in history] == [
      (True, 'hello'),
      (False, 'hi back'),
      (True, 'and then?'),
      (False, 'final'),
    ]

  def test_timestamps_are_timezone_aware_local_time(self):
    client = FakeTrailsClient(headers=[_header('trail-1')], steps={'trail-1': _forked_from_steps()})
    history = conversation_history(cast(Any, client), 'trail-1')
    expected = datetime(2026, 6, 7, 10, 0, 0, tzinfo=UTC).astimezone()
    assert history[0].when == expected
    assert history[0].when.tzinfo is not None

  def test_resolves_spilled_bodies(self):
    descriptor = {'s3': 'key', 'url': 'https://spill/x', 'size': 12}
    steps = [
      _row('trail-1', 's0', 'system_prompt', 'prompt'),
      _row('trail-1', 'u0', 'user_input', descriptor),
    ]
    client = FakeTrailsClient(
      headers=[_header('trail-1')],
      steps={'trail-1': steps},
      spilled={'https://spill/x': 'a very long message'},
    )
    history = conversation_history(cast(Any, client), 'trail-1')
    assert history == [
      HistoryMessage(
        by_user=True,
        text='a very long message',
        when=datetime.fromisoformat('2026-06-07T10:00:00.000000Z').astimezone(),
      )
    ]

  def test_walks_the_fork_chain_and_cuts_ancestors_at_their_fork_point(self):
    # the forked_from continued past the fork point ('diverged') — that content is
    # not part of the resumed conversation; the fork turn's own trailing
    # terminal reply ('hi back', recorded after the c1 fork step) is.
    forked_from_steps = _forked_from_steps() + [
      _row('trail-1', 'u1', 'user_input', 'diverged'),
      _row(
        'trail-1',
        'c2',
        'llm_call',
        _llm_call_body(_output_message('diverged reply')),
        response_id='r2',
      ),
      _row('trail-1', 'a2', 'assistant', 'diverged reply', terminal=True),
    ]
    child_steps = [
      _row('trail-2', 's0', 'system_prompt', 'prompt'),
      _row('trail-2', 'u0', 'user_input', 'continue'),
      _row(
        'trail-2',
        'c1',
        'llm_call',
        _llm_call_body(_output_message('continued')),
        response_id='r3',
      ),
      _row('trail-2', 'a1', 'assistant', 'continued', terminal=True),
    ]
    client = FakeTrailsClient(
      headers=[
        _header(
          'trail-2',
          forked_from={'trail_id': 'trail-1', 'step_id': 'c1'},
        ),
        _header('trail-1'),
      ],
      steps={'trail-1': forked_from_steps, 'trail-2': child_steps},
    )
    history = conversation_history(cast(Any, client), 'trail-2')
    assert [(m.by_user, m.text) for m in history] == [
      (True, 'hello'),
      (False, 'hi back'),
      (True, 'continue'),
      (False, 'continued'),
    ]


class TestResume:
  def _client(self) -> FakeTrailsClient:
    return FakeTrailsClient(
      headers=[_header('trail-1')],
      steps={'trail-1': _forked_from_steps()},
    )

  def test_resumes_the_latest_call_trail_at_its_latest_fork_point(self):
    spec = LLMSpec(model='gpt-5', service_tier='priority')
    with patch('bro.launch.resume.fork') as fork_stub:
      resumed = resume(cast(Any, self._client()), 'record', RESUME_LATEST, llm_spec=spec)
    assert resumed.trail_id == 'trail-1'
    assert [(m.by_user, m.text) for m in resumed.history] == [
      (True, 'hello'),
      (False, 'hi back'),
    ]
    (trail, step_id), kwargs = fork_stub.call_args
    assert trail.header.id == 'trail-1'
    assert step_id == 'c1'
    assert kwargs['llm_spec'] is spec
    assert kwargs['surface'] == 'call'
    # the fetch_forked_from seam resolves ancestors through the same client
    assert kwargs['fetch_forked_from']('trail-1').header.id == 'trail-1'
    assert resumed.bro is fork_stub.return_value

  def test_an_explicit_at_overrides_the_latest_fork_point(self):
    spec = LLMSpec(model='gpt-5', service_tier='priority')
    with patch('bro.launch.resume.fork') as fork_stub:
      resume(cast(Any, self._client()), 'record', 'trail-1', llm_spec=spec, at='b1')
    (_, step_id), _ = fork_stub.call_args
    assert step_id == 'b1'

  def test_rejects_a_trail_of_a_different_bro(self):
    client = FakeTrailsClient(
      headers=[_header('trail-1', bro='other')],
      steps={'trail-1': _forked_from_steps()},
    )
    with pytest.raises(ValueError, match="belongs to bro 'other'"):
      resume(cast(Any, client), 'record', 'trail-1', llm_spec=LLMSpec(model='gpt-5'))

  def test_raises_when_the_bro_has_nothing_to_resume(self):
    client = FakeTrailsClient(headers=[], steps={})
    with pytest.raises(ValueError, match='no call conversation found'):
      resume(cast(Any, client), 'record', RESUME_LATEST, llm_spec=LLMSpec(model='gpt-5'))
