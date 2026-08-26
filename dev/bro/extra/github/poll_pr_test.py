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
  events: list[dict[str, Any]] = []
  poll_pr.emit_cycle(
    owner='x',
    repo='y',
    pr=1,
    token='t',
    seen_comment_ids=kwargs.pop('seen_comment_ids', set()),
    seen_review_ids=kwargs.pop('seen_review_ids', set()),
    is_actionable=kwargs.pop('is_actionable', lambda login: True),
    emit=events.append,
  )
  return events


def _poll(**overrides) -> int:
  arguments: dict[str, Any] = {
    'owner': 'o',
    'repo': 'r',
    'pr': 1,
    'token': lambda: 't',
    'interval': 0,
    'self_login': None,
    'failure_grace': 300,
  }
  return poll_pr.poll_pr(**{**arguments, **overrides})


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

  def test_review_whose_inline_fetch_fails_is_emitted_on_the_next_cycle(self, monkeypatch):
    seen_c: set[int] = set()
    seen_r: set[int] = set()
    api = _FakeAPI(
      reviews=[_review(100, 'alice', 'APPROVED')],
      review_inline={100: [_inline(200, 100, 'alice', 'nit')]},
      all_inline=[_inline(200, 100, 'alice', 'nit')],
    )
    _install(monkeypatch, api)

    def failing_inline(*a):
      raise _http_error(403)

    monkeypatch.setattr(poll_pr, '_fetch_review_inline_comments', failing_inline)
    with pytest.raises(urllib.error.HTTPError):
      _run(api, seen_comment_ids=seen_c, seen_review_ids=seen_r)
    assert seen_r == set()

    _install(monkeypatch, api)
    events = _run(api, seen_comment_ids=seen_c, seen_review_ids=seen_r)
    assert [e['event'] for e in events] == ['review']
    assert events[0]['comments'][0]['id'] == 200

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


class _Stepper:
  """a fetch fake serving `steps` in order, raising the ones that are exceptions."""

  def __init__(self, steps: list[Any]):
    self.steps = steps
    self.calls = 0

  def __call__(self, *args):
    step = self.steps[self.calls]
    self.calls += 1
    if isinstance(step, BaseException):
      raise step
    return step


class TestPollLoopResilience:
  def _baseline(self, monkeypatch):
    monkeypatch.setattr(poll_pr, '_owner_login', lambda *a: 'owner')
    monkeypatch.setattr(poll_pr, '_fetch_issue_comments', lambda *a: [])
    monkeypatch.setattr(poll_pr, '_fetch_review_comments', lambda *a: [])
    monkeypatch.setattr(poll_pr, '_fetch_reviews', lambda *a: [])
    monkeypatch.setattr(poll_pr.time, 'sleep', lambda _: None)

  def test_transient_cycle_error_is_swallowed(self, monkeypatch, capsys):
    self._baseline(monkeypatch)
    fetch = _Stepper([_http_error(503), {'merged': True}])
    monkeypatch.setattr(poll_pr, '_fetch_pr', fetch)
    assert _poll() == 0
    assert fetch.calls == 2
    assert 'Logging error' not in capsys.readouterr().err

  def test_remote_disconnected_cycle_error_is_swallowed(self, monkeypatch):
    self._baseline(monkeypatch)
    fetch = _Stepper([http.client.RemoteDisconnected('server closed connection'), {'merged': True}])
    monkeypatch.setattr(poll_pr, '_fetch_pr', fetch)
    assert _poll() == 0
    assert fetch.calls == 2


