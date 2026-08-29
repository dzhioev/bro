#!/usr/bin/env python
"""a pull request's review state as one JSON object.

`viewer` is the account the token acts for; it and every other login in the
output are read off the same REST payloads, so any two of them compare
directly.
"""

import json
from typing import Any, Optional

from bro.base import credentials
from bro.base.args import Parser
from bro.extra.github import api, pulls

__cli_name__ = 'pr-state'


def _review(review: dict[str, Any]) -> dict[str, Any]:
  return {
    'id': review['id'],
    'user': review['user']['login'],
    'state': review['state'],
    'body': review['body'],
    'submitted_at': review.get('submitted_at'),
    'url': review['html_url'],
  }


def _comment(comment: dict[str, Any]) -> dict[str, Any]:
  return {
    'id': comment['id'],
    # GitHub omits the key on a thread's root comment
    'in_reply_to': comment.get('in_reply_to_id'),
    'user': comment['user']['login'],
    'path': comment['path'],
    'line': comment.get('line'),
    'body': comment['body'],
    'url': comment['html_url'],
  }


def pr_state(owner: str, repo: str, pr: int, token: str) -> dict[str, Any]:
  pull = pulls.pull_request(owner, repo, pr, token)
  return {
    'viewer': api.viewer_login(token),
    'pull_request': {
      'number': pull['number'],
      'url': pull['html_url'],
      'state': 'merged' if pull['merged'] else pull['state'],
      'draft': pull['draft'],
      'title': pull['title'],
      'body': pull['body'],
      'author': pull['user']['login'],
      'base': pull['base']['ref'],
      'head': pull['head']['ref'],
      'head_sha': pull['head']['sha'],
    },
    'reviews': [_review(r) for r in pulls.reviews(owner, repo, pr, token)],
    'comments': [_comment(c) for c in pulls.review_comments(owner, repo, pr, token)],
  }


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(
    description="a pull request's review state as one JSON object: the account the token "
    'acts for, the PR, its reviews, and its inline review comments'
  )
  parser.add_argument(
    'repo',
    type=pulls.owner_repo,
    metavar='owner/repo',
    help='target repo (e.g. owner/repository)',
  )
  parser.add_argument('pr', type=int, help='PR number')
  parser.add_argument(
    '--credential', default='github', help='credential-store secret resolved into the API token'
  )
  namespace = parser.parse(argv)
  owner, repo = namespace['repo']
  token = credentials.default_store().get_instance(namespace['credential'])
  print(json.dumps(pr_state(owner, repo, namespace['pr'], token), indent=2))
  return 0
