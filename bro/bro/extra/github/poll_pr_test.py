#!/usr/bin/env python
import email.message
import json
import urllib.error
from typing import Any, Optional

import pytest

import poll_pr


def _http_error(code: int, headers: Optional[dict[str, str]] = None) -> urllib.error.HTTPError:
  hdrs = email.message.Message()
  for k, v in (headers or {}).items():
    hdrs[k] = v
  return urllib.error.HTTPError('https://api.github.com/x', code, f'HTTP {code}', hdrs, None)


class _FakeResp:
  def __init__(self, payload: Any):
    self._payload = payload

  def __enter__(self):
    return self

  def __exit__(self, *exc):
    return False

  def read(self) -> bytes:
    return json.dumps(self._payload).encode()


class _FakeUrlopen:
  """urlopen stand-in that replays `steps`: an exception step is raised, any
  other value is returned wrapped in a context-manager response. counts calls.
  """

  def __init__(self, steps: list[Any]):
    self._steps = steps
    self.call_count = 0

  def __call__(self, req, *args, **kwargs):
    step = self._steps[self.call_count]
    self.call_count += 1
    if isinstance(step, BaseException):
      raise step
    return _FakeResp(step)


def _user(login: str) -> dict[str, Any]:
  return {'user': {'login': login}}


def _issue_comment(id: int, login: str, body: str) -> dict[str, Any]:
  return {
    'id': id,
    'body': body,
    'html_url': f'https://github.com/x/y/issues/1#issuecomment-{id}',
    **_user(login),
  }


def _review(id: int, login: str, state: str, body: str = '') -> dict[str, Any]:
  return {
    'id': id,
    'state': state,
    'body': body,
    'html_url': f'https://github.com/x/y/pull/1#pullrequestreview-{id}',
    **_user(login),
  }


def _inline(id: int, review_id: int, login: str, body: str, line: int = 10) -> dict[str, Any]:
  return {
    'id': id,
    'pull_request_review_id': review_id,
    'body': body,
    'path': 'src/foo.py',
    'line': line,
    'html_url': f'https://github.com/x/y/pull/1#discussion_r{id}',
    **_user(login),
  }


class _FakeApi:
  """records inline-comment fetch arguments and serves canned per-endpoint data."""

  def __init__(
    self,
    issue_comments: Optional[list[dict[str, Any]]] = None,
    reviews: Optional[list[dict[str, Any]]] = None,
    review_inline: Optional[dict[int, list[dict[str, Any]]]] = None,
    all_inline: Optional[list[dict[str, Any]]] = None,
  ):
    self.issue_comments = issue_comments or []
    self.reviews = reviews or []
    self.review_inline = review_inline or {}
    self.all_inline = all_inline or []
    self.review_inline_calls: list[int] = []

  def fetch_issue_comments(self, owner, repo, pr, token):
    return self.issue_comments

  def fetch_reviews(self, owner, repo, pr, token):
    return self.reviews

  def fetch_review_inline_comments(self, owner, repo, pr, review_id, token):
    self.review_inline_calls.append(review_id)
    return self.review_inline.get(review_id, [])

  def fetch_review_comments(self, owner, repo, pr, token):
    return self.all_inline


def _install(monkeypatch, api: _FakeApi) -> None:
  monkeypatch.setattr(poll_pr, '_fetch_issue_comments', api.fetch_issue_comments)
  monkeypatch.setattr(poll_pr, '_fetch_reviews', api.fetch_reviews)
  monkeypatch.setattr(poll_pr, '_fetch_review_inline_comments', api.fetch_review_inline_comments)
  monkeypatch.setattr(poll_pr, '_fetch_review_comments', api.fetch_review_comments)


def _run(api: _FakeApi, **kwargs) -> list[dict[str, Any]]:
  return poll_pr.emit_cycle(
    owner='x',
    repo='y',
    pr=1,
    token='t',
    seen_comment_ids=kwargs.pop('seen_comment_ids', set()),
    seen_review_ids=kwargs.pop('seen_review_ids', set()),
    is_actionable=kwargs.pop('is_actionable', lambda login: True),
  )


