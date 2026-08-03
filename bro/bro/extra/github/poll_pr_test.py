#!/usr/bin/env python
import email.message
import http.client
import itertools
import json
import urllib.error
from typing import Any, Optional

import pytest

from bro.extra.github import poll_pr


def _http_error(code: int) -> urllib.error.HTTPError:
  return urllib.error.HTTPError(
    'https://api.github.com/x', code, f'HTTP {code}', email.message.Message(), None
  )


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


class _FakeAPI:
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


def _install(monkeypatch, api: _FakeAPI) -> None:
  monkeypatch.setattr(poll_pr, '_fetch_issue_comments', api.fetch_issue_comments)
  monkeypatch.setattr(poll_pr, '_fetch_reviews', api.fetch_reviews)
  monkeypatch.setattr(poll_pr, '_fetch_review_inline_comments', api.fetch_review_inline_comments)
  monkeypatch.setattr(poll_pr, '_fetch_review_comments', api.fetch_review_comments)


def _run(api: _FakeAPI, **kwargs) -> list[dict[str, Any]]:
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
    api = _FakeAPI(
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
    api = _FakeAPI(all_inline=[_inline(201, 99, 'alice', 'follow-up note')])
    _install(monkeypatch, api)
    events = _run(api)
    assert len(events) == 1
    assert events[0]['event'] == 'comment'
    assert events[0]['id'] == 201
    assert events[0]['body'] == 'follow-up note'

  def test_issue_comment_emitted_separately(self, monkeypatch):
    api = _FakeAPI(issue_comments=[_issue_comment(50, 'alice', 'top-level comment')])
    _install(monkeypatch, api)
    events = _run(api)
    assert len(events) == 1
    assert events[0]['event'] == 'comment'
    assert events[0]['id'] == 50

  def test_seen_ids_prevent_re_emission(self, monkeypatch):
    api = _FakeAPI(
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

    cycle1 = _FakeAPI(
      reviews=[_review(100, 'alice', 'APPROVED')],
      review_inline={100: []},  # not yet indexed
      all_inline=[],
    )
    _install(monkeypatch, cycle1)
    first = _run(cycle1, seen_comment_ids=seen_c, seen_review_ids=seen_r)
    assert len(first) == 1
    assert first[0]['event'] == 'review'
    assert first[0]['comments'] == []

    cycle2 = _FakeAPI(
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
    api = _FakeAPI(
      reviews=[_review(100, 'self-bot', 'APPROVED')],
      review_inline={100: [_inline(200, 100, 'self-bot', 'self comment')]},
      all_inline=[_inline(200, 100, 'self-bot', 'self comment')],
    )
    _install(monkeypatch, api)
    events = _run(api, is_actionable=lambda login: login != 'self-bot')
    assert events == []
    assert api.review_inline_calls == [100]


class TestPollLoopResilience:
  def _baseline(self, monkeypatch):
    monkeypatch.setattr(poll_pr, '_owner_login', lambda *a: 'owner')
    monkeypatch.setattr(poll_pr, '_fetch_issue_comments', lambda *a: [])
    monkeypatch.setattr(poll_pr, '_fetch_review_comments', lambda *a: [])
    monkeypatch.setattr(poll_pr, '_fetch_reviews', lambda *a: [])
    monkeypatch.setattr(poll_pr.time, 'sleep', lambda _: None)

  def test_transient_cycle_error_is_swallowed(self, monkeypatch, capsys):
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
    assert poll_pr.poll_pr('o', 'r', 1, lambda: 't', interval=0, self_login=None) == 0
    assert len(calls) == 2
    assert 'Logging error' not in capsys.readouterr().err

  def test_fatal_cycle_error_propagates(self, monkeypatch):
    self._baseline(monkeypatch)

    def fake_fetch_pr(*a):
      raise _http_error(404)

    monkeypatch.setattr(poll_pr, '_fetch_pr', fake_fetch_pr)
    with pytest.raises(urllib.error.HTTPError) as exception:
      poll_pr.poll_pr('o', 'r', 1, lambda: 't', interval=0, self_login=None)
    assert exception.value.code == 404

  def test_remote_disconnected_cycle_error_is_swallowed(self, monkeypatch):
    self._baseline(monkeypatch)
    pr_steps: list[Any] = [
      http.client.RemoteDisconnected('server closed connection'),
      {'merged': True},
    ]
    calls: list[int] = []

    def fake_fetch_pr(*a):
      step = pr_steps[len(calls)]
      calls.append(1)
      if isinstance(step, BaseException):
        raise step
      return step

    monkeypatch.setattr(poll_pr, '_fetch_pr', fake_fetch_pr)
    assert poll_pr.poll_pr('o', 'r', 1, lambda: 't', interval=0, self_login=None) == 0
    assert len(calls) == 2


class TestConflictDetection:
  def _baseline(self, monkeypatch):
    monkeypatch.setattr(poll_pr, '_owner_login', lambda *a: 'owner')
    monkeypatch.setattr(poll_pr, '_fetch_issue_comments', lambda *a: [])
    monkeypatch.setattr(poll_pr, '_fetch_review_comments', lambda *a: [])
    monkeypatch.setattr(poll_pr, '_fetch_reviews', lambda *a: [])
    monkeypatch.setattr(poll_pr.time, 'sleep', lambda _: None)

  def test_conflict_fires_once_and_rearms_after_clean(self, monkeypatch, capsys):
    self._baseline(monkeypatch)
    pr_steps: list[dict[str, Any]] = [
      {'state': 'open', 'mergeable': False, **_user('alice')},  # fires
      {'state': 'open', 'mergeable': False, **_user('alice')},  # already fired
      {'state': 'open', 'mergeable': None, **_user('alice')},  # still computing: no change
      {'state': 'open', 'mergeable': True, **_user('alice')},  # re-arms
      {'state': 'open', 'mergeable': False, **_user('alice')},  # fires again
      {'merged': True},
    ]
    pr_calls: list[int] = []

    def fake_fetch_pr(*a):
      pr_calls.append(1)
      return pr_steps[len(pr_calls) - 1]

    monkeypatch.setattr(poll_pr, '_fetch_pr', fake_fetch_pr)
    assert poll_pr.poll_pr('o', 'r', 1, lambda: 't', interval=0, self_login='x') == 0
    events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert events == [
      {'event': 'conflicts', 'pr': 1},
      {'event': 'conflicts', 'pr': 1},
      {'event': 'merged', 'pr': 1},
    ]

  def test_mergeable_pr_emits_no_conflict_event(self, monkeypatch, capsys):
    self._baseline(monkeypatch)
    pr_steps: list[dict[str, Any]] = [
      {'state': 'open', 'mergeable': True, **_user('alice')},
      {'merged': True},
    ]
    pr_calls: list[int] = []

    def fake_fetch_pr(*a):
      pr_calls.append(1)
      return pr_steps[len(pr_calls) - 1]

    monkeypatch.setattr(poll_pr, '_fetch_pr', fake_fetch_pr)
    assert poll_pr.poll_pr('o', 'r', 1, lambda: 't', interval=0, self_login='x') == 0
    events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert events == [{'event': 'merged', 'pr': 1}]


class TestTokenAndSelf:
  def _baseline(self, monkeypatch):
    monkeypatch.setattr(poll_pr, '_owner_login', lambda *a: 'alice')
    monkeypatch.setattr(poll_pr, '_fetch_issue_comments', lambda *a: [])
    monkeypatch.setattr(poll_pr, '_fetch_review_comments', lambda *a: [])
    monkeypatch.setattr(poll_pr, '_fetch_reviews', lambda *a: [])
    monkeypatch.setattr(poll_pr.time, 'sleep', lambda _: None)

  def test_token_provider_is_consulted_every_cycle(self, monkeypatch):
    self._baseline(monkeypatch)
    tokens_seen: list[str] = []
    pr_steps: list[dict[str, Any]] = [{'state': 'open', **_user('alice')}, {'merged': True}]

    def fake_fetch_pr(owner, repo, pr, token):
      tokens_seen.append(token)
      return pr_steps[len(tokens_seen) - 1]

    monkeypatch.setattr(poll_pr, '_fetch_pr', fake_fetch_pr)
    counter = itertools.count(1)
    provider = lambda: f't{next(counter)}'  # noqa: E731
    assert poll_pr.poll_pr('o', 'r', 1, provider, interval=0, self_login='x') == 0
    # each cycle re-reads the provider: two cycles, two distinct tokens
    assert len(tokens_seen) == 2
    assert len(set(tokens_seen)) == 2

  def test_self_defaults_to_pr_author(self, monkeypatch, capsys):
    self._baseline(monkeypatch)
    author_comment = _issue_comment(50, 'alice', 'my own note')
    issue_calls: list[int] = []

    def fake_issue_comments(*a):
      issue_calls.append(1)
      # absent at the startup baseline scan, appears on the first poll cycle
      return [] if len(issue_calls) == 1 else [author_comment]

    monkeypatch.setattr(poll_pr, '_fetch_issue_comments', fake_issue_comments)
    pr_steps: list[dict[str, Any]] = [{'state': 'open', **_user('alice')}, {'merged': True}]
    pr_calls: list[int] = []

    def fake_fetch_pr(*a):
      pr_calls.append(1)
      return pr_steps[len(pr_calls) - 1]

    monkeypatch.setattr(poll_pr, '_fetch_pr', fake_fetch_pr)
    assert poll_pr.poll_pr('o', 'r', 1, lambda: 't', interval=0, self_login=None) == 0
    out = capsys.readouterr().out
    # the repo owner authored the PR; with self defaulted to the PR author their
    # own comment is filtered rather than emitted
    assert '"comment"' not in out
    assert json.loads(out.strip().splitlines()[-1]) == {'event': 'merged', 'pr': 1}


class TestMain:
  def _capture_poll(self, monkeypatch) -> dict:
    captured = {}

    def fake_poll(owner, repo, pr, token, interval, self_login):
      captured['token'] = token
      return 0

    monkeypatch.setattr(poll_pr, 'poll_pr', fake_poll)
    monkeypatch.setattr(poll_pr.credentials, 'get', lambda name: f'resolved:{name}')
    return captured

  def test_credential_defaults_to_github(self, monkeypatch):
    captured = self._capture_poll(monkeypatch)
    assert poll_pr.main(['poll-pr', 'x/y', '1']) == 0
    assert captured['token']() == 'resolved:github'

  def test_credential_flag_resolves_per_read(self, monkeypatch):
    captured = self._capture_poll(monkeypatch)
    assert poll_pr.main(['poll-pr', 'x/y', '1', '--credential', 'other']) == 0
    assert captured['token']() == 'resolved:other'
