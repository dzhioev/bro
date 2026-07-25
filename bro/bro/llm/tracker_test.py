import http.client
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

import pytest

import llm.tracker
from base import configs
from llm.tracker import (
  ForkedFrom,
  HTTPStatusError,
  HTTPTracker,
  LocalFileTracker,
  NullTracker,
  Tracker,
  read_local_file,
)


def _read_jsonl(path: Path) -> list[dict]:
  return [json.loads(line) for line in path.read_text().splitlines() if len(line) > 0]


def _request_payload(request: tuple[str, str, Optional[bytes], dict[str, str]]) -> dict:
  body = request[2]
  assert body is not None
  return json.loads(body)


class TestNullTracker:
  def test_start_trail_returns_empty_string(self):
    t = NullTracker()
    trail_id = t.start_trail(
      bro='b', llm_spec={}, system_prompt='', forked_from=None, interactive=False, surface='x'
    )
    assert trail_id == ''

  def test_methods_are_noops(self):
    t = NullTracker()
    t.start_trail(
      bro='b', llm_spec={}, system_prompt='p', forked_from=None, interactive=True, surface='x'
    )
    assert t.step('reasoning', 'r', turn_index=1) is None
    t.step('end', {'reason': 'ok'})
    t.end_trail('ok')


class TestLocalFileTrackerStartTrail:
  def test_writes_header_line_with_metadata(self, tmp_path: Path):
    path = tmp_path / 'trail.jsonl'
    tracker = LocalFileTracker(path)
    trail_id = tracker.start_trail(
      bro='echo',
      llm_spec={'type': 'echo', 'model': 'm'},
      system_prompt='do the thing',
      forked_from=None,
      interactive=False,
      surface='ask',
      summoned_by={'session': 'c:root'},
    )
    tracker.close()
    records = _read_jsonl(path)
    # header line + the auto-emitted system_prompt step
    assert len(records) == 2
    header = records[0]
    assert header['record_type'] == 'trail'
    assert header['id'] == trail_id
    assert header['bro'] == 'echo'
    assert header['version'] == configs.VERSION
    assert header['native']['llm'] == {'type': 'echo', 'model': 'm'}
    assert header['interactive'] is False
    assert header['surface'] == 'ask'
    assert header['forked_from'] is None
    assert header['summoned_by'] == {'session': 'c:root'}
    assert 'started_at' in header
    [recorded] = read_local_file(path)
    assert recorded.header.summoned_by == {'session': 'c:root'}

  def test_auto_emits_system_prompt_as_first_step(self, tmp_path: Path):
    path = tmp_path / 'trail.jsonl'
    tracker = LocalFileTracker(path)
    trail_id = tracker.start_trail(
      bro='b',
      llm_spec={},
      system_prompt='full prompt text',
      forked_from=None,
      interactive=False,
      surface='ask',
    )
    tracker.close()
    records = _read_jsonl(path)
    step = records[1]
    assert step['record_type'] == 'step'
    assert step['trail_id'] == trail_id
    assert step['kind'] == 'system_prompt'
    assert step['body'] == 'full prompt text'
    assert step['turn_index'] == 0
    assert 'step_id' in step
    assert 'ts' in step

  def test_forked_from_is_serialized_when_present(self, tmp_path: Path):
    path = tmp_path / 'trail.jsonl'
    tracker = LocalFileTracker(path)
    forked_from = ForkedFrom(
      trail_id='abc',
      step_id='def',
    )
    tracker.start_trail(
      bro='b',
      llm_spec={},
      system_prompt='',
      forked_from=forked_from,
      interactive=False,
      surface='ask',
    )
    tracker.close()
    header = _read_jsonl(path)[0]
    assert header['forked_from'] == {'trail_id': 'abc', 'step_id': 'def'}
    assert 'summoned_by' not in header

  def test_bro_version_comes_from_configs(self, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(configs, 'VERSION', '42')
    path = tmp_path / 'trail.jsonl'
    tracker = LocalFileTracker(path)
    tracker.start_trail(
      bro='b',
      llm_spec={},
      system_prompt='',
      forked_from=None,
      interactive=False,
      surface='ask',
    )
    tracker.close()
    assert _read_jsonl(path)[0]['version'] == '42'


class TestLocalFileTrackerStep:
  def test_appends_step_with_extras(self, tmp_path: Path):
    path = tmp_path / 'trail.jsonl'
    tracker = LocalFileTracker(path)
    tracker.start_trail(
      bro='b',
      llm_spec={},
      system_prompt='p',
      forked_from=None,
      interactive=False,
      surface='ask',
    )
    tracker.step(
      'tool_call',
      None,
      tool_name='add_task',
      arguments={'name': 'x'},
      call_id='c1',
      turn_index=1,
    )
    tracker.close()
    records = _read_jsonl(path)
    # header + system_prompt step + tool_call step
    assert len(records) == 3
    step = records[2]
    assert step['kind'] == 'tool_call'
    assert step['body'] is None
    assert step['tool_name'] == 'add_task'
    assert step['arguments'] == {'name': 'x'}
    assert step['call_id'] == 'c1'
    assert step['turn_index'] == 1

  def test_step_before_start_trail_raises(self, tmp_path: Path):
    tracker = LocalFileTracker(tmp_path / 'trail.jsonl')
    with pytest.raises(RuntimeError):
      tracker.step('reasoning', 'thinking')

  def test_each_step_gets_unique_step_id(self, tmp_path: Path):
    path = tmp_path / 'trail.jsonl'
    tracker = LocalFileTracker(path)
    tracker.start_trail(
      bro='b',
      llm_spec={},
      system_prompt='p',
      forked_from=None,
      interactive=False,
      surface='ask',
    )
    tracker.step('reasoning', 'a')
    tracker.step('reasoning', 'b')
    tracker.close()
    records = _read_jsonl(path)
    step_ids = [r['step_id'] for r in records if r['record_type'] == 'step']
    assert len(set(step_ids)) == len(step_ids)

  def test_step_returns_the_written_step_id(self, tmp_path: Path):
    path = tmp_path / 'trail.jsonl'
    tracker = LocalFileTracker(path)
    tracker.start_trail(
      bro='b',
      llm_spec={},
      system_prompt='p',
      forked_from=None,
      interactive=False,
      surface='ask',
    )
    returned = tracker.step('reasoning', 'a')
    tracker.close()
    records = _read_jsonl(path)
    assert returned == records[-1]['step_id']


class TestLocalFileTrackerEndTrail:
  def test_emits_end_step(self, tmp_path: Path):
    path = tmp_path / 'trail.jsonl'
    tracker = LocalFileTracker(path)
    tracker.start_trail(
      bro='b',
      llm_spec={},
      system_prompt='p',
      forked_from=None,
      interactive=False,
      surface='ask',
    )
    tracker.end_trail('ok')
    tracker.close()
    records = _read_jsonl(path)
    end = records[-1]
    assert end['record_type'] == 'step'
    assert end['kind'] == 'end'
    assert end['body'] == {'reason': 'ok'}

  def test_second_end_trail_is_noop(self, tmp_path: Path):
    path = tmp_path / 'trail.jsonl'
    tracker = LocalFileTracker(path)
    tracker.start_trail(
      bro='b',
      llm_spec={},
      system_prompt='p',
      forked_from=None,
      interactive=False,
      surface='ask',
    )
    tracker.end_trail('ok')
    tracker.end_trail('raised')
    tracker.close()
    end_records = [r for r in _read_jsonl(path) if r.get('kind') == 'end']
    assert len(end_records) == 1


class TestLocalFileTrackerAppend:
  def test_multiple_trails_coexist_in_one_file(self, tmp_path: Path):
    path = tmp_path / 'trail.jsonl'
    tracker = LocalFileTracker(path)
    first = tracker.start_trail(
      bro='a',
      llm_spec={},
      system_prompt='p1',
      forked_from=None,
      interactive=False,
      surface='ask',
    )
    tracker.end_trail('ok')
    second = tracker.start_trail(
      bro='b',
      llm_spec={},
      system_prompt='p2',
      forked_from=None,
      interactive=False,
      surface='ask',
    )
    tracker.end_trail('ok')
    tracker.close()
    records = _read_jsonl(path)
    header_ids = {record['id'] for record in records if record['record_type'] == 'trail'}
    step_trail_ids = {record['trail_id'] for record in records if record['record_type'] == 'step'}
    assert header_ids == {first, second}
    assert step_trail_ids == {first, second}
    assert first != second


class TestTrackerIsABC:
  def test_cannot_instantiate_base_class(self):
    with pytest.raises(TypeError):
      Tracker()  # type: ignore[abstract]


class _FakeResponse:
  def __init__(self, status: int, body: bytes):
    self.status = status
    self._body = body

  def read(self) -> bytes:
    return self._body


class _FakeConnection:
  """programmable stand-in for `http.client.HTTPSConnection`.

  each entry queued via `queue(...)` represents one round-trip and is consumed
  on a `request` / `getresponse` pair. an `Exception` instance simulates a
  transport-level failure raised from `request`; a `(status, body)` tuple
  serves as a successful HTTP response.
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


def _install_fake_connection(monkeypatch: pytest.MonkeyPatch) -> _FakeConnection:
  fake = _FakeConnection()
  # the tracker re-opens after every drop_connection; tests use a single shared fake
  # because the queued response semantics already capture the per-attempt state.
  monkeypatch.setattr(http.client, 'HTTPSConnection', lambda *args, **kwargs: fake)
  # the retry loop sleeps between attempts; skip the wall-clock wait so tests
  # finish in microseconds rather than seconds.
  monkeypatch.setattr(time, 'sleep', lambda _: None)
  # park the keepalive thread of any trail started under this fake: at the
  # default cadence it would outlive the test's wiring and hit the real
  # network. keepalive tests override the interval downwards themselves.
  monkeypatch.setattr(llm.tracker, 'KEEPALIVE_INTERVAL_SECONDS', 3600.0)
  return fake


class TestHTTPTrackerConstructor:
  def test_rejects_non_https_url(self):
    with pytest.raises(ValueError, match='https'):
      HTTPTracker('http://trails.example', 'tok')

  def test_rejects_url_without_scheme(self):
    with pytest.raises(ValueError, match='https'):
      HTTPTracker('trails.example', 'tok')


class TestHTTPTrackerStartTrail:
  def test_posts_v1_trails_and_returns_server_trail_id(self, monkeypatch):
    fake = _install_fake_connection(monkeypatch)
    fake.queue((201, b'{"id": "T-server"}'))
    tracker = HTTPTracker('https://trails.example', 'tok')
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
    assert tracker._trail_id == 'T-server'
    assert len(fake.requests) == 1
    method, path, body, headers = fake.requests[0]
    assert (method, path) == ('POST', '/v1/trails')
    assert headers['Authorization'] == 'Bearer tok'
    assert headers['Content-Type'] == 'application/json'
    assert body is not None
    payload = json.loads(body)
    assert payload['bro'] == 'dev'
    assert payload['version'] == configs.VERSION
    assert payload['native']['llm'] == {'type': 'chat_gpt', 'model': 'gpt-5'}
    assert payload['body']['system_prompt'] == 'do the thing'
    assert payload['forked_from'] is None
    assert payload['interactive'] is False
    assert payload['surface'] == 'ask'
    assert payload['summoned_by'] == {'trail_id': 'T-forked_from'}

  def test_serializes_forked_from_on_forks(self, monkeypatch):
    fake = _install_fake_connection(monkeypatch)
    fake.queue((201, b'{"id": "T1"}'))
    tracker = HTTPTracker('https://trails.example', 'tok')
    forked_from = ForkedFrom(
      trail_id='abc',
      step_id='def',
    )
    tracker.start_trail(
      bro='b',
      llm_spec={},
      system_prompt='p',
      forked_from=forked_from,
      interactive=True,
      surface='fork',
    )
    body = fake.requests[0][2]
    assert body is not None
    payload = json.loads(body)
    assert payload['forked_from'] == {'trail_id': 'abc', 'step_id': 'def'}
    assert 'summoned_by' not in payload

  def test_fail_fast_no_retries_on_transport_error(self, monkeypatch):
    fake = _install_fake_connection(monkeypatch)
    fake.queue(ConnectionError('boom'))
    tracker = HTTPTracker('https://trails.example', 'tok')
    with pytest.raises(ConnectionError):
      tracker.start_trail(
        bro='b',
        llm_spec={},
        system_prompt='',
        forked_from=None,
        interactive=False,
        surface='x',
      )
    assert len(fake.requests) == 1

  def test_fail_fast_no_retries_on_http_error(self, monkeypatch):
    fake = _install_fake_connection(monkeypatch)
    fake.queue((500, b'oops'))
    tracker = HTTPTracker('https://trails.example', 'tok')
    with pytest.raises(HTTPStatusError) as exception_info:
      tracker.start_trail(
        bro='b',
        llm_spec={},
        system_prompt='',
        forked_from=None,
        interactive=False,
        surface='x',
      )
    assert exception_info.value.status == 500
    assert len(fake.requests) == 1


class TestHTTPTrackerStep:
  def _ready(self, monkeypatch) -> tuple[HTTPTracker, _FakeConnection]:
    fake = _install_fake_connection(monkeypatch)
    fake.queue((201, b'{"id": "T1"}'))
    tracker = HTTPTracker('https://trails.example', 'tok')
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
    tracker = HTTPTracker('https://trails.example', 'tok')
    with pytest.raises(RuntimeError):
      tracker.step('reasoning', 'thinking')

  def test_posts_v1_steps_with_kind_body_extras(self, monkeypatch):
    tracker, fake = self._ready(monkeypatch)
    fake.queue((204, b''))
    tracker.step(
      'tool_call',
      None,
      tool_name='add_task',
      arguments={'name': 'x'},
      call_id='c1',
      turn_index=1,
    )
    method, path, body, headers = fake.requests[1]
    assert (method, path) == ('POST', '/v1/trails/T1/steps')
    assert headers['Authorization'] == 'Bearer tok'
    assert body is not None
    payload = json.loads(body)
    assert payload['kind'] == 'tool_call'
    assert payload['body'] is None
    assert payload['tool_name'] == 'add_task'
    assert payload['arguments'] == {'name': 'x'}
    assert payload['call_id'] == 'c1'
    assert payload['turn_index'] == 1
    # the client mints the step_id so the server can dedup retries on it.
    assert isinstance(payload['step_id'], str) and len(payload['step_id']) > 0

  def test_retries_transient_blips_and_recovers(self, monkeypatch):
    tracker, fake = self._ready(monkeypatch)
    fake.queue(ConnectionError('blip 1'))
    fake.queue(ConnectionError('blip 2'))
    fake.queue((204, b''))
    tracker.step('reasoning', 'thinking', turn_index=1)
    # initial attempt + 2 retries = 3 requests; the third one succeeded so the
    # remaining 2s retry slot was never used.
    step_requests = [r for r in fake.requests if r[1].endswith('/steps')]
    assert len(step_requests) == 3

  def test_propagates_after_exhausting_retries(self, monkeypatch):
    tracker, fake = self._ready(monkeypatch)
    for _ in range(4):
      fake.queue(ConnectionError('always fails'))
    with pytest.raises(ConnectionError):
      tracker.step('reasoning', 'thinking', turn_index=1)
    step_requests = [r for r in fake.requests if r[1].endswith('/steps')]
    # initial + 3 retries (100ms / 500ms / 2s)
    assert len(step_requests) == 4

  def test_drops_connection_between_retry_attempts(self, monkeypatch):
    tracker, fake = self._ready(monkeypatch)
    fake.queue(ConnectionError('blip'))
    fake.queue((204, b''))
    tracker.step('reasoning', 'thinking', turn_index=1)
    # one start_trail connection open + at least one drop after the blip.
    assert fake.closes >= 1

  def test_retry_reuses_the_same_step_id(self, monkeypatch):
    tracker, fake = self._ready(monkeypatch)
    fake.queue(ConnectionError('blip'))
    fake.queue((204, b''))
    tracker.step('reasoning', 'thinking', turn_index=1)
    step_requests = [r for r in fake.requests if r[1].endswith('/steps')]
    assert len(step_requests) == 2
    ids = [_request_payload(r)['step_id'] for r in step_requests]
    # both attempts of the same POST carry one id — that is what lets the server
    # treat the retry as an idempotent no-op rather than a duplicate row.
    assert ids[0] == ids[1]

  def test_distinct_steps_get_distinct_ids(self, monkeypatch):
    tracker, fake = self._ready(monkeypatch)
    fake.queue((204, b''))
    fake.queue((204, b''))
    tracker.step('reasoning', 'a', turn_index=1)
    tracker.step('reasoning', 'b', turn_index=2)
    ids = [_request_payload(r)['step_id'] for r in fake.requests if r[1].endswith('/steps')]
    assert len(ids) == 2 and ids[0] != ids[1]

  def test_step_returns_the_posted_step_id(self, monkeypatch):
    tracker, fake = self._ready(monkeypatch)
    fake.queue((204, b''))
    returned = tracker.step('reasoning', 'a', turn_index=1)
    (request,) = [r for r in fake.requests if r[1].endswith('/steps')]
    assert returned == _request_payload(request)['step_id']

  def test_deterministic_4xx_is_not_retried(self, monkeypatch):
    tracker, fake = self._ready(monkeypatch)
    # 413 (body too large) is deterministic — retrying can't help.
    fake.queue((413, b'too large'))
    with pytest.raises(HTTPStatusError) as exception_info:
      tracker.step('llm_call', 'x', turn_index=1)
    assert exception_info.value.status == 413
    step_requests = [r for r in fake.requests if r[1].endswith('/steps')]
    assert len(step_requests) == 1

  def test_5xx_is_retried(self, monkeypatch):
    tracker, fake = self._ready(monkeypatch)
    for _ in range(4):
      fake.queue((503, b'unavailable'))
    with pytest.raises(HTTPStatusError) as exception_info:
      tracker.step('reasoning', 'thinking', turn_index=1)
    assert exception_info.value.status == 503
    step_requests = [r for r in fake.requests if r[1].endswith('/steps')]
    # initial + 3 retries (5xx is transient).
    assert len(step_requests) == 4

  def test_429_is_retried(self, monkeypatch):
    tracker, fake = self._ready(monkeypatch)
    fake.queue((429, b'slow down'))
    fake.queue((204, b''))
    tracker.step('reasoning', 'thinking', turn_index=1)
    step_requests = [r for r in fake.requests if r[1].endswith('/steps')]
    assert len(step_requests) == 2


class TestHTTPTrackerEndTrail:
  def _ready(self, monkeypatch) -> tuple[HTTPTracker, _FakeConnection]:
    fake = _install_fake_connection(monkeypatch)
    fake.queue((201, b'{"id": "T1"}'))
    tracker = HTTPTracker('https://trails.example', 'tok')
    tracker.start_trail(
      bro='b',
      llm_spec={},
      system_prompt='p',
      forked_from=None,
      interactive=False,
      surface='x',
    )
    return tracker, fake

  def test_posts_v1_end_with_reason(self, monkeypatch):
    tracker, fake = self._ready(monkeypatch)
    fake.queue((204, b''))
    tracker.end_trail('ok')
    method, path, body, _ = fake.requests[1]
    assert (method, path) == ('POST', '/v1/trails/T1/end')
    assert body is not None
    payload = json.loads(body)
    assert payload['reason'] == 'ok'
    # the end step carries a client-minted id too, so a retried end POST dedups.
    assert isinstance(payload['step_id'], str) and len(payload['step_id']) > 0
    # trail_id cleared so second end_trail is a no-op.
    assert tracker._trail_id is None

  def test_second_end_trail_is_noop(self, monkeypatch):
    tracker, fake = self._ready(monkeypatch)
    fake.queue((204, b''))
    tracker.end_trail('ok')
    # nothing queued — a second POST would assert; the no-op path proves it
    # never hit the wire.
    tracker.end_trail('raised')

  def test_logs_loudly_and_does_not_raise_on_persistent_failure(self, monkeypatch, caplog):
    tracker, fake = self._ready(monkeypatch)
    for _ in range(4):
      fake.queue(ConnectionError('still down'))
    with caplog.at_level(logging.WARNING):
      tracker.end_trail('ok')
    assert any('end_trail failed' in record.message for record in caplog.records)
    assert tracker._trail_id is None


class TestHTTPTrackerKeepalive:
  def _start(self, monkeypatch, interval: float) -> tuple[HTTPTracker, _FakeConnection]:
    fake = _install_fake_connection(monkeypatch)
    monkeypatch.setattr(llm.tracker, 'KEEPALIVE_INTERVAL_SECONDS', interval)
    fake.queue((201, b'{"id": "T1"}'))
    tracker = HTTPTracker('https://trails.example', 'tok')
    tracker.start_trail(
      bro='b', llm_spec={}, system_prompt='', forked_from=None, interactive=False, surface='x'
    )
    return tracker, fake

  def _keepalive_requests(self, fake: _FakeConnection) -> list:
    return [r for r in fake.requests if r[1] == '/v1/trails/T1/keepalive']

  def test_keepalive_posts_during_a_quiet_stretch(self, monkeypatch):
    tracker, fake = self._start(monkeypatch, interval=0.02)
    for _ in range(50):
      fake.queue((204, b''))
    deadline = time.monotonic() + 5.0
    while len(self._keepalive_requests(fake)) == 0 and time.monotonic() < deadline:
      threading.Event().wait(0.01)
    requests = self._keepalive_requests(fake)
    assert len(requests) > 0
    method, _, _, headers = requests[0]
    assert method == 'POST'
    assert headers['Authorization'] == 'Bearer tok'
    tracker.end_trail('ok')

  def test_no_keepalive_while_writes_flow(self, monkeypatch):
    tracker, fake = self._start(monkeypatch, interval=0.5)
    for _ in range(5):
      fake.queue((204, b''))
      tracker.step('assistant', 'x')
      threading.Event().wait(0.02)
    fake.queue((204, b''))
    tracker.end_trail('ok')
    thread = tracker._keepalive_thread
    assert thread is not None
    thread.join(2.0)
    assert not thread.is_alive()
    assert len(self._keepalive_requests(fake)) == 0

  def test_end_trail_stops_the_keepalive_thread(self, monkeypatch):
    tracker, fake = self._start(monkeypatch, interval=0.02)
    for _ in range(50):
      fake.queue((204, b''))
    thread = tracker._keepalive_thread
    assert thread is not None and thread.is_alive()
    tracker.end_trail('ok')
    thread.join(2.0)
    assert not thread.is_alive()

  def test_close_stops_the_keepalive_thread(self, monkeypatch):
    tracker, _ = self._start(monkeypatch, interval=3600.0)
    thread = tracker._keepalive_thread
    assert thread is not None and thread.is_alive()
    tracker.close()
    thread.join(2.0)
    assert not thread.is_alive()

  def test_keepalive_failure_is_swallowed_and_recording_continues(self, monkeypatch, caplog):
    tracker, fake = self._start(monkeypatch, interval=0.02)
    # the keepalive POST and its one retry both fail; recording must go on.
    fake.queue((500, b'oops'))
    fake.queue((500, b'oops'))
    deadline = time.monotonic() + 5.0
    with caplog.at_level(logging.WARNING):
      while len(self._keepalive_requests(fake)) < 2 and time.monotonic() < deadline:
        threading.Event().wait(0.01)
      # park the thread before sharing the response queue with the main
      # thread again; one straggler wake may still consume a queued response,
      # so the queue below carries a spare.
      monkeypatch.setattr(llm.tracker, 'KEEPALIVE_INTERVAL_SECONDS', 3600.0)
      threading.Event().wait(0.2)
    assert len(self._keepalive_requests(fake)) >= 2
    assert any('keepalive failed' in record.message for record in caplog.records)
    for _ in range(3):
      fake.queue((204, b''))
    tracker.step('assistant', 'x')
    assert any(r[1] == '/v1/trails/T1/steps' for r in fake.requests)
    tracker.end_trail('ok')
