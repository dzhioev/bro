import http.client
import json
import logging
import threading
import time
from typing import Any, Optional

import pytest

import bro.trails.record.spine as trails_record_spine
from bro.base import configs
from bro.trails.client import HTTPStatusError
from bro.trails.model import ForkedFrom, tools_sha256
from bro.trails.record.bro import Recorder


def _request_payload(request: tuple[str, str, Optional[bytes], dict[str, str]]) -> dict:
  body = request[2]
  assert body is not None
  return json.loads(body)


class _FakeResponse:
  def __init__(self, status: int, body: bytes):
    self.status = status
    self._body = body

  def read(self) -> bytes:
    return self._body


class _FakeConnection:
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


def _install_fake_connection(monkeypatch: pytest.MonkeyPatch) -> _FakeConnection:
  fake = _FakeConnection()
  monkeypatch.setattr(http.client, 'HTTPSConnection', lambda *args, **kwargs: fake)
  monkeypatch.setattr(time, 'sleep', lambda _: None)
  monkeypatch.setattr(trails_record_spine, 'KEEPALIVE_INTERVAL_SECONDS', 3600.0)
  return fake


def _append_response(extent: int, *, appended: int = 1) -> tuple[int, bytes]:
  return 200, json.dumps({'extent': extent, 'appended': appended}).encode()


class TestRecorderConstructor:
  def test_rejects_non_https_url(self):
    with pytest.raises(ValueError, match='https'):
      Recorder('http://bro.trails.example', 'tok')

  def test_rejects_url_without_scheme(self):
    with pytest.raises(ValueError, match='https'):
      Recorder('bro.trails.example', 'tok')


class TestRecorderStartTrail:
  def test_opens_a_universal_body_and_returns_server_trail_id(self, monkeypatch):
    fake = _install_fake_connection(monkeypatch)
    fake.queue((201, b'{"id": "T-server"}'))
    tracker = Recorder('https://bro.trails.example', 'tok')
    trail_id = tracker.start_trail(
      bro='dev',
      llm_spec={'type': 'chat_gpt', 'model': 'gpt-5'},
      system_prompt='do the thing',
      forked_from=None,
      interactive=False,
      surface='ask',
      summoned_by={'trail_id': 'T-forked_from'},
    )

    assert trail_id == 'T-server'
    assert tracker._recording is not None
    assert tracker._recording.trail_id == 'T-server'
    assert tracker._recording.extent == 1
    method, path, _, headers = fake.requests[0]
    assert (method, path) == ('POST', '/v1/trails')
    assert headers['Authorization'] == 'Bearer tok'
    payload = _request_payload(fake.requests[0])
    assert payload['bro'] == 'dev'
    assert payload['version'] == configs.VERSION
    assert payload['native']['llm'] == {'type': 'chat_gpt', 'model': 'gpt-5'}
    assert payload['body'] == {
      'records': [{'kind': 'system_prompt', 'body': 'do the thing', 'turn_index': 0}]
    }
    assert payload['summoned_by'] == {'trail_id': 'T-forked_from'}

  @pytest.mark.parametrize(
    'forked_from, expected',
    [
      (ForkedFrom(trail_id='source', step_id=4), {'trail_id': 'source', 'step_id': 4}),
      (
        ForkedFrom(trail_id='source', step_id=4, index=2),
        {'trail_id': 'source', 'step_id': 4, 'index': 2},
      ),
    ],
  )
  def test_serializes_fork_pointers(self, monkeypatch, forked_from, expected):
    fake = _install_fake_connection(monkeypatch)
    fake.queue((201, b'{"id": "T1"}'))
    tracker = Recorder('https://bro.trails.example', 'tok')
    tracker.start_trail(
      bro='b',
      llm_spec={},
      system_prompt='p',
      forked_from=forked_from,
      interactive=True,
      surface='fork',
    )
    assert _request_payload(fake.requests[0])['forked_from'] == expected

  @pytest.mark.parametrize('failure', [ConnectionError('boom'), (500, b'oops')])
  def test_create_is_not_retried(self, monkeypatch, failure):
    fake = _install_fake_connection(monkeypatch)
    fake.queue(failure)
    tracker = Recorder('https://bro.trails.example', 'tok')
    with pytest.raises((ConnectionError, HTTPStatusError)):
      tracker.start_trail(
        bro='b',
        llm_spec={},
        system_prompt='',
        forked_from=None,
        interactive=False,
        surface='x',
      )
    assert len(fake.requests) == 1


