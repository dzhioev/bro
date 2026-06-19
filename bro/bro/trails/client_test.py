import http.client
import json
from typing import Any
from unittest.mock import patch

import pytest

from llm.tracker import Parent, RecordedTrail, Step, Trail
from trails.client import (
  TrailsClient,
  fetch_recorded_trail,
  step_from_row,
  trail_from_header,
)


class _FakeResponse:
  def __init__(self, status: int, body: bytes):
    self.status = status
    self._body = body

  def read(self) -> bytes:
    return self._body


class _FakeConn:
  """programmable stand-in for `http.client.HTTPSConnection`. each entry
  queued via `queue(...)` is consumed on a `request`/`getresponse` pair.
  exceptions simulate transport failures; tuples are HTTP responses.
  """

  def __init__(self) -> None:
    self.queued: list[Any] = []
    self.requests: list[tuple[str, str, bytes | None, dict[str, str]]] = []
    self.closes = 0
    self._pending: tuple[int, bytes] | None = None

  def queue(self, item: Any) -> None:
    self.queued.append(item)

  def request(self, method: str, path: str, body: bytes | None = None, headers=None) -> None:
    self.requests.append((method, path, body, dict(headers) if headers is not None else {}))
    if len(self.queued) == 0:
      raise AssertionError(f'unexpected request: {method} {path}')
    item = self.queued.pop(0)
    if isinstance(item, Exception):
      raise item
    self._pending = item

  def getresponse(self) -> _FakeResponse:
    assert self._pending is not None
    status, body = self._pending
    self._pending = None
    return _FakeResponse(status, body)

  def close(self) -> None:
    self.closes += 1


def _install_fake_conn(monkeypatch) -> _FakeConn:
  fake = _FakeConn()
  monkeypatch.setattr(http.client, 'HTTPSConnection', lambda *a, **k: fake)
  return fake


def _client() -> TrailsClient:
  return TrailsClient('https://trails.example', 'tok')


class TestConstructor:
  def test_rejects_non_https(self):
    with pytest.raises(ValueError, match='https'):
      TrailsClient('http://trails.example', 'tok')


class TestGetTrail:
  def test_get_trail_sends_authed_request(self, monkeypatch):
    fake = _install_fake_conn(monkeypatch)
    fake.queue((200, b'{"trail_id": "T1", "bro": "dev"}'))
    c = _client()
    result = c.get_trail('T1')
    assert result == {'trail_id': 'T1', 'bro': 'dev'}
    method, path, body, headers = fake.requests[0]
    assert (method, path) == ('GET', '/v1/trails/T1')
    assert headers['Authorization'] == 'Bearer tok'
    assert body is None

  def test_http_error_propagates(self, monkeypatch):
    fake = _install_fake_conn(monkeypatch)
    # one failed response + one retry: the client retries once on any failure
    # (transport or HTTP error), and stops after the second attempt.
    fake.queue((404, b'not found'))
    fake.queue((404, b'not found'))
    c = _client()
    with pytest.raises(http.client.HTTPException):
      c.get_trail('missing')


class TestGetSteps:
  def test_includes_after_and_limit(self, monkeypatch):
    fake = _install_fake_conn(monkeypatch)
    fake.queue((200, b'{"steps": [], "next": null}'))
    c = _client()
    c.get_steps('T1', after='s5', limit=20)
    _, path, _, _ = fake.requests[0]
    assert path.startswith('/v1/trails/T1/steps?')
    assert 'after=s5' in path
    assert 'limit=20' in path

  def test_returns_steps_and_next(self, monkeypatch):
    fake = _install_fake_conn(monkeypatch)
    fake.queue(
      (
        200,
        json.dumps(
          {'steps': [{'step_id': 's1', 'kind': 'user_input', 'body': 'hi'}], 'next': 'c1'}
        ).encode(),
      )
    )
    c = _client()
    result = c.get_steps('T1')
    assert result['next'] == 'c1'
    assert result['steps'][0]['kind'] == 'user_input'


class TestIterSteps:
  def test_paginates_until_next_is_none(self, monkeypatch):
    fake = _install_fake_conn(monkeypatch)
    fake.queue((200, json.dumps({'steps': [{'step_id': 's1'}], 'next': 's1'}).encode()))
    fake.queue((200, json.dumps({'steps': [{'step_id': 's2'}], 'next': None}).encode()))
    c = _client()
    steps = list(c.iter_steps('T1'))
    assert [s['step_id'] for s in steps] == ['s1', 's2']
    # second request carried the cursor from the first page
    assert 'after=s1' in fake.requests[1][1]


