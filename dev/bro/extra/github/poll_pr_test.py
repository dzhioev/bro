#!/usr/bin/env python
import email.message
import http.client
import io
import itertools
import json
import re
import subprocess
import urllib.error
from pathlib import Path
from typing import Any, ClassVar, Optional

import pytest

from bro import watches
from bro.base import credentials, retry
from bro.extra.github import poll_pr


def _http_error(code: int) -> urllib.error.HTTPError:
  return urllib.error.HTTPError(
    'https://api.github.com/x', code, f'HTTP {code}', email.message.Message(), None
  )


def _user(login: str) -> dict[str, Any]:
  return {'user': {'login': login}}


def _open_pr(login: str = 'alice', sha: str = 'deadbeef', **extra: Any) -> dict[str, Any]:
  return {
    'state': 'open',
    'head': {'sha': sha},
    'mergeable_state': 'blocked',
    **_user(login),
    **extra,
  }


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
    review_decision: Optional[str] = 'APPROVED',
  ):
    self.issue_comments = issue_comments or []
    self.reviews = reviews or []
    self.review_inline = review_inline or {}
    self.all_inline = all_inline or []
    self.review_decision = review_decision
    self.review_inline_calls: list[int] = []
    self.review_decision_calls = 0

  def fetch_issue_comments(self, owner, repo, pr, token):
    return self.issue_comments

  def fetch_reviews(self, owner, repo, pr, token):
    return self.reviews

  def fetch_review_inline_comments(self, owner, repo, pr, review_id, token):
    self.review_inline_calls.append(review_id)
    return self.review_inline.get(review_id, [])

  def fetch_review_comments(self, owner, repo, pr, token):
    return self.all_inline

  def fetch_review_decision(self, owner, repo, pr, token):
    self.review_decision_calls += 1
    return self.review_decision


def _install(monkeypatch, api: _FakeAPI) -> None:
  monkeypatch.setattr(poll_pr.pulls, 'issue_comments', api.fetch_issue_comments)
  monkeypatch.setattr(poll_pr.pulls, 'reviews', api.fetch_reviews)
  monkeypatch.setattr(poll_pr.pulls, 'review_inline_comments', api.fetch_review_inline_comments)
  monkeypatch.setattr(poll_pr.pulls, 'review_comments', api.fetch_review_comments)
  monkeypatch.setattr(poll_pr.pulls, 'review_decision', api.fetch_review_decision)


def _run(api: _FakeAPI, **kwargs) -> list[dict[str, Any]]:
  events: list[dict[str, Any]] = []
  poll_pr.emit_cycle(
    owner='x',
    repo='y',
    pr=1,
    token='t',
    seen_comment_ids=kwargs.pop('seen_comment_ids', set()),
    seen_review_ids=kwargs.pop('seen_review_ids', set()),
    parties=kwargs.pop('parties', {'alice'}),
    self_login=kwargs.pop('self_login', 'me'),
    emit=events.append,
  )
  return events


_RENDERED_EMIT = poll_pr._emit


@pytest.fixture(autouse=True)
def _events_as_json(monkeypatch):
  """stdout carries events as JSON lines, so the tests below read them back as dicts;
  `TestRender` and `TestTextOutput` cover the text they are rendered to."""
  monkeypatch.setattr(poll_pr, '_emit', lambda event: print(json.dumps(event), flush=True))
  monkeypatch.setattr(poll_pr.pulls, 'pull_commits', lambda *a: [])