class TestEmitCycle:
  def test_approved_review_bundles_inline_comments(self, monkeypatch):
    api = _FakeApi(
      reviews=[_review(100, 'alice', 'APPROVED')],
      review_inline={100: [_inline(200, 100, 'alice', 'nit: rename foo')]},
      # GitHub also returns the inline comment in the unfiltered list — bundling
      # must add it to `seen_comment_ids` so it doesn't double-emit standalone.
      all_inline=[_inline(200, 100, 'alice', 'nit: rename foo')],
    )
    _install(monkeypatch, api)
    events = _run(api)
    assert len(events) == 1
    e = events[0]
    assert e['event'] == 'review'
    assert e['state'] == 'APPROVED'
    assert e['id'] == 100
    assert len(e['comments']) == 1
    assert e['comments'][0]['id'] == 200
    assert e['comments'][0]['body'] == 'nit: rename foo'
    assert e['comments'][0]['line'] == 10

  def test_inline_comment_without_attached_review_fires_standalone(self, monkeypatch):
    # reply to an existing review thread, no new review wrapping it.
    api = _FakeApi(all_inline=[_inline(201, 99, 'alice', 'follow-up note')])
    _install(monkeypatch, api)
    events = _run(api)
    assert len(events) == 1
    assert events[0]['event'] == 'comment'
    assert events[0]['id'] == 201
    assert events[0]['body'] == 'follow-up note'

  def test_issue_comment_emitted_separately(self, monkeypatch):
    api = _FakeApi(issue_comments=[_issue_comment(50, 'alice', 'top-level comment')])
    _install(monkeypatch, api)
    events = _run(api)
    assert len(events) == 1
    assert events[0]['event'] == 'comment'
    assert events[0]['id'] == 50

  def test_seen_ids_prevent_re_emission(self, monkeypatch):
    api = _FakeApi(
      reviews=[_review(100, 'alice', 'APPROVED')],
      review_inline={100: [_inline(200, 100, 'alice', 'nit')]},
      all_inline=[_inline(200, 100, 'alice', 'nit')],
    )
    _install(monkeypatch, api)
    seen_c: set[int] = set()
    seen_r: set[int] = set()
    first = _run(api, seen_comment_ids=seen_c, seen_review_ids=seen_r)
    assert len(first) == 1
    second = _run(api, seen_comment_ids=seen_c, seen_review_ids=seen_r)
    assert second == []
    # the inline-comments-by-review endpoint is hit once per review (the first
    # cycle); the second cycle skips it because the review is already seen.
    assert api.review_inline_calls == [100]

  def test_inline_comment_arriving_after_review_fires_on_next_cycle(self, monkeypatch):
    # the race we want to survive: cycle 1 sees the review but the inline
    # comment isn't yet indexed; cycle 2 catches it via the standalone fetch.
    seen_c: set[int] = set()
    seen_r: set[int] = set()

    cycle1 = _FakeApi(
      reviews=[_review(100, 'alice', 'APPROVED')],
      review_inline={100: []},  # not yet indexed
      all_inline=[],
    )
    _install(monkeypatch, cycle1)
    first = _run(cycle1, seen_comment_ids=seen_c, seen_review_ids=seen_r)
    assert len(first) == 1
    assert first[0]['event'] == 'review'
    assert first[0]['comments'] == []

    cycle2 = _FakeApi(
      reviews=[_review(100, 'alice', 'APPROVED')],  # already seen
      review_inline={100: [_inline(200, 100, 'alice', 'late comment')]},
      all_inline=[_inline(200, 100, 'alice', 'late comment')],
    )
    _install(monkeypatch, cycle2)
    second = _run(cycle2, seen_comment_ids=seen_c, seen_review_ids=seen_r)
    assert len(second) == 1
    assert second[0]['event'] == 'comment'
    assert second[0]['body'] == 'late comment'

  def test_non_actionable_review_still_marks_inline_comments_seen(self, monkeypatch):
    # a bot or self-authored review should not emit, but its inline comments
    # must still be marked seen so they don't fire as standalone events.
    api = _FakeApi(
      reviews=[_review(100, 'self-bot', 'APPROVED')],
      review_inline={100: [_inline(200, 100, 'self-bot', 'self comment')]},
      all_inline=[_inline(200, 100, 'self-bot', 'self comment')],
    )
    _install(monkeypatch, api)
    events = _run(api, is_actionable=lambda login: login != 'self-bot')
    assert events == []
    assert api.review_inline_calls == [100]


