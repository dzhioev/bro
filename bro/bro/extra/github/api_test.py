#!/usr/bin/env python
import email.message
import http.client
import json
import urllib.error
from typing import Any, Optional

import pytest

from github import api


def _http_error(code: int, headers: Optional[dict[str, str]] = None) -> urllib.error.HTTPError:
  message = email.message.Message()
  if headers is not None:
    for key, value in headers.items():
      message[key] = value
  return urllib.error.HTTPError('https://api.github.com/x', code, f'HTTP {code}', message, None)


class _FakeResponse:
  def __init__(self, payload: Any):
    self._payload = payload

  def __enter__(self):
    return self

  def __exit__(self, *exception):
    return False

  def read(self) -> bytes:
    if isinstance(self._payload, bytes):
      return self._payload
    return json.dumps(self._payload).encode()


class _FakeUrlopen:
  """urlopen stand-in that replays `steps`: an exception step is raised, any
  other value is returned wrapped in a context-manager response. records the
  requests it was called with.
  """

  def __init__(self, steps: list[Any]):
    self._steps = steps
    self.requests: list[Any] = []

  @property
  def call_count(self) -> int:
    return len(self.requests)

  def __call__(self, request, *args, **kwargs):
    self.requests.append(request)
    step = self._steps[len(self.requests) - 1]
    if isinstance(step, BaseException):
      raise step
    return _FakeResponse(step)


def _install(monkeypatch, fake: _FakeUrlopen) -> None:
  monkeypatch.setattr(api.urllib.request, 'urlopen', fake)
  monkeypatch.setattr(api.time, 'sleep', lambda _: None)


class TestVerbs:
  def test_get_request_shape(self, monkeypatch):
    fake = _FakeUrlopen([{'ok': True}])
    _install(monkeypatch, fake)
    assert api.get('https://api.github.com/x', 't') == {'ok': True}
    request = fake.requests[0]
    assert request.get_method() == 'GET'
    assert request.data is None
    assert request.get_header('Authorization') == 'Bearer t'
    assert request.get_header('Accept') == 'application/vnd.github+json'
    assert request.get_header('X-github-api-version') == '2022-11-28'

  def test_post_sends_json_body(self, monkeypatch):
    fake = _FakeUrlopen([{'id': 1}])
    _install(monkeypatch, fake)
    assert api.post('https://api.github.com/x', 't', {'title': 'hi'}) == {'id': 1}
    request = fake.requests[0]
    assert request.get_method() == 'POST'
    assert json.loads(request.data) == {'title': 'hi'}
    assert request.get_header('Content-type') == 'application/json'

  def test_patch_sends_json_body(self, monkeypatch):
    fake = _FakeUrlopen([{'id': 1}])
    _install(monkeypatch, fake)
    assert api.patch('https://api.github.com/x', 't', {'state': 'closed'}) == {'id': 1}
    request = fake.requests[0]
    assert request.get_method() == 'PATCH'
    assert json.loads(request.data) == {'state': 'closed'}

  def test_delete_empty_response_returns_none(self, monkeypatch):
    fake = _FakeUrlopen([b''])
    _install(monkeypatch, fake)
    assert api.delete('https://api.github.com/x', 't') is None
    assert fake.requests[0].get_method() == 'DELETE'


class TestRetry:
  def test_retries_transient_401_then_returns(self, monkeypatch):
    fake = _FakeUrlopen([_http_error(401), {'ok': True}])
    _install(monkeypatch, fake)
    assert api.get('https://api.github.com/x', 't') == {'ok': True}
    assert fake.call_count == 2

  def test_does_not_retry_404(self, monkeypatch):
    fake = _FakeUrlopen([_http_error(404), {'ok': True}])
    _install(monkeypatch, fake)
    with pytest.raises(urllib.error.HTTPError) as exception:
      api.get('https://api.github.com/x', 't')
    assert exception.value.code == 404
    assert fake.call_count == 1

  def test_retries_network_error(self, monkeypatch):
    fake = _FakeUrlopen([urllib.error.URLError('connection reset'), {'ok': True}])
    _install(monkeypatch, fake)
    assert api.get('https://api.github.com/x', 't') == {'ok': True}
    assert fake.call_count == 2

  def test_retries_remote_disconnected(self, monkeypatch):
    fake = _FakeUrlopen([http.client.RemoteDisconnected('server closed connection'), {'ok': True}])
    _install(monkeypatch, fake)
    assert api.get('https://api.github.com/x', 't') == {'ok': True}
    assert fake.call_count == 2

  def test_retries_mutating_verb(self, monkeypatch):
    fake = _FakeUrlopen([_http_error(503), {'id': 1}])
    _install(monkeypatch, fake)
    assert api.post('https://api.github.com/x', 't', {'title': 'hi'}) == {'id': 1}
    assert fake.call_count == 2

  def test_gives_up_after_max_attempts(self, monkeypatch):
    fake = _FakeUrlopen([_http_error(503)] * api._MAX_ATTEMPTS)
    _install(monkeypatch, fake)
    with pytest.raises(urllib.error.HTTPError):
      api.get('https://api.github.com/x', 't')
    assert fake.call_count == api._MAX_ATTEMPTS


class TestRetryDelay:
  def test_honors_retry_after_seconds(self):
    error = _http_error(429, {'Retry-After': '7'})
    assert api._retry_delay(error, 0) == 7.0

  def test_honors_rate_limit_reset_when_exhausted(self, monkeypatch):
    monkeypatch.setattr(api.time, 'time', lambda: 1000.0)
    error = _http_error(403, {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '1012'})
    assert api._retry_delay(error, 0) == 12.0

  def test_falls_back_to_exponential_backoff(self):
    error = _http_error(503)
    assert api._retry_delay(error, 0) == api._BASE_BACKOFF
    assert api._retry_delay(error, 2) == api._BASE_BACKOFF * 4

  def test_caps_server_hint_at_max_backoff(self):
    error = _http_error(429, {'Retry-After': '9999'})
    assert api._retry_delay(error, 0) == api._MAX_BACKOFF
