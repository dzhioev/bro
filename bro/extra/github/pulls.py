"""reads over a pull request and its review state."""

from typing import Any, Optional

from bro.base.args import ArgumentTypeError
from bro.extra.github import api


def owner_repo(arg: str) -> tuple[str, str]:
  parts = arg.split('/')
  if len(parts) != 2:
    raise ArgumentTypeError('repo must be in owner/repo format')
  return parts[0], parts[1]


def pull_request(owner: str, repo: str, pr: int, token: str) -> dict[str, Any]:
  url = f'https://api.github.com/repos/{owner}/{repo}/pulls/{pr}'
  return api.get(url, token)


_REVIEW_DECISION_QUERY = """
  query($owner: String!, $repo: String!, $pr: Int!) {
    repository(owner: $owner, name: $repo) {
      pullRequest(number: $pr) { reviewDecision }
    }
  }
"""


def review_decision(owner: str, repo: str, pr: int, token: str) -> Optional[str]:
  """GitHub's verdict on the base branch's review rule: `APPROVED`,
  `CHANGES_REQUESTED`, `REVIEW_REQUIRED`, or None where the base asks for no
  review.

  It answers whether a review the base grants standing to is still owed, which
  the reviews themselves do not: an approval from an account the rule does not
  count leaves the decision at `REVIEW_REQUIRED`. GraphQL because the REST
  pull-request payload carries no equivalent field.
  """
  data = api.graphql(
    _REVIEW_DECISION_QUERY,
    token,
    f'reading the review decision of {owner}/{repo}#{pr}',
    owner=owner,
    repo=repo,
    pr=pr,
  )
  return data['repository']['pullRequest']['reviewDecision']


def issue_comments(owner: str, repo: str, pr: int, token: str) -> list[dict[str, Any]]:
  url = f'https://api.github.com/repos/{owner}/{repo}/issues/{pr}/comments?per_page=100'
  return api.get_all(url, token)


def review_comments(owner: str, repo: str, pr: int, token: str) -> list[dict[str, Any]]:
  url = f'https://api.github.com/repos/{owner}/{repo}/pulls/{pr}/comments?per_page=100'
  return api.get_all(url, token)


def reviews(owner: str, repo: str, pr: int, token: str) -> list[dict[str, Any]]:
  url = f'https://api.github.com/repos/{owner}/{repo}/pulls/{pr}/reviews?per_page=100'
  return api.get_all(url, token)


def review_inline_comments(
  owner: str, repo: str, pr: int, review_id: int, token: str
) -> list[dict[str, Any]]:
  url = (
    f'https://api.github.com/repos/{owner}/{repo}/pulls/{pr}/reviews/'
    f'{review_id}/comments?per_page=100'
  )
  return api.get_all(url, token)