class TestListTrails:
  def test_passes_all_filters(self, monkeypatch):
    fake = _install_fake_conn(monkeypatch)
    fake.queue((200, b'{"trails": [], "next": null}'))
    c = _client()
    c.list_trails(bro='dev', since='2026-06-01', until='2026-06-30', cursor='c1', limit=10)
    _, path, _, _ = fake.requests[0]
    assert 'bro=dev' in path
    assert 'since=2026-06-01' in path
    assert 'until=2026-06-30' in path
    assert 'cursor=c1' in path
    assert 'limit=10' in path

  def test_parent_and_bro_independent(self, monkeypatch):
    fake = _install_fake_conn(monkeypatch)
    fake.queue((200, b'{"trails": [], "next": null}'))
    c = _client()
    c.list_trails(parent='T-parent')
    _, path, _, _ = fake.requests[0]
    assert 'parent=T-parent' in path


class TestIterTrails:
  def test_max_items_caps_total(self, monkeypatch):
    fake = _install_fake_conn(monkeypatch)
    fake.queue(
      (200, json.dumps({'trails': [{'trail_id': 'T1'}, {'trail_id': 'T2'}], 'next': 'c1'}).encode())
    )
    fake.queue((200, json.dumps({'trails': [{'trail_id': 'T3'}], 'next': None}).encode()))
    c = _client()
    out = list(c.iter_trails(max_items=2))
    assert [t['trail_id'] for t in out] == ['T1', 'T2']

  def test_walks_across_pages(self, monkeypatch):
    fake = _install_fake_conn(monkeypatch)
    fake.queue((200, json.dumps({'trails': [{'trail_id': 'T1'}], 'next': 'c1'}).encode()))
    fake.queue((200, json.dumps({'trails': [{'trail_id': 'T2'}], 'next': None}).encode()))
    c = _client()
    out = list(c.iter_trails())
    assert [t['trail_id'] for t in out] == ['T1', 'T2']
    assert 'cursor=c1' in fake.requests[1][1]


class TestRetryBehavior:
  def test_one_transport_blip_recovered(self, monkeypatch):
    fake = _install_fake_conn(monkeypatch)
    fake.queue(ConnectionError('blip'))
    fake.queue((200, b'{"trail_id": "T1"}'))
    c = _client()
    result = c.get_trail('T1')
    assert result == {'trail_id': 'T1'}
    assert fake.closes >= 1

  def test_second_failure_propagates(self, monkeypatch):
    fake = _install_fake_conn(monkeypatch)
    fake.queue(ConnectionError('blip 1'))
    fake.queue(ConnectionError('blip 2'))
    c = _client()
    with pytest.raises(ConnectionError):
      c.get_trail('T1')


class TestTrailFromHeader:
  def test_minimal_header(self):
    trail = trail_from_header(
      {
        'trail_id': 'T1',
        'bro': 'dev',
        'bro_version': 7,
        'llm_spec': {'type': 'chat_gpt', 'model': 'gpt-5'},
        'started_at': '2026-06-07T00:00:00.000000Z',
        'interactive': False,
        'entry_point': 'cli:bro_run',
        'parent': None,
      }
    )
    assert trail.trail_id == 'T1'
    assert trail.bro == 'dev'
    assert trail.bro_version == 7
    assert trail.parent is None
    assert isinstance(trail, Trail)

  def test_parent_present(self):
    trail = trail_from_header(
      {
        'trail_id': 'T2',
        'bro': 'dev',
        'bro_version': 1,
        'llm_spec': {},
        'started_at': '2026-06-07T00:00:00.000000Z',
        'interactive': True,
        'entry_point': 'fork',
        'parent': {'trail_id': 'T1', 'step_id': 'S5', 'relationship': 'fork'},
      }
    )
    assert isinstance(trail.parent, Parent)
    assert trail.parent.trail_id == 'T1'
    assert trail.parent.step_id == 'S5'
    assert trail.parent.relationship == 'fork'


class TestStepFromRow:
  def test_splits_extras_from_canonical(self):
    step = step_from_row(
      {
        'trail_id': 'T1',
        'step_id': 'S1',
        'ts': '2026-06-07T00:00:00.000000Z',
        'kind': 'tool_call',
        'body': None,
        'tool_name': 'add_task',
        'arguments': {'name': 'x'},
        'call_id': 'c1',
        'turn_index': 1,
      }
    )
    assert isinstance(step, Step)
    assert step.kind == 'tool_call'
    assert step.body is None
    assert step.extras == {
      'tool_name': 'add_task',
      'arguments': {'name': 'x'},
      'call_id': 'c1',
      'turn_index': 1,
    }

  def test_missing_body_defaults_to_none(self):
    step = step_from_row(
      {
        'trail_id': 'T1',
        'step_id': 'S1',
        'ts': '2026-06-07T00:00:00.000000Z',
        'kind': 'end',
      }
    )
    assert step.body is None


