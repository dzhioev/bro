import http.client
import json
from typing import Any, Optional
from unittest.mock import patch

import pytest

from bro.trails.client import (
  HTTPStatusError,
  TrailsClient,
  fetch_recorded_trail,
  step_from_row,
  trail_from_header,
)
from bro.trails.model import ForkedFrom, RecordedTrail, Step, Trail


class _FakeResponse:
  def __init__(self, status: int, body: bytes):
    self.status = status
    self._body = body

  def read(self) -> bytes:
    return self._body


class _FakeConnection:
  """programmable stand-in for `http.client.HTTPSConnection`. each entry
  queued via `queue(...)` is consumed on a `request`/`getresponse` pair.
  exceptions simulate transport failures; tuples are HTTP responses.
  """

  def __init__(self) -> None:
    self.queued: list[Any] = []
    self.requests: list[tuple[str, str, Optional[bytes], dict[str, str]]] = []
    self.closes = 0
    self._pending: Optional[tuple[int, bytes]] = None

  def queue(self, item: Any) -> None:
    self.queued.append(item)

  def request(self, method: str, path: str, body: Optional[bytes] = None, headers=None) -> None:
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


def _install_fake_connection(monkeypatch) -> _FakeConnection:
  fake = _FakeConnection()
  monkeypatch.setattr(http.client, 'HTTPSConnection', lambda *a, **k: fake)
  return fake


def _client() -> TrailsClient:
  return TrailsClient('https://bro.trails.example', 'tok')


class TestConstructor:
  def test_rejects_non_https(self):
    with pytest.raises(ValueError, match='https'):
      TrailsClient('http://bro.trails.example', 'tok')


class TestGetTrail:
  def test_get_trail_sends_authed_request(self, monkeypatch):
    fake = _install_fake_connection(monkeypatch)
    fake.queue((200, b'{"trail_id": "T1", "bro": "dev"}'))
    c = _client()
    result = c.get_trail('T1')
    assert result == {'trail_id': 'T1', 'bro': 'dev'}
    method, path, body, headers = fake.requests[0]
    assert (method, path) == ('GET', '/v1/trails/T1')
    assert headers['Authorization'] == 'Bearer tok'
    assert body is None

  def test_deterministic_http_error_propagates_without_retry(self, monkeypatch):
    fake = _install_fake_connection(monkeypatch)
    fake.queue((404, b'not found'))
    c = _client()
    with pytest.raises(HTTPStatusError) as exception_info:
      c.get_trail('missing')
    assert exception_info.value.status == 404
    assert len(fake.requests) == 1


class TestLineageLookups:
  def test_finds_step_identities_by_repeated_uuid_query(self, monkeypatch):
    fake = _install_fake_connection(monkeypatch)
    fake.queue((200, b'{"steps": [{"trail_id": "T1", "step_id": 2, "uuid": "u2"}]}'))
    result = _client().find_steps_by_uuid({'u2', 'u1'})
    assert result == [{'trail_id': 'T1', 'step_id': 2, 'uuid': 'u2'}]
    assert fake.requests[0][1] == '/v1/steps?uuid=u1&uuid=u2'

  def test_empty_uuid_lookup_needs_no_request(self, monkeypatch):
    fake = _install_fake_connection(monkeypatch)
    assert _client().find_steps_by_uuid(set()) == []
    assert fake.requests == []

  def test_reads_one_step_and_bounded_uuid_projection(self, monkeypatch):
    fake = _install_fake_connection(monkeypatch)
    fake.queue((200, b'{"step_id": 4, "raw": "line"}'))
    fake.queue((200, b'{"steps": [{"step_id": 4, "uuid": "u4"}]}'))
    client = _client()
    assert client.get_step('T1', 4)['raw'] == 'line'
    assert client.get_step_uuids('T1', through=4) == [{'step_id': 4, 'uuid': 'u4'}]
    assert fake.requests[0][1] == '/v1/trails/T1/steps/4'
    assert fake.requests[1][1] == '/v1/trails/T1/steps/uuids?through=4'