class TestGhGetRetry:
  def test_retries_transient_401_then_returns(self, monkeypatch):
    fake = _FakeUrlopen([_http_error(401), {'ok': True}])
    monkeypatch.setattr(poll_pr.urllib.request, 'urlopen', fake)
    monkeypatch.setattr(poll_pr.time, 'sleep', lambda _: None)
    assert poll_pr._gh_get('https://api.github.com/x', 't') == {'ok': True}
    assert fake.call_count == 2

  def test_does_not_retry_404(self, monkeypatch):
    fake = _FakeUrlopen([_http_error(404), {'ok': True}])
    monkeypatch.setattr(poll_pr.urllib.request, 'urlopen', fake)
    monkeypatch.setattr(poll_pr.time, 'sleep', lambda _: None)
    with pytest.raises(urllib.error.HTTPError) as exc:
      poll_pr._gh_get('https://api.github.com/x', 't')
    assert exc.value.code == 404
    assert fake.call_count == 1

  def test_retries_network_error(self, monkeypatch):
    fake = _FakeUrlopen([urllib.error.URLError('connection reset'), {'ok': True}])
    monkeypatch.setattr(poll_pr.urllib.request, 'urlopen', fake)
    monkeypatch.setattr(poll_pr.time, 'sleep', lambda _: None)
    assert poll_pr._gh_get('https://api.github.com/x', 't') == {'ok': True}
    assert fake.call_count == 2

  def test_gives_up_after_max_attempts(self, monkeypatch):
    fake = _FakeUrlopen([_http_error(503)] * poll_pr._MAX_ATTEMPTS)
    monkeypatch.setattr(poll_pr.urllib.request, 'urlopen', fake)
    monkeypatch.setattr(poll_pr.time, 'sleep', lambda _: None)
    with pytest.raises(urllib.error.HTTPError):
      poll_pr._gh_get('https://api.github.com/x', 't')
    assert fake.call_count == poll_pr._MAX_ATTEMPTS


class TestRetryDelay:
  def test_honors_retry_after_seconds(self):
    err = _http_error(429, {'Retry-After': '7'})
    assert poll_pr._retry_delay(err, 0) == 7.0

  def test_honors_rate_limit_reset_when_exhausted(self, monkeypatch):
    monkeypatch.setattr(poll_pr.time, 'time', lambda: 1000.0)
    err = _http_error(403, {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '1012'})
    assert poll_pr._retry_delay(err, 0) == 12.0

  def test_falls_back_to_exponential_backoff(self):
    err = _http_error(503)
    assert poll_pr._retry_delay(err, 0) == poll_pr._BASE_BACKOFF
    assert poll_pr._retry_delay(err, 2) == poll_pr._BASE_BACKOFF * 4

  def test_caps_server_hint_at_max_backoff(self):
    err = _http_error(429, {'Retry-After': '9999'})
    assert poll_pr._retry_delay(err, 0) == poll_pr._MAX_BACKOFF


class TestPollLoopResilience:
  def _baseline(self, monkeypatch):
    monkeypatch.setattr(poll_pr, '_owner_login', lambda *a: 'owner')
    monkeypatch.setattr(poll_pr, '_fetch_issue_comments', lambda *a: [])
    monkeypatch.setattr(poll_pr, '_fetch_review_comments', lambda *a: [])
    monkeypatch.setattr(poll_pr, '_fetch_reviews', lambda *a: [])
    monkeypatch.setattr(poll_pr.time, 'sleep', lambda _: None)

  def test_transient_cycle_error_is_swallowed(self, monkeypatch):
    self._baseline(monkeypatch)
    pr_steps: list[Any] = [_http_error(503), {'merged': True}]
    calls: list[int] = []

    def fake_fetch_pr(*a):
      step = pr_steps[len(calls)]
      calls.append(1)
      if isinstance(step, BaseException):
        raise step
      return step

    monkeypatch.setattr(poll_pr, '_fetch_pr', fake_fetch_pr)
    assert poll_pr.poll_pr('o', 'r', 1, 't', interval=0, self_login=None) == 0
    assert len(calls) == 2

  def test_fatal_cycle_error_propagates(self, monkeypatch):
    self._baseline(monkeypatch)

    def fake_fetch_pr(*a):
      raise _http_error(404)

    monkeypatch.setattr(poll_pr, '_fetch_pr', fake_fetch_pr)
    with pytest.raises(urllib.error.HTTPError) as exc:
      poll_pr.poll_pr('o', 'r', 1, 't', interval=0, self_login=None)
    assert exc.value.code == 404