class TestRecorderStep:
  def _ready(self, monkeypatch) -> tuple[Recorder, _FakeConnection]:
    fake = _install_fake_connection(monkeypatch)
    fake.queue((201, b'{"id": "T1"}'))
    tracker = Recorder('https://bro.trails.example', 'tok')
    tracker.start_trail(
      bro='b',
      llm_spec={},
      system_prompt='p',
      forked_from=None,
      interactive=False,
      surface='x',
    )
    return tracker, fake

  def test_step_before_start_trail_raises(self, monkeypatch):
    _install_fake_connection(monkeypatch)
    tracker = Recorder('https://bro.trails.example', 'tok')
    with pytest.raises(RuntimeError, match='before start_trail'):
      tracker.step('user_input', 'hello', turn_index=0)

  def test_appends_at_the_current_offset_and_returns_the_ordinal(self, monkeypatch):
    tracker, fake = self._ready(monkeypatch)
    fake.queue(_append_response(2))
    step_id = tracker.step('tool_result', '4', turn_index=0, call_index=1, call_id='c1')

    assert step_id == 1
    method, path, _, headers = fake.requests[1]
    assert (method, path) == ('POST', '/v1/trails/T1/records')
    assert headers['Authorization'] == 'Bearer tok'
    assert _request_payload(fake.requests[1]) == {
      'offset': 1,
      'records': [
        {
          'kind': 'tool_result',
          'body': '4',
          'turn_index': 0,
          'call_index': 1,
          'call_id': 'c1',
        }
      ],
    }
    assert tracker._recording is not None
    assert tracker._recording.extent == 2

  def test_distinct_steps_advance_the_offset(self, monkeypatch):
    tracker, fake = self._ready(monkeypatch)
    fake.queue(_append_response(2))
    fake.queue(_append_response(3))
    assert tracker.step('user_input', 'a', turn_index=0) == 1
    assert tracker.step('user_input', 'b', turn_index=1) == 2
    assert [_request_payload(request)['offset'] for request in fake.requests[1:]] == [1, 2]

  def test_llm_call_replaces_tools_with_a_content_addressed_blob(self, monkeypatch):
    tracker, fake = self._ready(monkeypatch)
    fake.queue(_append_response(2))
    tools = [{'type': 'function', 'name': 'read'}]
    body = {
      'request': {'model': 'gpt-5', 'input': [], 'tools': tools},
      'response': {'id': 'r1', 'model': 'gpt-5', 'usage': {'input_tokens': 3}},
    }
    tracker.step('llm_call', body, turn_index=0, call_index=1, response_id='r1')

    payload = _request_payload(fake.requests[1])
    sha256 = tools_sha256(tools)
    assert payload['tools'] == {sha256: tools}
    record = payload['records'][0]
    assert record['tools_sha256'] == sha256
    assert record['body']['request'] == {'model': 'gpt-5', 'input': []}
    assert body['request']['tools'] == tools

  @pytest.mark.parametrize('body', ['not an object', {'request': {}}])
  def test_malformed_llm_call_fails_before_writing(self, monkeypatch, body):
    tracker, fake = self._ready(monkeypatch)
    with pytest.raises(ValueError, match='llm_call'):
      tracker.step('llm_call', body)
    assert len(fake.requests) == 1

  def test_retry_uses_the_same_offset_and_record(self, monkeypatch):
    tracker, fake = self._ready(monkeypatch)
    fake.queue(ConnectionError('response lost after commit'))
    fake.queue(_append_response(2, appended=0))
    tracker.step('user_input', 'hello', turn_index=0)
    requests = fake.requests[1:]
    assert len(requests) == 2
    assert _request_payload(requests[0]) == _request_payload(requests[1])
    assert fake.closes >= 1

  def test_propagates_after_exhausting_retries(self, monkeypatch):
    tracker, fake = self._ready(monkeypatch)
    for _ in range(4):
      fake.queue(ConnectionError('always fails'))
    with pytest.raises(ConnectionError):
      tracker.step('user_input', 'hello', turn_index=0)
    assert len(fake.requests[1:]) == 4
    assert tracker._recording is not None
    assert tracker._recording.extent == 1

  def test_unexpected_extent_fails_without_advancing(self, monkeypatch):
    tracker, fake = self._ready(monkeypatch)
    fake.queue(_append_response(3))
    with pytest.raises(RuntimeError, match='expected 2'):
      tracker.step('user_input', 'hello', turn_index=0)
    assert tracker._recording is not None
    assert tracker._recording.extent == 1

  @pytest.mark.parametrize('status', [429, 503])
  def test_retryable_http_status_is_retried(self, monkeypatch, status):
    tracker, fake = self._ready(monkeypatch)
    fake.queue((status, b'transient'))
    fake.queue(_append_response(2))
    tracker.step('user_input', 'hello', turn_index=0)
    assert len(fake.requests[1:]) == 2

  def test_deterministic_4xx_is_not_retried(self, monkeypatch):
    tracker, fake = self._ready(monkeypatch)
    fake.queue((413, b'too large'))
    with pytest.raises(HTTPStatusError) as exception_info:
      tracker.step('llm_call', {'request': {'tools': []}, 'response': {}})
    assert exception_info.value.status == 413
    assert len(fake.requests[1:]) == 1