class TestGetSteps:
  def test_includes_after_and_limit(self, monkeypatch):
    fake = _install_fake_connection(monkeypatch)
    fake.queue((200, b'{"steps": [], "next": null}'))
    c = _client()
    c.get_steps('T1', after=5, limit=20)
    _, path, _, _ = fake.requests[0]
    assert path.startswith('/v1/trails/T1/steps?')
    assert 'after=5' in path
    assert 'limit=20' in path

  def test_returns_steps_and_next(self, monkeypatch):
    fake = _install_fake_connection(monkeypatch)
    fake.queue(
      (
        200,
        json.dumps(
          {'steps': [{'step_id': 1, 'kind': 'user_input', 'body': 'hi'}], 'next': 2}
        ).encode(),
      )
    )
    c = _client()
    result = c.get_steps('T1')
    assert result['next'] == 2
    assert result['steps'][0]['kind'] == 'user_input'


class TestIterSteps:
  def test_after_starts_past_the_cursor(self, monkeypatch):
    fake = _install_fake_connection(monkeypatch)
    fake.queue((200, json.dumps({'steps': [{'step_id': 2}], 'next': None}).encode()))
    c = _client()
    steps = list(c.iter_steps('T1', after=1))
    assert [s['step_id'] for s in steps] == [2]
    assert 'after=1' in fake.requests[0][1]

  def test_paginates_until_next_is_none(self, monkeypatch):
    fake = _install_fake_connection(monkeypatch)
    fake.queue((200, json.dumps({'steps': [{'step_id': 1}], 'next': 1}).encode()))
    fake.queue((200, json.dumps({'steps': [{'step_id': 2}], 'next': None}).encode()))
    c = _client()
    steps = list(c.iter_steps('T1'))
    assert [s['step_id'] for s in steps] == [1, 2]
    # second request carried the cursor from the first page
    assert 'after=1' in fake.requests[1][1]


class TestListTrails:
  def test_passes_all_filters(self, monkeypatch):
    fake = _install_fake_connection(monkeypatch)
    fake.queue((200, b'{"trails": [], "next": null}'))
    c = _client()
    c.list_trails(bro='dev', since='2026-06-01', until='2026-06-30', cursor='c1', limit=10)
    _, path, _, _ = fake.requests[0]
    assert 'bro=dev' in path
    assert 'since=2026-06-01' in path
    assert 'until=2026-06-30' in path
    assert 'cursor=c1' in path
    assert 'limit=10' in path

  def test_forked_from_and_bro_independent(self, monkeypatch):
    fake = _install_fake_connection(monkeypatch)
    fake.queue((200, b'{"trails": [], "next": null}'))
    c = _client()
    c.list_trails(forked_from='T-forked_from')
    _, path, _, _ = fake.requests[0]
    assert 'forked_from=T-forked_from' in path


class TestIterTrails:
  def test_max_items_caps_total(self, monkeypatch):
    fake = _install_fake_connection(monkeypatch)
    fake.queue((200, json.dumps({'trails': [{'id': 'T1'}, {'id': 'T2'}], 'next': 'c1'}).encode()))
    fake.queue((200, json.dumps({'trails': [{'trail_id': 'T3'}], 'next': None}).encode()))
    c = _client()
    out = list(c.iter_trails(max_items=2))
    assert [t['id'] for t in out] == ['T1', 'T2']

  def test_walks_across_pages(self, monkeypatch):
    fake = _install_fake_connection(monkeypatch)
    fake.queue((200, json.dumps({'trails': [{'id': 'T1'}], 'next': 'c1'}).encode()))
    fake.queue((200, json.dumps({'trails': [{'id': 'T2'}], 'next': None}).encode()))
    c = _client()
    out = list(c.iter_trails())
    assert [t['id'] for t in out] == ['T1', 'T2']
    assert 'cursor=c1' in fake.requests[1][1]


