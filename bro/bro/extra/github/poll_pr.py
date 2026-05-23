#!/usr/bin/env python
"""poll a GitHub PR for merge status, new comments, and new reviews."""

import json
import logging
import sys
import time
import urllib.request
from typing import Any

from base.args import Parser

__cli_name__ = 'poll-pr'

_log = logging.getLogger(__name__)


def _gh_get(url: str, token: str) -> Any:
  req = urllib.request.Request(
    url,
    headers={
      'Authorization': f'Bearer {token}',
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
    },
  )
  with urllib.request.urlopen(req) as resp:
    return json.loads(resp.read())


def _fetch_pr(owner: str, repo: str, pr: int, token: str) -> dict[str, Any]:
  url = f'https://api.github.com/repos/{owner}/{repo}/pulls/{pr}'
  return _gh_get(url, token)


def _fetch_issue_comments(owner: str, repo: str, pr: int, token: str) -> list[dict[str, Any]]:
  url = f'https://api.github.com/repos/{owner}/{repo}/issues/{pr}/comments?per_page=100'
  return _gh_get(url, token)


def _fetch_review_comments(owner: str, repo: str, pr: int, token: str) -> list[dict[str, Any]]:
  url = f'https://api.github.com/repos/{owner}/{repo}/pulls/{pr}/comments?per_page=100'
  return _gh_get(url, token)


def _fetch_reviews(owner: str, repo: str, pr: int, token: str) -> list[dict[str, Any]]:
  url = f'https://api.github.com/repos/{owner}/{repo}/pulls/{pr}/reviews?per_page=100'
  return _gh_get(url, token)


def _owner_login(owner: str, repo: str, token: str) -> str:
  url = f'https://api.github.com/repos/{owner}/{repo}'
  data: dict[str, Any] = _gh_get(url, token)
  return data['owner']['login']


def poll_pr(
  owner: str,
  repo: str,
  pr: int,
  token: str,
  interval: int,
  self_login: str | None,
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

  def _is_actionable(login: str) -> bool:
    if self_login is not None and login == self_login:
      return False
    if login.endswith('[bot]'):
      return False
    return login == repo_owner_login

  while True:
    pr_data = _fetch_pr(owner, repo, pr, token)

    if pr_data.get('merged'):
      print(json.dumps({'event': 'merged', 'pr': pr}), flush=True)
      return 0

    if pr_data.get('state') == 'closed':
      print(json.dumps({'event': 'closed', 'pr': pr}), flush=True)
      return 1

    for comments in (
      _fetch_issue_comments(owner, repo, pr, token),
      _fetch_review_comments(owner, repo, pr, token),
    ):
      for c in comments:
        if c['id'] in seen_comment_ids:
          continue
        seen_comment_ids.add(c['id'])

        login = c.get('user', {}).get('login', '')
        if not _is_actionable(login):
          continue

        print(
          json.dumps(
            {
              'event': 'comment',
              'id': c['id'],
              'user': login,
              'body': c.get('body', ''),
              'path': c.get('path'),
              'url': c.get('html_url', ''),
            }
          ),
          flush=True,
        )

    for r in _fetch_reviews(owner, repo, pr, token):
      if r['id'] in seen_review_ids:
        continue
      seen_review_ids.add(r['id'])

      login = r.get('user', {}).get('login', '')
      if not _is_actionable(login):
        continue

      print(
        json.dumps(
          {
            'event': 'review',
            'id': r['id'],
            'user': login,
            'state': r.get('state', ''),
            'body': r.get('body', ''),
            'url': r.get('html_url', ''),
          }
        ),
        flush=True,
      )

    time.sleep(interval)


def main(argv=None):
  parser = Parser(description='poll a GitHub PR for merge status, new comments, and new reviews')
  parser.add_argument('repo', help='owner/repo (e.g. dzhioev/ppp)')
  parser.add_argument('pr', type=int, help='PR number')
  parser.add_argument('--token', required=True, secret=True, help='GitHub token')
  parser.add_argument('--interval', type=int, default=10, help='poll interval in seconds')
  parser.add_argument('--self', dest='self_login', help='login to filter out (your own comments)')
  ns = parser.parse(argv)
  parts = ns['repo'].split('/')
  if len(parts) != 2:
    print('repo must be owner/repo format', file=sys.stderr)
    return 2
  owner, repo = parts
  return poll_pr(
    owner=owner,
    repo=repo,
    pr=ns['pr'],
    token=ns['token'],
    interval=ns['interval'],
    self_login=ns['self_login'],
  )


if __name__ == '__main__':
  sys.exit(main(sys.argv))