class TestRecorderEndTrail:
  def _ready(self, monkeypatch) -> tuple[Recorder, _FakeConnection]:
    fake = _install_fake_connection(monkeypatch)
    fake.queue((201, b'{"id": "T1"}'))
    tracker = Recorder('https://bro.trails.example', 'tok')
    tracker.start_trail(
      bro='b', llm_spec={}, system_prompt='p', forked_from=None, interactive=False, surface='x'
    )
    return tracker, fake

  def test_ends_the_header_without_a_legacy_end_step(self, monkeypatch):
    tracker, fake = self._ready(monkeypatch)
    fake.queue((204, b''))
    tracker.end_trail('ok')
    assert fake.requests[1][0:2] == ('POST', '/v1/trails/T1/end')
    assert _request_payload(fake.requests[1]) == {'reason': 'ok'}
    assert tracker._recording is None

  def test_second_end_trail_is_noop(self, monkeypatch):
    tracker, fake = self._ready(monkeypatch)
    fake.queue((204, b''))
    tracker.end_trail('ok')
    tracker.end_trail('raised')
    assert len(fake.requests) == 2

  def test_logs_and_clears_state_on_persistent_failure(self, monkeypatch, caplog):
    tracker, fake = self._ready(monkeypatch)
    for _ in range(4):
      fake.queue(ConnectionError('still down'))
    with caplog.at_level(logging.WARNING):
      tracker.end_trail('ok')
    assert any('end_trail failed' in record.message for record in caplog.records)
    assert tracker._recording is None


class TestRecorderKeepalive:
  def _start(self, monkeypatch, interval: float) -> tuple[Recorder, _FakeConnection]:
    fake = _install_fake_connection(monkeypatch)
    monkeypatch.setattr(trails_record_spine, 'KEEPALIVE_INTERVAL_SECONDS', interval)
    fake.queue((201, b'{"id": "T1"}'))
    tracker = Recorder('https://bro.trails.example', 'tok')
    tracker.start_trail(
      bro='b', llm_spec={}, system_prompt='', forked_from=None, interactive=False, surface='x'
    )
    return tracker, fake

  @staticmethod
  def _keepalive_requests(fake: _FakeConnection) -> list:
    return [request for request in fake.requests if request[1] == '/v1/trails/T1/keepalive']

  def test_keepalive_posts_during_a_quiet_stretch(self, monkeypatch):
    tracker, fake = self._start(monkeypatch, interval=0.02)
    for _ in range(50):
      fake.queue((204, b''))
    deadline = time.monotonic() + 5.0
    while len(self._keepalive_requests(fake)) == 0 and time.monotonic() < deadline:
      threading.Event().wait(0.01)
    assert len(self._keepalive_requests(fake)) > 0
    tracker.end_trail('ok')

  def test_no_keepalive_while_writes_flow(self, monkeypatch):
    tracker, fake = self._start(monkeypatch, interval=0.5)
    for extent in range(2, 7):
      fake.queue(_append_response(extent))
      tracker.step('user_input', 'x', turn_index=extent - 2)
      threading.Event().wait(0.02)
    fake.queue((204, b''))
    tracker.end_trail('ok')
    thread = tracker._keepalive_thread
    assert thread is not None
    thread.join(2.0)
    assert not thread.is_alive()
    assert len(self._keepalive_requests(fake)) == 0

  def test_end_and_close_stop_the_keepalive_thread(self, monkeypatch):
    tracker, fake = self._start(monkeypatch, interval=3600.0)
    thread = tracker._keepalive_thread
    assert thread is not None and thread.is_alive()
    fake.queue((204, b''))
    tracker.end_trail('ok')
    thread.join(2.0)
    assert not thread.is_alive()

    tracker, _ = self._start(monkeypatch, interval=3600.0)
    thread = tracker._keepalive_thread
    assert thread is not None and thread.is_alive()
    tracker.close()
    thread.join(2.0)
    assert not thread.is_alive()

  def test_keepalive_failure_does_not_stop_recording(self, monkeypatch, caplog):
    tracker, fake = self._start(monkeypatch, interval=0.02)
    fake.queue((500, b'oops'))
    fake.queue((500, b'oops'))
    deadline = time.monotonic() + 5.0
    with caplog.at_level(logging.WARNING):
      while len(self._keepalive_requests(fake)) < 2 and time.monotonic() < deadline:
        threading.Event().wait(0.01)
      monkeypatch.setattr(trails_record_spine, 'KEEPALIVE_INTERVAL_SECONDS', 3600.0)
      threading.Event().wait(0.2)
    assert any('keepalive failed' in record.message for record in caplog.records)
    for item in (_append_response(2), _append_response(2), (204, b'')):
      fake.queue(item)
    tracker.step('user_input', 'x', turn_index=0)
    tracker.end_trail('ok')