class TestFetchRecordedTrail:
  def test_combines_header_and_paginated_steps(self):
    """`fetch_recorded_trail` runs `get_trail` once then walks `iter_steps`
    cursor pages until exhausted, then returns a `RecordedTrail`.
    """
    with (
      patch.object(TrailsClient, 'get_trail') as get_trail,
      patch.object(TrailsClient, 'get_steps') as get_steps,
    ):
      get_trail.return_value = {
        'trail_id': 'T1',
        'bro': 'dev',
        'bro_version': 1,
        'llm_spec': {},
        'started_at': '2026-06-07T00:00:00.000000Z',
        'interactive': False,
        'entry_point': 'cli:bro_run',
        'parent': None,
      }
      get_steps.side_effect = [
        {
          'steps': [
            {
              'trail_id': 'T1',
              'step_id': 'S1',
              'ts': '2026-06-07T00:00:00.000000Z',
              'kind': 'system_prompt',
              'body': 'p',
              'turn_index': 0,
            }
          ],
          'next': 'S1',
        },
        {
          'steps': [
            {
              'trail_id': 'T1',
              'step_id': 'S2',
              'ts': '2026-06-07T00:00:01.000000Z',
              'kind': 'user_input',
              'body': 'hello',
              'turn_index': 0,
            }
          ],
          'next': None,
        },
      ]
      client = _client()
      trail = fetch_recorded_trail(client, 'T1')
      assert isinstance(trail, RecordedTrail)
      assert trail.header.trail_id == 'T1'
      assert [s.kind for s in trail.steps] == ['system_prompt', 'user_input']
      assert [s.step_id for s in trail.steps] == ['S1', 'S2']

  def test_follows_spilled_body_descriptor(self):
    """a step whose body is a `{s3,url,size}` spill descriptor is resolved by
    following the presigned URL, so the rehydrated step carries the full body
    (not the descriptor) — fork replay depends on the complete `response.output`.
    """
    full_body = {'response': {'output': [{'type': 'message', 'content': 'big'}]}}
    descriptor = {'s3': 'trails/T1/steps/S2.json', 'url': 'https://s3/presigned', 'size': 2_000_000}
    with (
      patch.object(TrailsClient, 'get_trail') as get_trail,
      patch.object(TrailsClient, 'get_steps') as get_steps,
      patch.object(TrailsClient, 'fetch_spilled_body') as fetch_spilled,
    ):
      get_trail.return_value = {
        'trail_id': 'T1',
        'bro': 'dev',
        'bro_version': 1,
        'llm_spec': {},
        'started_at': '2026-06-07T00:00:00.000000Z',
        'interactive': False,
        'entry_point': 'cli:bro_run',
        'parent': None,
      }
      get_steps.return_value = {
        'steps': [
          {
            'trail_id': 'T1',
            'step_id': 'S1',
            'ts': '2026-06-07T00:00:00.000000Z',
            'kind': 'user_input',
            'body': 'hi',
          },
          {
            'trail_id': 'T1',
            'step_id': 'S2',
            'ts': '2026-06-07T00:00:01.000000Z',
            'kind': 'llm_call',
            'body': descriptor,
          },
        ],
        'next': None,
      }
      fetch_spilled.return_value = full_body
      trail = fetch_recorded_trail(_client(), 'T1')
      fetch_spilled.assert_called_once_with(descriptor['url'])
      bodies = {s.step_id: s.body for s in trail.steps}
      assert bodies['S1'] == 'hi'
      assert bodies['S2'] == full_body

  def test_inline_body_with_s3_key_is_not_followed(self):
    """a genuine body that merely contains an `s3` key (but not the full
    descriptor triple) is left untouched — only the exact {s3,url,size} shape is
    a spill descriptor.
    """
    with (
      patch.object(TrailsClient, 'get_trail') as get_trail,
      patch.object(TrailsClient, 'get_steps') as get_steps,
      patch.object(TrailsClient, 'fetch_spilled_body') as fetch_spilled,
    ):
      get_trail.return_value = {
        'trail_id': 'T1',
        'bro': 'dev',
        'bro_version': 1,
        'llm_spec': {},
        'started_at': '2026-06-07T00:00:00.000000Z',
        'interactive': False,
        'entry_point': 'cli:bro_run',
        'parent': None,
      }
      lookalike = {'s3': 'some/key'}
      get_steps.return_value = {
        'steps': [
          {
            'trail_id': 'T1',
            'step_id': 'S1',
            'ts': '2026-06-07T00:00:00.000000Z',
            'kind': 'tool_result',
            'body': lookalike,
          },
        ],
        'next': None,
      }
      trail = fetch_recorded_trail(_client(), 'T1')
      fetch_spilled.assert_not_called()
      assert trail.steps[0].body == lookalike
