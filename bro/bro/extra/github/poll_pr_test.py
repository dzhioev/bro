#!/usr/bin/env python
from typing import Any

import poll_pr


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
    issue_comments: list[dict[str, Any]] | None = None,
    reviews: list[dict[str, Any]] | None = None,
    review_inline: dict[int, list[dict[str, Any]]] | None = None,
    all_inline: list[dict[str, Any]] | None = None,
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