def _poll(**overrides) -> int:
  arguments: dict[str, Any] = {
    'owner': 'o',
    'repo': 'r',
    'pr': 1,
    'token': lambda: 't',
    'interval': 0,
    'failure_grace': 300,
    'disconnect_after': 60,
    'local_tip': lambda sha: False,
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

  def test_review_carries_the_decision_the_base_reached(self, monkeypatch):
    api = _FakeAPI(
      reviews=[_review(100, 'alice', 'APPROVED')],
      review_decision='REVIEW_REQUIRED',
    )
    _install(monkeypatch, api)
    events = _run(api)
    assert events[0]['state'] == 'APPROVED'
    assert events[0]['review_decision'] == 'REVIEW_REQUIRED'

  def test_one_decision_read_serves_every_review_in_a_cycle(self, monkeypatch):
    api = _FakeAPI(reviews=[_review(100, 'alice', 'COMMENTED'), _review(101, 'alice', 'APPROVED')])
    _install(monkeypatch, api)
    events = _run(api)
    assert [e['id'] for e in events] == [100, 101]
    assert api.review_decision_calls == 1

  def test_a_cycle_emitting_no_review_leaves_the_decision_unread(self, monkeypatch):
    api = _FakeAPI(
      reviews=[_review(100, 'self-bot', 'APPROVED')],
      issue_comments=[_issue_comment(50, 'alice', 'top-level comment')],
    )
    _install(monkeypatch, api)
    events = _run(api, self_login='self-bot')
    assert [e['event'] for e in events] == ['comment']
    assert api.review_decision_calls == 0

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

    monkeypatch.setattr(poll_pr.pulls, 'review_inline_comments', failing_inline)
    with pytest.raises(urllib.error.HTTPError):
      _run(api, seen_comment_ids=seen_c, seen_review_ids=seen_r)
    assert seen_r == set()

    _install(monkeypatch, api)
    events = _run(api, seen_comment_ids=seen_c, seen_review_ids=seen_r)
    assert [e['event'] for e in events] == ['review']
    assert events[0]['comments'][0]['id'] == 200

  def test_self_authored_review_still_marks_inline_comments_seen(self, monkeypatch):
    # a self-authored review should not emit, but its inline comments must
    # still be marked seen so they don't fire as standalone events.
    api = _FakeAPI(
      reviews=[_review(100, 'self-bot', 'APPROVED')],
      review_inline={100: [_inline(200, 100, 'self-bot', 'self comment')]},
      all_inline=[_inline(200, 100, 'self-bot', 'self comment')],
    )
    _install(monkeypatch, api)
    events = _run(api, self_login='self-bot')
    assert events == []
    assert api.review_inline_calls == [100]

  def test_a_comment_from_outside_the_parties_stays_silent(self, monkeypatch):
    api = _FakeAPI(issue_comments=[_issue_comment(50, 'github-actions[bot]', 'coverage: 93%')])
    _install(monkeypatch, api)
    assert _run(api, parties={'alice'}) == []

  def test_a_review_admits_its_author_for_the_same_cycle_comments(self, monkeypatch):
    # a first-time reviewer's review and comment arrive in one cycle: the
    # review admits the login to the parties before the comment passes run
    api = _FakeAPI(
      reviews=[_review(100, 'newcomer', 'CHANGES_REQUESTED')],
      issue_comments=[_issue_comment(50, 'newcomer', 'overall: needs work')],
    )
    _install(monkeypatch, api)
    parties: set[str] = {'alice'}
    events = _run(api, parties=parties)
    assert [e['event'] for e in events] == ['review', 'comment']
    assert 'newcomer' in parties

  def test_a_party_bot_login_fires(self, monkeypatch):
    # the PR author can be an app account; a party fires whatever its suffix
    api = _FakeAPI(issue_comments=[_issue_comment(50, 'app[bot]', 'fixed in abc123')])
    _install(monkeypatch, api)
    events = _run(api, parties={'app[bot]'})
    assert [e['event'] for e in events] == ['comment']


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
    monkeypatch.setattr(poll_pr.pulls, 'issue_comments', lambda *a: [])
    monkeypatch.setattr(poll_pr.pulls, 'review_comments', lambda *a: [])
    monkeypatch.setattr(poll_pr.pulls, 'reviews', lambda *a: [])
    monkeypatch.setattr(poll_pr.api, 'viewer_login', lambda *a: 'alice')
    monkeypatch.setattr(poll_pr.time, 'sleep', lambda _: None)

  def test_transient_cycle_error_is_swallowed(self, monkeypatch, capsys):
    self._baseline(monkeypatch)
    fetch = _Stepper([_open_pr(), _http_error(503), {'merged': True}])
    monkeypatch.setattr(poll_pr.pulls, 'pull_request', fetch)
    assert _poll() == 0
    assert fetch.calls == 3
    assert 'Logging error' not in capsys.readouterr().err

  def test_the_watch_beats_before_any_request_and_every_cycle(self, monkeypatch):
    self._baseline(monkeypatch)
    events: list[str] = []
    monkeypatch.setattr(poll_pr.watches, 'beat', lambda within: events.append(f'beat {within}'))
    promise = 10 + poll_pr._CYCLE_SLACK_SECONDS
    fetch = _Stepper([_open_pr(), _open_pr(), {'merged': True}])
    monkeypatch.setattr(
      poll_pr.pulls, 'pull_request', lambda *a: (events.append('request'), fetch(*a))[1]
    )

    assert _poll(interval=10) == 0

    assert events == [
      f'beat {promise}',
      'request',
      f'beat {promise}',
      'request',
      f'beat {promise}',
      'request',
    ]

  def test_remote_disconnected_cycle_error_is_swallowed(self, monkeypatch):
    self._baseline(monkeypatch)
    fetch = _Stepper(
      [_open_pr(), http.client.RemoteDisconnected('server closed connection'), {'merged': True}]
    )
    monkeypatch.setattr(poll_pr.pulls, 'pull_request', fetch)
    assert _poll() == 0
    assert fetch.calls == 3


class _Response(io.BytesIO):
  headers: ClassVar[dict[str, str]] = {}

  def __enter__(self):
    return self


class TestSourceFailures:
  def _baseline(self, monkeypatch):
    monkeypatch.setattr(poll_pr, '_owner_login', lambda *a: 'owner')
    monkeypatch.setattr(poll_pr.pulls, 'issue_comments', lambda *a: [])
    monkeypatch.setattr(poll_pr.pulls, 'review_comments', lambda *a: [])
    monkeypatch.setattr(poll_pr.pulls, 'reviews', lambda *a: [])
    monkeypatch.setattr(poll_pr.pulls, 'review_inline_comments', lambda *a: [])
    monkeypatch.setattr(poll_pr.pulls, 'review_decision', lambda *a: 'APPROVED')
    monkeypatch.setattr(poll_pr.api, 'viewer_login', lambda *a: 'alice')
    monkeypatch.setattr(poll_pr.time, 'sleep', lambda _: None)

  def _clock(self, monkeypatch, step: float) -> None:
    """a clock that only the poll interval moves, `step` seconds per cycle."""
    now = [0.0]
    monkeypatch.setattr(poll_pr.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(poll_pr.time, 'sleep', lambda _: now.__setitem__(0, now[0] + step))

  def test_a_failing_source_does_not_suppress_the_others(self, monkeypatch, capsys):
    # a token without `checks: read` 403s on check-runs while reviews and
    # comments stay readable
    self._baseline(monkeypatch)
    monkeypatch.setattr(
      poll_pr.pulls, 'pull_request', _Stepper([_open_pr(), _open_pr(), {'merged': True}])
    )
    monkeypatch.setattr(poll_pr, '_fetch_check_runs', _Stepper([_http_error(403)]))
    # empty at the startup baseline scan, so the review counts as new
    monkeypatch.setattr(
      poll_pr.pulls, 'reviews', _Stepper([[], [_review(100, 'owner', 'APPROVED')]])
    )
    assert _poll() == 0
    events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert [e['event'] for e in events] == ['review', 'merged']

  def test_a_refused_graphql_read_ends_the_watch_through_its_source(self, monkeypatch, capsys):
    self._baseline(monkeypatch)
    self._clock(monkeypatch, step=10)
    monkeypatch.setattr(poll_pr.pulls, 'pull_request', lambda *a: _open_pr())
    monkeypatch.setattr(poll_pr, '_fetch_check_runs', lambda *a: [])
    # empty at the startup baseline scan, so every cycle after it has a review
    # to emit — and therefore a decision to read
    baseline_scan = iter([[]])
    monkeypatch.setattr(
      poll_pr.pulls, 'reviews', lambda *a: next(baseline_scan, [_review(100, 'owner', 'APPROVED')])
    )

    def refused(*a):
      raise poll_pr.api.GraphQLError('reading the review decision: [{"type": "FORBIDDEN"}]')

    monkeypatch.setattr(poll_pr.pulls, 'review_decision', refused)
    assert _poll(failure_grace=30) == 2
    events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert [e['event'] for e in events] == ['watch_failed']
    assert events[0]['source'] == 'reviews'

  def test_a_source_failing_past_the_grace_window_ends_the_watch(self, monkeypatch, capsys):
    self._baseline(monkeypatch)
    self._clock(monkeypatch, step=10)
    monkeypatch.setattr(poll_pr.pulls, 'pull_request', lambda *a: _open_pr())

    def failing_checks(*a):
      raise _http_error(403)

    monkeypatch.setattr(poll_pr, '_fetch_check_runs', failing_checks)
    assert _poll(failure_grace=30) == 2
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
    monkeypatch.setattr(
      poll_pr.pulls,
      'pull_request',
      _Stepper([_open_pr(), _open_pr(), _open_pr(), _open_pr(), {'merged': True}]),
    )
    # each failure is 20s past the previous one — only an unbroken run of them
    # crosses the 30s window
    monkeypatch.setattr(
      poll_pr, '_fetch_check_runs', _Stepper([_http_error(403), [], _http_error(403), []])
    )
    assert _poll(failure_grace=30) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert events == [{'event': 'merged', 'pr': 1}]

  def test_a_source_failing_past_the_disconnect_window_reports_once_and_again_on_recovery(
    self, monkeypatch, capsys
  ):
    self._baseline(monkeypatch)
    self._clock(monkeypatch, step=10)
    monkeypatch.setattr(
      poll_pr.pulls, 'pull_request', _Stepper([_open_pr()] * 9 + [{'merged': True}])
    )
    failure = _http_error(503)
    # failing from the first cycle (t=0) to the sixth (t=50), healthy from t=60
    monkeypatch.setattr(poll_pr, '_fetch_check_runs', _Stepper([failure] * 6 + [[]] * 3))
    assert _poll(disconnect_after=30) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert events == [
      {
        'event': 'disconnected',
        'pr': 1,
        'source': 'checks',
        'reason': 'HTTP 503',
        'failing_for': 30,
      },
      {'event': 'reconnected', 'pr': 1, 'disconnected_for': 60},
      {'event': 'merged', 'pr': 1},
    ]

  def _through_the_retry_layer(self, monkeypatch, failures: int):
    """pull-request reads that go through the real API retry loop and fail
    `failures` times with a 30 s `Retry-After`, the retry sleeps moving the clock."""
    now = [0.0]
    monkeypatch.setattr(poll_pr.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(poll_pr.time, 'sleep', lambda _: None)
    monkeypatch.setattr(retry, 'sleep', lambda seconds: now.__setitem__(0, now[0] + seconds))
    refusal = urllib.error.HTTPError(
      'https://api.github.com/p', 403, 'Forbidden', email.message.Message(), None
    )
    refusal.headers['Retry-After'] = '30'
    answers = [refusal] * failures + [_open_pr(), _open_pr(), {'merged': True}]

    def urlopen(request, *, timeout):
      answer = answers.pop(0)
      if isinstance(answer, BaseException):
        raise answer
      return _Response(json.dumps(answer).encode())

    monkeypatch.setattr(poll_pr.api.urllib.request, 'urlopen', urlopen)
    api_get = poll_pr.api.get
    reads = iter([_open_pr()])
    monkeypatch.setattr(
      poll_pr.pulls,
      'pull_request',
      lambda *a: next(reads, None) or api_get('https://api.github.com/p', 't'),
    )
    monkeypatch.setattr(poll_pr, '_fetch_check_runs', lambda *a: [])
    return now

  def test_an_outage_is_reported_while_the_api_still_retries(self, monkeypatch, capsys):
    self._baseline(monkeypatch)
    now = self._through_the_retry_layer(monkeypatch, failures=4)
    assert _poll() == 0
    events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert [e['event'] for e in events] == ['disconnected', 'reconnected', 'merged']
    assert events[0]['failing_for'] == 60
    assert now[0] == 120

  def test_a_retry_exhaustion_does_not_report_the_outage_twice(self, monkeypatch, capsys):
    self._baseline(monkeypatch)
    self._through_the_retry_layer(monkeypatch, failures=5)
    assert _poll() == 0
    events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert [e['event'] for e in events] == ['disconnected', 'reconnected', 'merged']

  def test_a_blip_shorter_than_the_disconnect_window_reports_nothing(self, monkeypatch, capsys):
    self._baseline(monkeypatch)
    self._clock(monkeypatch, step=10)
    monkeypatch.setattr(
      poll_pr.pulls, 'pull_request', _Stepper([_open_pr()] * 4 + [{'merged': True}])
    )
    monkeypatch.setattr(poll_pr, '_fetch_check_runs', _Stepper([_http_error(503)] * 2 + [[]] * 3))
    assert _poll(disconnect_after=30) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert events == [{'event': 'merged', 'pr': 1}]

  def test_a_failed_baseline_ends_the_watch_before_the_first_cycle(self, monkeypatch, capsys):
    self._baseline(monkeypatch)

    def failing_reviews(*a):
      raise _http_error(403)

    monkeypatch.setattr(poll_pr.pulls, 'reviews', failing_reviews)
    monkeypatch.setattr(poll_pr.pulls, 'pull_request', lambda *a: _open_pr())
    assert _poll() == 2
    events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert [(e['event'], e['source']) for e in events] == [('watch_failed', 'baseline')]

  def test_a_pr_unreadable_at_the_baseline_ends_the_watch(self, monkeypatch, capsys):
    self._baseline(monkeypatch)

    def missing_pr(*a):
      raise _http_error(404)

    monkeypatch.setattr(poll_pr.pulls, 'pull_request', missing_pr)
    assert _poll() == 2
    events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert [(e['event'], e['source']) for e in events] == [('watch_failed', 'baseline')]

  def test_a_not_found_ends_the_watch_once_the_window_closes(self, monkeypatch, capsys):
    self._baseline(monkeypatch)
    self._clock(monkeypatch, step=10)
    not_found = [_http_error(404) for _ in range(4)]
    monkeypatch.setattr(poll_pr.pulls, 'pull_request', _Stepper([_open_pr(), *not_found]))
    monkeypatch.setattr(poll_pr, '_fetch_check_runs', lambda *a: [])
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
    monkeypatch.setattr(
      poll_pr.pulls, 'pull_request', _Stepper([_open_pr(), _http_error(404), {'merged': True}])
    )
    monkeypatch.setattr(poll_pr, '_fetch_check_runs', lambda *a: [])
    assert _poll(failure_grace=30) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert events == [{'event': 'merged', 'pr': 1}]


def _check_run(name: str, status: str, conclusion: Optional[str] = None) -> dict[str, Any]:
  return {
    'name': name,
    'status': status,
    'conclusion': conclusion,
    'html_url': f'https://github.com/x/y/runs/{name}',
  }


def _edge(transition: Optional[poll_pr.CheckTransition]) -> Optional[str]:
  return None if transition is None else transition.state


class TestRender:
  def test_a_bare_event_is_its_kind_alone(self):
    assert poll_pr.render({'event': 'merged', 'pr': 7}) == 'merged:'

  def test_fields_are_indented_under_the_kind_and_the_pr_is_left_out(self):
    event = {'event': 'pushed', 'pr': 7, 'head': 'abc'}
    assert poll_pr.render(event) == 'pushed:\n  head: abc'

  def test_values_are_plain_none_is_spelled_out_and_empty_is_left_out(self):
    event = {'event': 'disconnected', 'reason': 'HTTP 403', 'decision': None, 'empty': ''}
    assert poll_pr.render(event) == 'disconnected:\n  reason: HTTP 403\n  decision: none'

  def test_durations_count_seconds(self):
    event = {'event': 'reconnected', 'disconnected_for': 245}
    assert poll_pr.render(event) == 'reconnected:\n  disconnected_for: 245s'

  def test_a_body_is_a_block_with_its_blank_lines_empty(self):
    event = {'event': 'comment', 'user': 'bob', 'body': 'first\n\nsecond'}
    assert poll_pr.render(event).splitlines() == [
      'comment:',
      '  user: bob',
      '  body: |',
      '    first',
      '',
      '    second',
    ]

  def test_an_empty_body_is_left_out(self):
    assert poll_pr.render({'event': 'comment', 'body': ' \n'}) == 'comment:'

  def test_list_items_are_dashed_with_their_body_a_level_deeper(self):
    event = {
      'event': 'review',
      'id': 2,
      'comments': [
        {'id': 3, 'path': 'a.py', 'line': None, 'body': 'fix this'},
        {'id': 5, 'path': 'b.py', 'body': ''},
      ],
    }
    assert poll_pr.render(event).splitlines() == [
      'review:',
      '  id: 2',
      '  comments:',
      '    - id: 3',
      '      path: a.py',
      '      line: none',
      '      body: |',
      '        fix this',
      '    - id: 5',
      '      path: b.py',
    ]

  def test_an_empty_list_adds_no_lines(self):
    event = {'event': 'checks', 'state': 'green', 'failing': []}
    assert poll_pr.render(event) == 'checks:\n  state: green'


class TestTextOutput:
  def test_events_reach_stdout_stamped_and_rendered(self, monkeypatch, capsys):
    monkeypatch.setattr(poll_pr, '_emit', _RENDERED_EMIT)
    monkeypatch.setattr(poll_pr, '_timestamp', lambda: '2026-10-22T03:41:07Z')
    poll_pr._emit({'event': 'conflicts', 'pr': 9})
    assert capsys.readouterr().out == 'conflicts:\n  at: 2026-10-22T03:41:07Z\n'

  def test_a_watch_producer_marks_every_line_after_the_first_as_a_continuation(
    self, monkeypatch, capsys
  ):
    monkeypatch.setattr(poll_pr, '_emit', _RENDERED_EMIT)
    monkeypatch.setattr(poll_pr, '_timestamp', lambda: '2026-10-22T03:41:07Z')
    monkeypatch.setattr(watches, 'diagnostics_path', lambda: Path('/diagnostics'))
    poll_pr._emit({'event': 'conflicts', 'pr': 9})
    assert capsys.readouterr().out == (
      f'conflicts:\n{watches.continued("  at: 2026-10-22T03:41:07Z")}\n'
    )

  def test_the_timestamp_is_utc_to_the_second(self):
    assert re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', poll_pr._timestamp())


class TestMergedEvent:
  def _merged(self, monkeypatch, capsys, **pr_fields):
    monkeypatch.setattr(poll_pr, '_owner_login', lambda *a: 'owner')
    for name in ('issue_comments', 'review_comments', 'reviews'):
      monkeypatch.setattr(poll_pr.pulls, name, lambda *a: [])
    monkeypatch.setattr(poll_pr.api, 'viewer_login', lambda *a: 'alice')
    monkeypatch.setattr(poll_pr.time, 'sleep', lambda _: None)
    monkeypatch.setattr(
      poll_pr.pulls, 'pull_request', _Stepper([_open_pr(), {'merged': True, **pr_fields}])
    )
    monkeypatch.setattr(poll_pr, '_fetch_check_runs', lambda *a: [])
    assert _poll() == 0
    return [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]

  def test_it_names_who_merged_what_and_the_commits_the_pr_carried(self, monkeypatch, capsys):
    monkeypatch.setattr(
      poll_pr.pulls,
      'pull_commits',
      lambda *a: [
        {'sha': 'aaa', 'commit': {'message': 'first\n\nbody'}},
        {'sha': 'bbb', 'commit': {'message': 'second'}},
      ],
    )
    events = self._merged(
      monkeypatch, capsys, merged_by={'login': 'dzhioev'}, merge_commit_sha='ccc'
    )
    assert events == [
      {
        'event': 'merged',
        'pr': 1,
        'merged_by': 'dzhioev',
        'merge_commit': 'ccc',
        'commits': [{'sha': 'aaa', 'subject': 'first'}, {'sha': 'bbb', 'subject': 'second'}],
      }
    ]

  def test_a_failed_commit_read_still_reports_the_merge(self, monkeypatch, capsys):
    def failing(*a):
      raise _http_error(503)

    monkeypatch.setattr(poll_pr.pulls, 'pull_commits', failing)
    events = self._merged(monkeypatch, capsys, merge_commit_sha='ccc')
    assert events == [{'event': 'merged', 'pr': 1, 'merge_commit': 'ccc'}]


class TestCheckTracker:
  def test_fires_once_per_red_episode_and_rearms(self):
    tracker = poll_pr.CheckTracker()
    running = [_check_run('tests', 'in_progress')]
    failed = [_check_run('tests', 'completed', 'failure')]
    passed = [_check_run('tests', 'completed', 'success')]

    assert tracker.update('head', running, 'unstable') is None
    fired = tracker.update('head', failed, 'unstable')
    assert fired is not None
    assert fired.state == 'red'
    assert [run['name'] for run in fired.failed_runs] == ['tests']
    assert tracker.update('head', failed, 'unstable') is None
    assert _edge(tracker.update('head', passed, 'clean')) == 'green'
    assert _edge(tracker.update('head', failed, 'unstable')) == 'red'
    assert _edge(tracker.update('head', passed, 'clean')) == 'green'

  def test_a_run_starting_on_a_green_head_takes_the_green_back(self):
    tracker = poll_pr.CheckTracker()
    passed = [_check_run('tests', 'completed', 'success')]
    rerun = [_check_run('tests', 'queued')]

    assert _edge(tracker.update('head', passed, 'clean')) == 'green'
    assert tracker.update('head', passed, 'clean') is None
    assert _edge(tracker.update('head', rerun, 'unstable')) == 'pending'
    assert tracker.update('head', rerun, 'unstable') is None
    assert _edge(tracker.update('head', passed, 'clean')) == 'green'

  def test_a_new_head_rearms_green(self):
    tracker = poll_pr.CheckTracker()
    passed = [_check_run('tests', 'completed', 'success')]

    assert _edge(tracker.update('first', passed, 'clean')) == 'green'
    assert tracker.update('first', passed, 'clean') is None
    assert _edge(tracker.update('second', passed, 'clean')) == 'green'

  def test_neutral_and_skipped_are_green(self):
    tracker = poll_pr.CheckTracker()
    runs = [_check_run('a', 'completed', 'neutral'), _check_run('b', 'completed', 'skipped')]
    assert _edge(tracker.update('head', runs, 'clean')) == 'green'

  def test_no_runs_on_a_merge_held_for_a_check_is_not_green(self):
    assert poll_pr.CheckTracker().update('head', [], 'blocked') is None

  def test_no_runs_on_a_merge_held_for_none_fires_green_once(self):
    tracker = poll_pr.CheckTracker()

    assert _edge(tracker.update('head', [], 'clean')) == 'green'
    assert tracker.update('head', [], 'clean') is None

  def test_a_run_registering_after_the_empty_green_takes_it_back(self):
    tracker = poll_pr.CheckTracker()

    assert _edge(tracker.update('head', [], 'clean')) == 'green'
    assert _edge(tracker.update('head', [_check_run('tests', 'queued')], 'unstable')) == 'pending'
    passed = [_check_run('tests', 'completed', 'success')]
    assert _edge(tracker.update('head', passed, 'clean')) == 'green'

  def test_a_merge_no_longer_held_for_none_takes_the_empty_green_back(self):
    tracker = poll_pr.CheckTracker()

    assert _edge(tracker.update('head', [], 'clean')) == 'green'
    assert _edge(tracker.update('head', [], 'unstable')) == 'pending'

  def test_an_uncomputed_merge_state_moves_no_edge(self):
    tracker = poll_pr.CheckTracker()

    assert tracker.update('head', [], 'unknown') is None
    assert _edge(tracker.update('head', [], 'clean')) == 'green'
    assert tracker.update('head', [], 'unknown') is None


class TestCheckEvents:
  def _baseline(self, monkeypatch):
    monkeypatch.setattr(poll_pr, '_owner_login', lambda *a: 'owner')
    monkeypatch.setattr(poll_pr.pulls, 'issue_comments', lambda *a: [])
    monkeypatch.setattr(poll_pr.pulls, 'review_comments', lambda *a: [])
    monkeypatch.setattr(poll_pr.pulls, 'reviews', lambda *a: [])
    monkeypatch.setattr(poll_pr.api, 'viewer_login', lambda *a: 'alice')
    monkeypatch.setattr(poll_pr.time, 'sleep', lambda _: None)

  def test_failing_check_emits_one_event_with_the_run_url(self, monkeypatch, capsys):
    self._baseline(monkeypatch)
    monkeypatch.setattr(
      poll_pr.pulls,
      'pull_request',
      _Stepper([_open_pr(), _open_pr(), _open_pr(), {'merged': True}]),
    )
    monkeypatch.setattr(
      poll_pr,
      '_fetch_check_runs',
      _Stepper(
        [[_check_run('tests', 'in_progress')], [_check_run('tests', 'completed', 'failure')]]
      ),
    )
    assert _poll() == 0

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    checks = [e for e in events if e['event'] == 'checks']
    assert len(checks) == 1
    assert checks[0]['state'] == 'red'
    assert checks[0]['failing'] == [
      {'name': 'tests', 'conclusion': 'failure', 'url': 'https://github.com/x/y/runs/tests'}
    ]

  def test_all_checks_green_emits_one_event(self, monkeypatch, capsys):
    self._baseline(monkeypatch)
    monkeypatch.setattr(
      poll_pr.pulls,
      'pull_request',
      _Stepper([_open_pr(), _open_pr(), _open_pr(), {'merged': True}]),
    )
    monkeypatch.setattr(
      poll_pr,
      '_fetch_check_runs',
      _Stepper(
        [[_check_run('tests', 'in_progress')], [_check_run('tests', 'completed', 'success')]]
      ),
    )
    assert _poll() == 0

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    checks = [event for event in events if event['event'] == 'checks']
    assert checks == [
      {'event': 'checks', 'pr': 1, 'head': 'deadbeef', 'state': 'green', 'failing': []}
    ]

  def test_a_head_no_run_reports_on_turns_green_once_its_merge_is_held_for_none(
    self, monkeypatch, capsys
  ):
    self._baseline(monkeypatch)
    monkeypatch.setattr(
      poll_pr.pulls,
      'pull_request',
      _Stepper(
        [
          _open_pr(),
          _open_pr(mergeable_state='unknown'),
          _open_pr(mergeable_state='clean'),
          {'merged': True},
        ]
      ),
    )
    monkeypatch.setattr(poll_pr, '_fetch_check_runs', lambda *a: [])
    assert _poll() == 0

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    checks = [event for event in events if event['event'] == 'checks']
    assert checks == [
      {'event': 'checks', 'pr': 1, 'head': 'deadbeef', 'state': 'green', 'failing': []}
    ]

  def test_a_pr_without_a_head_sha_skips_the_check_fetch(self, monkeypatch):
    self._baseline(monkeypatch)
    monkeypatch.setattr(
      poll_pr.pulls, 'pull_request', lambda *a: {'merged': True, **_user('alice')}
    )
    fetched: list[int] = []
    monkeypatch.setattr(poll_pr, '_fetch_check_runs', lambda *a: fetched.append(1) or [])
    assert _poll() == 0
    assert fetched == []


class TestConflictDetection:
  def _baseline(self, monkeypatch):
    monkeypatch.setattr(poll_pr, '_owner_login', lambda *a: 'owner')
    monkeypatch.setattr(poll_pr.pulls, 'issue_comments', lambda *a: [])
    monkeypatch.setattr(poll_pr.pulls, 'review_comments', lambda *a: [])
    monkeypatch.setattr(poll_pr.pulls, 'reviews', lambda *a: [])
    monkeypatch.setattr(poll_pr.api, 'viewer_login', lambda *a: 'alice')
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

    def fake_pull_request(*a):
      pr_calls.append(1)
      return pr_steps[len(pr_calls) - 1]

    monkeypatch.setattr(poll_pr.pulls, 'pull_request', fake_pull_request)
    assert _poll() == 0
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

    def fake_pull_request(*a):
      pr_calls.append(1)
      return pr_steps[len(pr_calls) - 1]

    monkeypatch.setattr(poll_pr.pulls, 'pull_request', fake_pull_request)
    assert _poll() == 0
    events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert events == [{'event': 'merged', 'pr': 1}]


class TestTokenAndSelf:
  def _baseline(self, monkeypatch):
    monkeypatch.setattr(poll_pr, '_owner_login', lambda *a: 'alice')
    monkeypatch.setattr(poll_pr.pulls, 'issue_comments', lambda *a: [])
    monkeypatch.setattr(poll_pr.pulls, 'review_comments', lambda *a: [])
    monkeypatch.setattr(poll_pr.pulls, 'reviews', lambda *a: [])
    monkeypatch.setattr(poll_pr.api, 'viewer_login', lambda *a: 'alice')
    monkeypatch.setattr(poll_pr.time, 'sleep', lambda _: None)

  def test_token_provider_is_consulted_every_cycle(self, monkeypatch):
    self._baseline(monkeypatch)
    tokens_seen: list[str] = []
    pr_steps: list[dict[str, Any]] = [_open_pr(), _open_pr(), {'merged': True}]
    monkeypatch.setattr(poll_pr, '_fetch_check_runs', lambda *a: [])

    def fake_pull_request(owner, repo, pr, token):
      tokens_seen.append(token)
      return pr_steps[len(tokens_seen) - 1]

    monkeypatch.setattr(poll_pr.pulls, 'pull_request', fake_pull_request)
    counter = itertools.count(1)
    provider = lambda: f't{next(counter)}'  # noqa: E731
    assert _poll(token=provider) == 0
    # the baseline and each cycle re-read the provider: three distinct tokens
    assert len(tokens_seen) == 3
    assert len(set(tokens_seen)) == 3

  def test_self_is_the_account_the_token_acts_for(self, monkeypatch, capsys):
    self._baseline(monkeypatch)
    own_comment = _issue_comment(50, 'alice', 'my own note')
    # absent at the startup baseline scan, appears on the first poll cycle
    monkeypatch.setattr(
      poll_pr.pulls, 'issue_comments', _Stepper([[], [own_comment], [own_comment]])
    )
    monkeypatch.setattr(
      poll_pr.pulls, 'pull_request', _Stepper([_open_pr(), _open_pr(), {'merged': True}])
    )
    monkeypatch.setattr(poll_pr, '_fetch_check_runs', lambda *a: [])
    assert _poll() == 0
    out = capsys.readouterr().out
    assert '"comment"' not in out
    assert json.loads(out.strip().splitlines()[-1]) == {'event': 'merged', 'pr': 1}

  def test_a_party_seeded_from_a_baseline_review_fires_later_comments(self, monkeypatch, capsys):
    self._baseline(monkeypatch)
    # 'critic' is neither the author nor the repo owner — only the review it
    # posted before the watch started makes it a party
    monkeypatch.setattr(
      poll_pr.pulls, 'reviews', lambda *a: [_review(100, 'critic', 'CHANGES_REQUESTED')]
    )
    monkeypatch.setattr(
      poll_pr.pulls,
      'issue_comments',
      _Stepper([[], [_issue_comment(50, 'critic', 'still waiting on the rename')]]),
    )
    monkeypatch.setattr(
      poll_pr.pulls, 'pull_request', _Stepper([_open_pr(), _open_pr(), {'merged': True}])
    )
    monkeypatch.setattr(poll_pr, '_fetch_check_runs', lambda *a: [])
    assert _poll() == 0
    events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert [e['event'] for e in events] == ['comment', 'merged']
    assert events[0]['user'] == 'critic'

  def test_a_reviewer_side_watch_hears_the_bot_author(self, monkeypatch, capsys):
    self._baseline(monkeypatch)
    monkeypatch.setattr(
      poll_pr.pulls,
      'issue_comments',
      _Stepper([[], [_issue_comment(50, 'app[bot]', 'fixed in abc123')]]),
    )
    monkeypatch.setattr(
      poll_pr.pulls,
      'pull_request',
      _Stepper([_open_pr('app[bot]'), _open_pr('app[bot]'), {'merged': True}]),
    )
    monkeypatch.setattr(poll_pr, '_fetch_check_runs', lambda *a: [])
    monkeypatch.setattr(poll_pr.api, 'viewer_login', lambda *a: 'eyebro')
    assert _poll() == 0
    events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert [e['event'] for e in events] == ['comment', 'merged']
    assert events[0]['user'] == 'app[bot]'


class TestPushedEvents:
  def _baseline(self, monkeypatch):
    monkeypatch.setattr(poll_pr, '_owner_login', lambda *a: 'owner')
    monkeypatch.setattr(poll_pr.pulls, 'issue_comments', lambda *a: [])
    monkeypatch.setattr(poll_pr.pulls, 'review_comments', lambda *a: [])
    monkeypatch.setattr(poll_pr.pulls, 'reviews', lambda *a: [])
    monkeypatch.setattr(poll_pr, '_fetch_check_runs', lambda *a: [])
    monkeypatch.setattr(poll_pr.api, 'viewer_login', lambda *a: 'alice')
    monkeypatch.setattr(poll_pr.time, 'sleep', lambda _: None)

  def test_a_moved_head_fires_once_with_the_new_sha(self, monkeypatch, capsys):
    self._baseline(monkeypatch)
    monkeypatch.setattr(
      poll_pr.pulls,
      'pull_request',
      _Stepper([_open_pr(sha='aaa'), _open_pr(sha='aaa'), _open_pr(sha='bbb'), {'merged': True}]),
    )
    assert _poll() == 0
    events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert events == [{'event': 'pushed', 'pr': 1, 'head': 'bbb'}, {'event': 'merged', 'pr': 1}]

  def test_the_baseline_head_does_not_fire(self, monkeypatch, capsys):
    self._baseline(monkeypatch)
    monkeypatch.setattr(
      poll_pr.pulls,
      'pull_request',
      _Stepper([_open_pr(sha='aaa'), _open_pr(sha='aaa'), {'merged': True}]),
    )
    assert _poll() == 0
    events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert events == [{'event': 'merged', 'pr': 1}]

  def _green_after_a_move_to(self, monkeypatch, capsys, local_tip) -> list[dict[str, Any]]:
    monkeypatch.setattr(
      poll_pr.pulls,
      'pull_request',
      _Stepper([_open_pr(sha='aaa'), _open_pr(sha='aaa'), _open_pr(sha='bbb'), {'merged': True}]),
    )
    monkeypatch.setattr(
      poll_pr,
      '_fetch_check_runs',
      lambda owner, repo, sha, token: (
        [_check_run('tests', 'completed', 'success')]
        if sha == 'bbb'
        else [_check_run('tests', 'in_progress')]
      ),
    )
    assert _poll(local_tip=local_tip) == 0
    return [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]

  def test_a_move_to_a_local_branch_tip_stays_quiet_but_its_checks_fire(self, monkeypatch, capsys):
    self._baseline(monkeypatch)
    events = self._green_after_a_move_to(monkeypatch, capsys, local_tip=lambda sha: sha == 'bbb')
    assert events == [
      {'event': 'checks', 'pr': 1, 'head': 'bbb', 'state': 'green', 'failing': []},
      {'event': 'merged', 'pr': 1},
    ]

  def test_a_move_no_local_branch_holds_fires_before_its_checks(self, monkeypatch, capsys):
    self._baseline(monkeypatch)
    events = self._green_after_a_move_to(monkeypatch, capsys, local_tip=lambda sha: sha == 'aaa')
    assert events == [
      {'event': 'pushed', 'pr': 1, 'head': 'bbb'},
      {'event': 'checks', 'pr': 1, 'head': 'bbb', 'state': 'green', 'failing': []},
      {'event': 'merged', 'pr': 1},
    ]


def _git(root: Path, *arguments: str) -> str:
  return subprocess.run(
    ['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *arguments],
    cwd=root,
    check=True,
    capture_output=True,
    text=True,
  ).stdout.strip()


@pytest.fixture
def checkout(tmp_path) -> Path:
  root = tmp_path / 'checkout'
  root.mkdir()
  _git(root, 'init', '-q', '-b', 'master')
  _git(root, 'commit', '-q', '--allow-empty', '-m', 'first')
  _git(root, 'commit', '-q', '--allow-empty', '-m', 'second')
  return root


class TestBranchTipProbe:
  def test_a_branch_tip_is_held_and_its_ancestor_is_not(self, checkout):
    probe = poll_pr.branch_tip_probe(checkout)
    assert probe(_git(checkout, 'rev-parse', 'HEAD'))
    assert not probe(_git(checkout, 'rev-parse', 'HEAD~1'))

  def test_a_commit_fetched_into_a_remote_ref_is_not_held(self, checkout, tmp_path):
    clone = tmp_path / 'clone'
    _git(tmp_path, 'clone', '-q', str(checkout), str(clone))
    _git(checkout, 'commit', '-q', '--allow-empty', '-m', 'pushed from elsewhere')
    _git(clone, 'fetch', '-q', 'origin')
    assert not poll_pr.branch_tip_probe(clone)(_git(checkout, 'rev-parse', 'HEAD'))

  def test_an_unknown_sha_is_not_held(self, checkout):
    assert not poll_pr.branch_tip_probe(checkout)('0' * 40)

  def test_the_working_directory_is_the_default_checkout(self, checkout, monkeypatch):
    monkeypatch.chdir(checkout)
    assert poll_pr.branch_tip_probe(None)(_git(checkout, 'rev-parse', 'HEAD'))

  def test_a_working_directory_outside_a_repository_holds_nothing(
    self, checkout, tmp_path, monkeypatch
  ):
    outside = tmp_path / 'outside'
    outside.mkdir()
    monkeypatch.setenv('GIT_CEILING_DIRECTORIES', str(tmp_path))
    monkeypatch.chdir(outside)
    assert not poll_pr.branch_tip_probe(None)(_git(checkout, 'rev-parse', 'HEAD'))

  def test_a_named_checkout_outside_a_repository_is_an_error(self, tmp_path, monkeypatch):
    monkeypatch.setenv('GIT_CEILING_DIRECTORIES', str(tmp_path.parent))
    with pytest.raises(subprocess.CalledProcessError):
      poll_pr.branch_tip_probe(tmp_path)


class TestMain:
  def _capture_poll(self, monkeypatch) -> dict:
    captured = {}

    def fake_poll(owner, repo, pr, token, interval, failure_grace, disconnect_after, local_tip):
      captured['token'] = token
      captured['failure_grace'] = failure_grace
      captured['disconnect_after'] = disconnect_after
      captured['local_tip'] = local_tip
      return 0

    monkeypatch.setattr(poll_pr, 'poll_pr', fake_poll)
    return captured

  def test_the_token_is_the_picked_github_instance(self, monkeypatch, tmp_path):
    captured = self._capture_poll(monkeypatch)
    (tmp_path / 'creds').mkdir()
    (tmp_path / 'creds' / 'github+reviewer.cred').write_text('reviewer-token')
    (tmp_path / 'creds.json').write_text(json.dumps({'defaults': ['github+reviewer']}))
    registry = {'github': credentials.CredentialKind('github', 'GitHub access')}
    with credentials.as_default_store(credentials.Store(registry, tmp_path, {})):
      assert poll_pr.main(['poll-pr', 'x/y', '1']) == 0
      assert captured['token']() == 'reviewer-token'

  def test_failure_grace_flag_reaches_the_watch(self, monkeypatch):
    captured = self._capture_poll(monkeypatch)
    assert poll_pr.main(['poll-pr', 'x/y', '1', '--failure-grace', '60']) == 0
    assert captured['failure_grace'] == 60

  def test_a_producer_keeps_the_log_out_of_the_watch_output(self, monkeypatch, tmp_path):
    self._capture_poll(monkeypatch)
    diagnostics = tmp_path / 'diagnostics'
    redirected: list[Path] = []
    monkeypatch.setattr(poll_pr.watches, 'diagnostics_path', lambda: diagnostics)
    monkeypatch.setattr(poll_pr.log, 'to_file', redirected.append)
    assert poll_pr.main(['poll-pr', 'x/y', '1']) == 0
    assert redirected == [diagnostics]

  def test_disconnect_after_flag_reaches_the_watch(self, monkeypatch):
    captured = self._capture_poll(monkeypatch)
    assert poll_pr.main(['poll-pr', 'x/y', '1']) == 0
    assert captured['disconnect_after'] == 60
    assert poll_pr.main(['poll-pr', 'x/y', '1', '--disconnect-after', '20']) == 0
    assert captured['disconnect_after'] == 20

  def test_repo_flag_names_the_checkout_whose_tips_stay_quiet(self, monkeypatch, checkout):
    captured = self._capture_poll(monkeypatch)
    assert poll_pr.main(['poll-pr', 'x/y', '1', '--repo', str(checkout)]) == 0
    assert captured['local_tip'](_git(checkout, 'rev-parse', 'HEAD'))
