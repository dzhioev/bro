#!/usr/bin/env python
import json
from pathlib import Path
from typing import Any

from bro.base import credentials
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
    'user': {'login': login, 'type': 'User'},
    'author_association': 'OWNER',
    'state': 'CHANGES_REQUESTED',
    'commit_id': 'c0ffee1234567890',
    'body': 'one blocker',
    'submitted_at': '2026-08-29T10:00:00Z',
    'html_url': 'https://github.com/x/y/pull/7#pullrequestreview-100',
  }


def _comment(id: int, login: str, **extra: Any) -> dict[str, Any]:
  return {
    'id': id,
    'user': {'login': login, 'type': 'User'},
    'author_association': 'OWNER',
    'created_at': '2026-11-01T10:00:00Z',
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
  monkeypatch.setattr(pr_state.pulls, 'issue_comments', lambda *a: [])


class TestPrState:
  def test_logins_come_from_one_surface(self, monkeypatch):
    _install(
      monkeypatch,
      reviews=[_review('reviewer[bot]')],
      comments=[_comment(200, 'reviewer[bot]'), _comment(201, 'author[bot]')],
    )
    state = pr_state.pr_state('x', 'y', 7, 't', Path('/dev/null'))
    assert state['viewer'] == 'reviewer[bot]'
    assert state['pull_request']['author'] == 'author[bot]'
    assert [r['user'] for r in state['reviews']] == ['reviewer[bot]']
    assert [r['commit_id'] for r in state['reviews']] == ['c0ffee1234567890']
    assert [c['user'] for c in state['comments']] == ['reviewer[bot]', 'author[bot]']

  def test_a_merged_pull_request_reads_as_merged(self, monkeypatch):
    _install(monkeypatch, pull=_pull(state='closed', merged=True))
    assert (
      pr_state.pr_state('x', 'y', 7, 't', Path('/dev/null'))['pull_request']['state'] == 'merged'
    )

  def test_an_open_pull_request_keeps_its_state(self, monkeypatch):
    _install(monkeypatch)
    assert pr_state.pr_state('x', 'y', 7, 't', Path('/dev/null'))['pull_request']['state'] == 'open'

  def test_a_thread_reply_carries_its_root(self, monkeypatch):
    _install(
      monkeypatch,
      comments=[_comment(200, 'reviewer[bot]'), _comment(201, 'author[bot]', in_reply_to_id=200)],
    )
    comments = pr_state.pr_state('x', 'y', 7, 't', Path('/dev/null'))['comments']
    assert [c['in_reply_to'] for c in comments] == [None, 200]


def _picked_github_store(store_dir: Path) -> credentials.Store:
  (store_dir / 'creds').mkdir()
  (store_dir / 'creds' / 'github+reviewer.cred').write_text('reviewer-token')
  (store_dir / 'creds.json').write_text(json.dumps({'defaults': ['github+reviewer']}))
  registry = {'github': credentials.CredentialKind('github', 'GitHub access')}
  return credentials.Store(registry, store_dir, {})


class TestMain:
  def test_prints_the_state_as_json(self, monkeypatch, capsys, tmp_path):
    _install(monkeypatch)
    with credentials.as_default_store(_picked_github_store(tmp_path)):
      assert pr_state.main(['pr-state', 'x/y', '7', '--trust-file', '/dev/null']) == 0
    assert json.loads(capsys.readouterr().out)['pull_request']['number'] == 7

  def test_the_token_is_the_picked_github_instance(self, monkeypatch, capsys, tmp_path):
    _install(monkeypatch)
    monkeypatch.setattr(pr_state.api, 'viewer_login', lambda token: f'acts-for:{token}')
    with credentials.as_default_store(_picked_github_store(tmp_path)):
      assert pr_state.main(['pr-state', 'x/y', '7', '--trust-file', '/dev/null']) == 0
    assert json.loads(capsys.readouterr().out)['viewer'] == 'acts-for:reviewer-token'


class TestTrustFiltering:
  def test_untrusted_reviews_and_both_comment_kinds_have_no_text_or_metadata(
    self, monkeypatch, tmp_path
  ):
    review = {**_review('stranger'), 'author_association': 'CONTRIBUTOR'}
    inline = {**_comment(200, 'stranger'), 'author_association': 'NONE'}
    conversation = {
      **_comment(201, 'stranger'),
      'author_association': 'NONE',
      'body': 'untrusted conversation',
    }
    conversation.pop('path')
    _install(monkeypatch, reviews=[review], comments=[inline])
    monkeypatch.setattr(pr_state.pulls, 'issue_comments', lambda *args: [conversation])
    calls = []

    def get(url, token, **kwargs):
      calls.append(url)
      return {'permission': 'read'}

    monkeypatch.setattr(pr_state.api, 'get', get)
    path = tmp_path / 'trust'
    path.write_text('')
    state = pr_state.pr_state('x', 'y', 7, 't', path)
    assert state['reviews'] == [{'user': 'stranger', 'kind': 'review', 'url': review['html_url']}]
    assert state['comments'] == [
      {'user': 'stranger', 'kind': 'comment', 'url': conversation['html_url']},
      {'user': 'stranger', 'kind': 'comment', 'url': inline['html_url']},
    ]
    assert len(calls) == 1
    path.write_text('stranger\n')
    trusted = pr_state.pr_state('x', 'y', 7, 't', path)
    assert trusted['reviews'][0]['body'] == review['body']
    assert trusted['reviews'][0]['user_type'] == 'User'
    assert trusted['reviews'][0]['author_association'] == 'CONTRIBUTOR'
    assert trusted['reviews'][0]['state'] == review['state']
    assert trusted['reviews'][0]['commit_id'] == review['commit_id']
    assert [comment['body'] for comment in trusted['comments']] == [
      conversation['body'],
      inline['body'],
    ]
    assert len(calls) == 1

  def test_write_access_and_owner_use_the_same_rules_as_the_watch(self, monkeypatch):
    review = {**_review('writer'), 'author_association': 'NONE'}
    owner = _comment(200, 'owner')
    _install(monkeypatch, reviews=[review], comments=[owner])
    calls = []

    def get(url, token, **kwargs):
      calls.append(url)
      return {'permission': 'maintain'}

    monkeypatch.setattr(pr_state.api, 'get', get)
    state = pr_state.pr_state('x', 'y', 7, 't', Path('/dev/null'))
    assert state['reviews'][0]['body'] == review['body']
    assert state['comments'][0]['body'] == owner['body']
    assert calls == ['https://api.github.com/repos/x/y/collaborators/writer/permission']

  def test_the_cli_reads_the_named_trust_file(self, monkeypatch, capsys, tmp_path):
    _install(monkeypatch, reviews=[{**_review('reviewer'), 'author_association': 'NONE'}])
    path = tmp_path / 'trust'
    path.write_text('reviewer\n')
    with credentials.as_default_store(_picked_github_store(tmp_path)):
      assert pr_state.main(['pr-state', 'x/y', '7', '--trust-file', str(path)]) == 0
    assert json.loads(capsys.readouterr().out)['reviews'][0]['body'] == 'one blocker'