class TestSourceFailures:
  def _baseline(self, monkeypatch):
    monkeypatch.setattr(poll_pr, '_owner_login', lambda *a: 'owner')
    monkeypatch.setattr(poll_pr, '_fetch_issue_comments', lambda *a: [])
    monkeypatch.setattr(poll_pr, '_fetch_review_comments', lambda *a: [])
    monkeypatch.setattr(poll_pr, '_fetch_reviews', lambda *a: [])
    monkeypatch.setattr(poll_pr, '_fetch_review_inline_comments', lambda *a: [])
    monkeypatch.setattr(poll_pr.time, 'sleep', lambda _: None)

  def _clock(self, monkeypatch, step: float) -> None:
    ticks = itertools.count(0, step)
    monkeypatch.setattr(poll_pr.time, 'monotonic', lambda: next(ticks))

  def test_a_failing_source_does_not_suppress_the_others(self, monkeypatch, capsys):
    # a token without `checks: read` 403s on check-runs while reviews and
    # comments stay readable
    self._baseline(monkeypatch)
    open_pr = {'head': {'sha': 'deadbeef'}, 'state': 'open', **_user('alice')}
    monkeypatch.setattr(poll_pr, '_fetch_pr', _Stepper([open_pr, {'merged': True}]))
    monkeypatch.setattr(poll_pr, '_fetch_check_runs', _Stepper([_http_error(403)]))
    # empty at the startup baseline scan, so the review counts as new
    monkeypatch.setattr(
      poll_pr, '_fetch_reviews', _Stepper([[], [_review(100, 'owner', 'APPROVED')]])
    )
    assert _poll(self_login='x') == 0
    events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert [e['event'] for e in events] == ['review', 'merged']

  def test_a_source_failing_past_the_grace_window_ends_the_watch(self, monkeypatch, capsys):
    self._baseline(monkeypatch)
    self._clock(monkeypatch, step=10)
    open_pr = {'head': {'sha': 'deadbeef'}, 'state': 'open', **_user('alice')}
    monkeypatch.setattr(poll_pr, '_fetch_pr', lambda *a: open_pr)

    def failing_checks(*a):
      raise _http_error(403)

    monkeypatch.setattr(poll_pr, '_fetch_check_runs', failing_checks)
    assert _poll(self_login='x', failure_grace=30) == 2
    events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert events == [
      {
        'event': 'watch_failed',
        'pr': 1,
        'source': 'checks',
        'reason': 'HTTP 403',
        'failing_for': 30,
      }
    ]

  def test_a_source_recovering_inside_the_window_rearms_it(self, monkeypatch, capsys):
    self._baseline(monkeypatch)
    self._clock(monkeypatch, step=20)
    open_pr = {'head': {'sha': 'deadbeef'}, 'state': 'open', **_user('alice')}
    monkeypatch.setattr(
      poll_pr, '_fetch_pr', _Stepper([open_pr, open_pr, open_pr, open_pr, {'merged': True}])
    )
    # each failure is 20s past the previous one — only an unbroken run of them
    # crosses the 30s window
    monkeypatch.setattr(
      poll_pr, '_fetch_check_runs', _Stepper([_http_error(403), [], _http_error(403), []])
    )
    assert _poll(self_login='x', failure_grace=30) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert events == [{'event': 'merged', 'pr': 1}]

  def test_a_failed_baseline_ends_the_watch_before_the_first_cycle(self, monkeypatch, capsys):
    self._baseline(monkeypatch)

    def failing_reviews(*a):
      raise _http_error(403)

    monkeypatch.setattr(poll_pr, '_fetch_reviews', failing_reviews)
    monkeypatch.setattr(poll_pr, '_fetch_pr', lambda *a: {'merged': True})
    assert _poll() == 2
    events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert [(e['event'], e['source']) for e in events] == [('watch_failed', 'baseline')]

  def test_a_not_found_ends_the_watch_once_the_window_closes(self, monkeypatch, capsys):
    self._baseline(monkeypatch)
    self._clock(monkeypatch, step=10)

    def missing_pr(*a):
      raise _http_error(404)

    monkeypatch.setattr(poll_pr, '_fetch_pr', missing_pr)
    assert _poll(failure_grace=30) == 2
    events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert events == [
      {
        'event': 'watch_failed',
        'pr': 1,
        'source': 'pull-request',
        'reason': 'HTTP 404',
        'failing_for': 30,
      }
    ]

  def test_a_not_found_blip_clears_on_the_next_cycle(self, monkeypatch, capsys):
    self._baseline(monkeypatch)
    open_pr = {'head': {'sha': 'deadbeef'}, 'state': 'open', **_user('alice')}
    monkeypatch.setattr(
      poll_pr, '_fetch_pr', _Stepper([open_pr, _http_error(404), {'merged': True}])
    )
    monkeypatch.setattr(poll_pr, '_fetch_check_runs', lambda *a: [])
    assert _poll(self_login='x', failure_grace=30) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert events == [{'event': 'merged', 'pr': 1}]


def _check_run(name: str, status: str, conclusion: Optional[str] = None) -> dict[str, Any]:
  return {
    'name': name,
    'status': status,
    'conclusion': conclusion,
    'html_url': f'https://github.com/x/y/runs/{name}',
  }


