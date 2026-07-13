#!/usr/bin/env python
"""poll a GitHub PR for merge status, new comments, and new reviews."""

import http.client
import json
import logging
import time
import urllib.error
from collections.abc import Callable
from typing import Any, Optional

from base.args import ArgumentTypeError, Parser
from github import api

__cli_name__ = 'poll-pr'

_log = logging.getLogger(__name__)


def _fetch_pr(owner: str, repo: str, pr: int, token: str) -> dict[str, Any]:
  url = f'https://api.github.com/repos/{owner}/{repo}/pulls/{pr}'
  return api.get(url, token)


def _fetch_issue_comments(owner: str, repo: str, pr: int, token: str) -> list[dict[str, Any]]:
  url = f'https://api.github.com/repos/{owner}/{repo}/issues/{pr}/comments?per_page=100'
  return api.get(url, token)


def _fetch_review_comments(owner: str, repo: str, pr: int, token: str) -> list[dict[str, Any]]:
  url = f'https://api.github.com/repos/{owner}/{repo}/pulls/{pr}/comments?per_page=100'
  return api.get(url, token)


def _fetch_reviews(owner: str, repo: str, pr: int, token: str) -> list[dict[str, Any]]:
  url = f'https://api.github.com/repos/{owner}/{repo}/pulls/{pr}/reviews?per_page=100'
  return api.get(url, token)


def _fetch_review_inline_comments(
  owner: str, repo: str, pr: int, review_id: int, token: str
) -> list[dict[str, Any]]:
  url = (
    f'https://api.github.com/repos/{owner}/{repo}/pulls/{pr}/reviews/'
    f'{review_id}/comments?per_page=100'
  )
  return api.get(url, token)


def _owner_login(owner: str, repo: str, token: str) -> str:
  url = f'https://api.github.com/repos/{owner}/{repo}'
  data: dict[str, Any] = api.get(url, token)
  return data['owner']['login']


def _comment_event(comment: dict[str, Any]) -> dict[str, Any]:
  return {
    'event': 'comment',
    'id': comment['id'],
    'user': comment.get('user', {}).get('login', ''),
    'body': comment.get('body', ''),
    'path': comment.get('path'),
    'url': comment.get('html_url', ''),
  }


def _bundled_comment(comment: dict[str, Any]) -> dict[str, Any]:
  return {
    'id': comment['id'],
    'path': comment.get('path'),
    'line': comment.get('line'),
    'body': comment.get('body', ''),
    'url': comment.get('html_url', ''),
  }


def emit_cycle(
  owner: str,
  repo: str,
  pr: int,
  token: str,
  seen_comment_ids: set[int],
  seen_review_ids: set[int],
  is_actionable: Callable[[str], bool],
) -> list[dict[str, Any]]:
  """one polling cycle: fetch comments + reviews, return events to emit.

  mutates `seen_comment_ids` / `seen_review_ids` so subsequent cycles skip
  what was already emitted. inline review comments are bundled into the
  parent `review` event under a `comments` key — this kills the race where
  the inline-comments endpoint lags the reviews endpoint by milliseconds, so
  an APPROVED review with an attached "ship after fix" comment would
  otherwise emit as a comment-less APPROVED on one cycle and the comment on
  the next.
  """
  events: list[dict[str, Any]] = []

  for c in _fetch_issue_comments(owner, repo, pr, token):
    if c['id'] in seen_comment_ids:
      continue
    seen_comment_ids.add(c['id'])
    if not is_actionable(c.get('user', {}).get('login', '')):
      continue
    events.append(_comment_event(c))

  for r in _fetch_reviews(owner, repo, pr, token):
    if r['id'] in seen_review_ids:
      continue
    seen_review_ids.add(r['id'])
    login = r.get('user', {}).get('login', '')
    # fetch inline comments AFTER seeing the review so the window in which a
    # comment isn't yet indexed is as small as possible. mark them seen even
    # if the review's author is not actionable — we don't want them to fire
    # as standalone comment events on a later cycle.
    bundled: list[dict[str, Any]] = []
    for c in _fetch_review_inline_comments(owner, repo, pr, r['id'], token):
      seen_comment_ids.add(c['id'])
      bundled.append(_bundled_comment(c))
    if not is_actionable(login):
      continue
    events.append(
      {
        'event': 'review',
        'id': r['id'],
        'user': login,
        'state': r.get('state', ''),
        'body': r.get('body', ''),
        'url': r.get('html_url', ''),
        'comments': bundled,
      }
    )

  # any inline review comments not bundled with a review fire standalone —
  # covers replies to existing review threads and comments that became
  # visible after the per-review fetch above.
  for c in _fetch_review_comments(owner, repo, pr, token):
    if c['id'] in seen_comment_ids:
      continue
    seen_comment_ids.add(c['id'])
    if not is_actionable(c.get('user', {}).get('login', '')):
      continue
    events.append(_comment_event(c))

  return events