class TestRetryBehavior:
  def test_one_transport_blip_recovered(self, monkeypatch):
    fake = _install_fake_connection(monkeypatch)
    fake.queue(ConnectionError('blip'))
    fake.queue((200, b'{"id": "T1"}'))
    c = _client()
    result = c.get_trail('T1')
    assert result == {'id': 'T1'}
    assert fake.closes >= 1

  def test_second_failure_propagates(self, monkeypatch):
    fake = _install_fake_connection(monkeypatch)
    fake.queue(ConnectionError('blip 1'))
    fake.queue(ConnectionError('blip 2'))
    c = _client()
    with pytest.raises(ConnectionError):
      c.get_trail('T1')

  def test_retryable_status_recovered(self, monkeypatch):
    fake = _install_fake_connection(monkeypatch)
    fake.queue((503, b'unavailable'))
    fake.queue((200, b'{"id": "T1"}'))
    c = _client()
    assert c.get_trail('T1') == {'id': 'T1'}

  def test_persistent_retryable_status_propagates(self, monkeypatch):
    fake = _install_fake_connection(monkeypatch)
    fake.queue((503, b'unavailable 1'))
    fake.queue((503, b'unavailable 2'))
    c = _client()
    with pytest.raises(HTTPStatusError) as exception_info:
      c.get_trail('T1')
    assert exception_info.value.status == 503


class TestLaunchContext:
  def test_unwraps_the_context_document(self, monkeypatch):
    fake = _install_fake_connection(monkeypatch)
    fake.queue((200, b'{"launch_context": [{"title": "git state"}]}'))
    assert _client().get_launch_context('T1') == [{'title': 'git state'}]
    method, path, _, _ = fake.requests[0]
    assert (method, path) == ('GET', '/v1/trails/T1/context')

  def test_absent_context_is_none(self, monkeypatch):
    fake = _install_fake_connection(monkeypatch)
    fake.queue((404, b'no launch context'))
    assert _client().get_launch_context('T1') is None


class TestWrites:
  def test_create_trail_posts_the_payload_verbatim(self, monkeypatch):
    fake = _install_fake_connection(monkeypatch)
    fake.queue((201, b'{"id": "T1", "started_at": "2026-01-01T00:00:00Z"}'))
    payload = {'harness': 'claude', 'body': {'records': []}}
    result = _client().create_trail(payload)
    assert result['id'] == 'T1'
    method, path, body, headers = fake.requests[0]
    assert (method, path) == ('POST', '/v1/trails')
    assert headers['Content-Type'] == 'application/json'
    assert body is not None and json.loads(body) == payload

  def test_create_trail_is_not_retried(self, monkeypatch):
    # a lost create response must not double-create; the caller's own next
    # attempt is the retry
    fake = _install_fake_connection(monkeypatch)
    fake.queue(ConnectionError('blip'))
    with pytest.raises(ConnectionError):
      _client().create_trail({'harness': 'claude'})
    assert len(fake.requests) == 1

  def test_append_records_posts_offset_records_and_tool_blobs(self, monkeypatch):
    fake = _install_fake_connection(monkeypatch)
    fake.queue((200, b'{"extent": 2, "appended": 1}'))
    result = _client().append_records(
      'T1',
      1,
      [{'kind': 'user_input', 'body': 'hello'}],
      tools={'abc': [{'name': 'read'}]},
    )
    assert result == {'extent': 2, 'appended': 1}
    method, path, body, _ = fake.requests[0]
    assert (method, path) == ('POST', '/v1/trails/T1/records')
    assert body is not None and json.loads(body) == {
      'offset': 1,
      'records': [{'kind': 'user_input', 'body': 'hello'}],
      'tools': {'abc': [{'name': 'read'}]},
    }

  def test_admin_operations_use_the_server_seam(self, monkeypatch):
    fake = _install_fake_connection(monkeypatch)
    fake.queue((200, b'{"extent": 2}'))
    fake.queue((200, b'{"ok": true}'))
    fake.queue((200, b'{"extent": 1}'))
    client = _client()
    assert client.recompute('T1') == {'extent': 2}
    assert client.check('T1') == {'ok': True}
    assert client.relink('T1', {'trail_id': 'parent', 'step_id': 4}, 1) == {'extent': 1}
    assert [request[1] for request in fake.requests] == [
      '/v1/admin/trails/T1/recompute',
      '/v1/admin/trails/check',
      '/v1/admin/trails/T1/relink',
    ]

  def test_update_header_patches_changes(self, monkeypatch):
    fake = _install_fake_connection(monkeypatch)
    fake.queue((200, b'{"id": "T1", "subject": "s"}'))
    result = _client().update_header('T1', {'subject': 's'})
    assert result['subject'] == 's'
    method, path, body, _ = fake.requests[0]
    assert (method, path) == ('PATCH', '/v1/trails/T1')
    assert body is not None and json.loads(body) == {'subject': 's'}

  def test_end_trail_carries_reason_and_detail(self, monkeypatch):
    fake = _install_fake_connection(monkeypatch)
    fake.queue((204, b''))
    _client().end_trail('T1', 'raised', detail='blocked')
    method, path, body, _ = fake.requests[0]
    assert (method, path) == ('POST', '/v1/trails/T1/end')
    assert body is not None and json.loads(body) == {'reason': 'raised', 'detail': 'blocked'}

  def test_keepalive_posts_empty_payload(self, monkeypatch):
    fake = _install_fake_connection(monkeypatch)
    fake.queue((204, b''))
    _client().keepalive('T1')
    method, path, body, _ = fake.requests[0]
    assert (method, path) == ('POST', '/v1/trails/T1/keepalive')
    assert body is not None and json.loads(body) == {}

  def test_idempotent_write_recovers_one_transport_blip(self, monkeypatch):
    fake = _install_fake_connection(monkeypatch)
    fake.queue(ConnectionError('blip'))
    fake.queue((204, b''))
    _client().keepalive('T1')
    assert len(fake.requests) == 2