class TestCheckTracker:
  def test_fires_once_per_red_episode_and_rearms(self):
    tracker = poll_pr.CheckTracker()
    running = [_check_run('tests', 'in_progress')]
    failed = [_check_run('tests', 'completed', 'failure')]
    passed = [_check_run('tests', 'completed', 'success')]

    assert tracker.update(running) == []
    fired = tracker.update(failed)
    assert [run['name'] for run in fired] == ['tests']
    assert tracker.update(failed) == []  # still red: no repeat
    assert tracker.update(passed) == []  # re-run went green: re-arms
    assert len(tracker.update(failed)) == 1

  def test_neutral_and_skipped_are_not_failures(self):
    tracker = poll_pr.CheckTracker()
    runs = [_check_run('a', 'completed', 'neutral'), _check_run('b', 'completed', 'skipped')]
    assert tracker.update(runs) == []

  def test_no_checks_is_not_a_failure(self):
    assert poll_pr.CheckTracker().update([]) == []


class TestCheckEvents:
  def _baseline(self, monkeypatch):
    monkeypatch.setattr(poll_pr, '_owner_login', lambda *a: 'owner')
    monkeypatch.setattr(poll_pr, '_fetch_issue_comments', lambda *a: [])
    monkeypatch.setattr(poll_pr, '_fetch_review_comments', lambda *a: [])
    monkeypatch.setattr(poll_pr, '_fetch_reviews', lambda *a: [])
    monkeypatch.setattr(poll_pr.time, 'sleep', lambda _: None)

  def test_failing_check_emits_one_event_with_the_run_url(self, monkeypatch, capsys):
    self._baseline(monkeypatch)
    head = {'head': {'sha': 'deadbeef'}, 'state': 'open', **_user('alice')}
    pr_steps: list[dict[str, Any]] = [head, head, {'merged': True}]
    runs = [
      [_check_run('tests', 'in_progress')],
      [_check_run('tests', 'completed', 'failure')],
      [],
    ]
    calls: list[int] = []

    def fake_fetch_pr(*a):
      step = pr_steps[len(calls)]
      calls.append(1)
      return step

    monkeypatch.setattr(poll_pr, '_fetch_pr', fake_fetch_pr)
    monkeypatch.setattr(poll_pr, '_fetch_check_runs', lambda *a: runs[len(calls) - 1])
    assert _poll() == 0

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    checks = [e for e in events if e['event'] == 'checks']
    assert len(checks) == 1
    assert checks[0]['failing'] == [
      {'name': 'tests', 'conclusion': 'failure', 'url': 'https://github.com/x/y/runs/tests'}
    ]

  def test_a_pr_without_a_head_sha_skips_the_check_fetch(self, monkeypatch):
    self._baseline(monkeypatch)
    monkeypatch.setattr(poll_pr, '_fetch_pr', lambda *a: {'merged': True})
    fetched: list[int] = []
    monkeypatch.setattr(poll_pr, '_fetch_check_runs', lambda *a: fetched.append(1) or [])
    assert _poll() == 0
    assert fetched == []


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
    assert _poll(self_login='x') == 0
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
    assert _poll(self_login='x') == 0
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
    assert _poll(token=provider, self_login='x') == 0
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
    assert _poll() == 0
    out = capsys.readouterr().out
    # the repo owner authored the PR; with self defaulted to the PR author their
    # own comment is filtered rather than emitted
    assert '"comment"' not in out
    assert json.loads(out.strip().splitlines()[-1]) == {'event': 'merged', 'pr': 1}


class TestMain:
  def _capture_poll(self, monkeypatch) -> dict:
    captured = {}

    def fake_poll(owner, repo, pr, token, interval, self_login, failure_grace):
      captured['token'] = token
      captured['failure_grace'] = failure_grace
      return 0

    monkeypatch.setattr(poll_pr, 'poll_pr', fake_poll)

    class _Store:
      def get_instance(self, name: str) -> str:
        return f'resolved:{name}'

    monkeypatch.setattr(poll_pr.credentials, 'default_store', lambda: _Store())
    return captured

  def test_credential_flag_accepts_a_storage_name(self, monkeypatch):
    captured = self._capture_poll(monkeypatch)
    assert poll_pr.main(['poll-pr', 'x/y', '1', '--credential', 'github+other']) == 0
    assert captured['token']() == 'resolved:github+other'

  def test_failure_grace_flag_reaches_the_watch(self, monkeypatch):
    captured = self._capture_poll(monkeypatch)
    assert poll_pr.main(['poll-pr', 'x/y', '1', '--failure-grace', '60']) == 0
    assert captured['failure_grace'] == 60
