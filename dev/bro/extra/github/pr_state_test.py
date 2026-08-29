#!/usr/bin/env python
import json
from typing import Any

from bro.extra.github import pr_state


def _pull(**extra: Any) -> dict[str, Any]:
  return {
    'number': 7,
    'html_url': 'https://github.com/x/y/pull/7',
    'state': 'open',
    'merged': False,
    'draft': False,
    'title': 'a change',
    'body': 'what it does',
    'user': {'login': 'author[bot]'},
    'base': {'ref': 'master'},
    'head': {'ref': 'topic', 'sha': 'deadbeef'},
    **extra,
  }


def _review(login: str) -> dict[str, Any]:
  return {
    'id': 100,
    'user': {'login': login},
    'state': 'CHANGES_REQUESTED',
    'body': 'one blocker',
    'submitted_at': '2026-08-29T10:00:00Z',
    'html_url': 'https://github.com/x/y/pull/7#pullrequestreview-100',
  }


def _comment(id: int, login: str, **extra: Any) -> dict[str, Any]:
  return {
    'id': id,
    'user': {'login': login},
    'path': 'src/foo.py',
    'line': 12,
    'body': 'rename this',
    'html_url': f'https://github.com/x/y/pull/7#discussion_r{id}',
    **extra,
  }


def _install(monkeypatch, viewer='reviewer[bot]', pull=None, reviews=(), comments=()) -> None:
  monkeypatch.setattr(pr_state.api, 'viewer_login', lambda token: viewer)
  monkeypatch.setattr(pr_state.pulls, 'pull_request', lambda *a: pull or _pull())
  monkeypatch.setattr(pr_state.pulls, 'reviews', lambda *a: list(reviews))
  monkeypatch.setattr(pr_state.pulls, 'review_comments', lambda *a: list(comments))


class TestPrState:
  def test_logins_come_from_one_surface(self, monkeypatch):
    _install(
      monkeypatch,
      reviews=[_review('reviewer[bot]')],
      comments=[_comment(200, 'reviewer[bot]'), _comment(201, 'author[bot]')],
    )
    state = pr_state.pr_state('x', 'y', 7, 't')
    assert state['viewer'] == 'reviewer[bot]'
    assert state['pull_request']['author'] == 'author[bot]'
    assert [r['user'] for r in state['reviews']] == ['reviewer[bot]']
    assert [c['user'] for c in state['comments']] == ['reviewer[bot]', 'author[bot]']

  def test_a_merged_pull_request_reads_as_merged(self, monkeypatch):
    _install(monkeypatch, pull=_pull(state='closed', merged=True))
    assert pr_state.pr_state('x', 'y', 7, 't')['pull_request']['state'] == 'merged'

  def test_an_open_pull_request_keeps_its_state(self, monkeypatch):
    _install(monkeypatch)
    assert pr_state.pr_state('x', 'y', 7, 't')['pull_request']['state'] == 'open'

  def test_a_thread_reply_carries_its_root(self, monkeypatch):
    _install(
      monkeypatch,
      comments=[_comment(200, 'reviewer[bot]'), _comment(201, 'author[bot]', in_reply_to_id=200)],
    )
    comments = pr_state.pr_state('x', 'y', 7, 't')['comments']
    assert [c['in_reply_to'] for c in comments] == [None, 200]


class TestMain:
  def test_prints_the_state_as_json(self, monkeypatch, capsys):
    _install(monkeypatch)

    class _Store:
      def get_instance(self, name: str) -> str:
        return f'resolved:{name}'

    monkeypatch.setattr(pr_state.credentials, 'default_store', lambda: _Store())
    assert pr_state.main(['pr-state', 'x/y', '7']) == 0
    assert json.loads(capsys.readouterr().out)['pull_request']['number'] == 7