class TestMessages:
  def test_repeated_type_filters(self, monkeypatch):
    fake = _install_fake_connection(monkeypatch)
    fake.queue((200, b'{"messages": [], "next": null}'))
    client = _client()
    client.get_messages('T1', types={'assistant', 'user_input'}, after=5, limit=20)
    _, path, _, _ = fake.requests[0]
    assert path.startswith('/v1/trails/T1/messages?')
    assert 'type=assistant' in path
    assert 'type=user_input' in path
    assert 'after=5' in path
    assert 'limit=20' in path

  def test_iterator_pages(self, monkeypatch):
    fake = _install_fake_connection(monkeypatch)
    fake.queue((200, b'{"messages": [{"type": "user_input"}], "next": "1"}'))
    fake.queue((200, b'{"messages": [{"type": "assistant"}], "next": null}'))
    client = _client()
    assert [message['type'] for message in client.iter_messages('T1')] == [
      'user_input',
      'assistant',
    ]
    assert 'after=1' in fake.requests[1][1]


class TestTrailFromHeader:
  def test_minimal_header(self):
    trail = trail_from_header(
      {
        'id': 'T1',
        'harness': 'bro',
        'bro': 'dev',
        'version': str(7),
        'native': {'llm': {'type': 'chat_gpt', 'model': 'gpt-5'}},
        'started_at': '2026-06-07T00:00:00.000000Z',
        'interactive': False,
        'surface': 'ask',
        'forked_from': None,
      }
    )
    assert trail.id == 'T1'
    assert trail.bro == 'dev'
    assert trail.version == '7'
    assert trail.forked_from is None
    assert trail.summoned_by is None
    assert isinstance(trail, Trail)

  def test_forked_from_present(self):
    trail = trail_from_header(
      {
        'id': 'T2',
        'harness': 'bro',
        'bro': 'dev',
        'version': str(1),
        'native': {'llm': {}},
        'started_at': '2026-06-07T00:00:00.000000Z',
        'interactive': True,
        'surface': 'fork',
        'forked_from': {'trail_id': 'T1', 'step_id': 5, 'index': 2},
        'summoned_by': {'trail_id': 'T-root'},
      }
    )
    assert isinstance(trail.forked_from, ForkedFrom)
    assert trail.forked_from.trail_id == 'T1'
    assert trail.forked_from.step_id == 5
    assert trail.forked_from.index == 2
    assert trail.summoned_by == {'trail_id': 'T-root'}