def poll_pr(
  owner: str,
  repo: str,
  pr: int,
  token: str,
  interval: int,
  self_login: Optional[str],
) -> int:
  seen_comment_ids: set[int] = set()
  seen_review_ids: set[int] = set()

  repo_owner_login = _owner_login(owner, repo, token)
  _log.info(f'repo owner: {repo_owner_login}')

  for comments in (
    _fetch_issue_comments(owner, repo, pr, token),
    _fetch_review_comments(owner, repo, pr, token),
  ):
    for c in comments:
      seen_comment_ids.add(c['id'])
  for r in _fetch_reviews(owner, repo, pr, token):
    seen_review_ids.add(r['id'])
  _log.info(f'existing comments: {len(seen_comment_ids)}, existing reviews: {len(seen_review_ids)}')

  def is_actionable(login: str) -> bool:
    if self_login is not None and login == self_login:
      return False
    if login.endswith('[bot]'):
      return False
    return login == repo_owner_login

  while True:
    # `github.api` already retries transient blips per call; this guard is the
    # second layer — if a whole cycle still fails on a transient error (a longer
    # outage), log and poll again next interval instead of exiting, preserving
    # the seen-id baselines. a non-transient error (404 — PR/repo gone) is fatal
    # and propagates.
    try:
      pr_data = _fetch_pr(owner, repo, pr, token)

      if pr_data.get('merged'):
        print(json.dumps({'event': 'merged', 'pr': pr}), flush=True)
        return 0

      if pr_data.get('state') == 'closed':
        print(json.dumps({'event': 'closed', 'pr': pr}), flush=True)
        return 1

      for event in emit_cycle(
        owner, repo, pr, token, seen_comment_ids, seen_review_ids, is_actionable
      ):
        print(json.dumps(event), flush=True)
    except (http.client.HTTPException, OSError) as error:
      if isinstance(error, urllib.error.URLError) and not api.is_transient(error):
        raise
      if isinstance(error, urllib.error.HTTPError):
        reason = f'HTTP {error.code}'
      elif isinstance(error, urllib.error.URLError):
        reason = str(error.reason)
      else:
        reason = f'{type(error).__name__}: {error}'
      _log.warning(f'{reason} during poll cycle; continuing after {interval}s')

    time.sleep(interval)


def _owner_repo(arg: str) -> tuple[str, str]:
  parts = arg.split('/')
  if len(parts) != 2:
    raise ArgumentTypeError('repo must be in owner/repo format')
  return parts[0], parts[1]


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description='poll a GitHub PR for merge status, new comments, and new reviews')
  parser.add_argument(
    'repo', type=_owner_repo, metavar='owner/repo', help='target repo (e.g. dzhioev/ppp)'
  )
  parser.add_argument('pr', type=int, help='PR number')
  parser.add_argument('--token', required=True, secret=True, help='GitHub token')
  parser.add_argument('--interval', type=int, default=10, help='poll interval in seconds')
  parser.add_argument('--self', dest='self_login', help='login to filter out (your own comments)')
  namespace = parser.parse(argv)
  owner, repo = namespace['repo']
  return poll_pr(
    owner=owner,
    repo=repo,
    pr=namespace['pr'],
    token=namespace['token'],
    interval=namespace['interval'],
    self_login=namespace['self_login'],
  )