class TestStepFromRow:
  def test_splits_extras_from_canonical(self):
    step = step_from_row(
      {
        'trail_id': 'T1',
        'step_id': 1,
        'ts': '2026-06-07T00:00:00.000000Z',
        'kind': 'tool_call',
        'body': None,
        'tool_name': 'add_task',
        'arguments': {'name': 'x'},
        'call_id': 'c1',
        'turn_index': 1,
        'usage': {'output_tokens': 2},
        'payload_sha256': 'digest',
      }
    )
    assert isinstance(step, Step)
    assert step.kind == 'tool_call'
    assert step.body is None
    assert step.usage == {'output_tokens': 2}
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
        'step_id': 1,
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
        'id': 'T1',
        'harness': 'bro',
        'bro': 'dev',
        'version': str(1),
        'native': {'llm': {}},
        'started_at': '2026-06-07T00:00:00.000000Z',
        'interactive': False,
        'surface': 'ask',
        'forked_from': None,
      }
      get_steps.side_effect = [
        {
          'steps': [
            {
              'trail_id': 'T1',
              'step_id': 1,
              'ts': '2026-06-07T00:00:00.000000Z',
              'kind': 'system_prompt',
              'body': 'p',
              'turn_index': 0,
            }
          ],
          'next': 1,
        },
        {
          'steps': [
            {
              'trail_id': 'T1',
              'step_id': 2,
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
      assert trail.header.id == 'T1'
      assert [s.kind for s in trail.steps] == ['system_prompt', 'user_input']
      assert [s.step_id for s in trail.steps] == [1, 2]

  def test_follows_spilled_body_descriptor(self):
    """a step whose body is a `{s3,url,size}` spill descriptor is resolved by
    following the presigned URL, so the rehydrated step carries the full body
    (not the descriptor) — fork replay depends on the complete `response.output`.
    """
    full_body = {'response': {'output': [{'type': 'message', 'content': 'big'}]}}
    descriptor = {
      's3': 'trails/steps/T1/2-abc123.json',
      'url': 'https://s3/presigned',
      'size': 2_000_000,
    }
    with (
      patch.object(TrailsClient, 'get_trail') as get_trail,
      patch.object(TrailsClient, 'get_steps') as get_steps,
      patch.object(TrailsClient, 'fetch_spilled_body') as fetch_spilled,
    ):
      get_trail.return_value = {
        'id': 'T1',
        'harness': 'bro',
        'bro': 'dev',
        'version': str(1),
        'native': {'llm': {}},
        'started_at': '2026-06-07T00:00:00.000000Z',
        'interactive': False,
        'surface': 'ask',
        'forked_from': None,
      }
      get_steps.return_value = {
        'steps': [
          {
            'trail_id': 'T1',
            'step_id': 1,
            'ts': '2026-06-07T00:00:00.000000Z',
            'kind': 'user_input',
            'body': 'hi',
          },
          {
            'trail_id': 'T1',
            'step_id': 2,
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
      assert bodies[1] == 'hi'
      assert bodies[2] == full_body

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
        'id': 'T1',
        'harness': 'bro',
        'bro': 'dev',
        'version': str(1),
        'native': {'llm': {}},
        'started_at': '2026-06-07T00:00:00.000000Z',
        'interactive': False,
        'surface': 'ask',
        'forked_from': None,
      }
      lookalike = {'s3': 'some/key'}
      get_steps.return_value = {
        'steps': [
          {
            'trail_id': 'T1',
            'step_id': 1,
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
