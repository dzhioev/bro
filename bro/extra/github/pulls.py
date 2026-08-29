"""reads over a pull request and its review state."""

from typing import Any

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


def issue_comments(owner: str, repo: str, pr: int, token: str) -> list[dict[str, Any]]:
  url = f'https://api.github.com/repos/{owner}/{repo}/issues/{pr}/comments?per_page=100'
  return api.get(url, token)


def review_comments(owner: str, repo: str, pr: int, token: str) -> list[dict[str, Any]]:
  url = f'https://api.github.com/repos/{owner}/{repo}/pulls/{pr}/comments?per_page=100'
  return api.get(url, token)


def reviews(owner: str, repo: str, pr: int, token: str) -> list[dict[str, Any]]:
  url = f'https://api.github.com/repos/{owner}/{repo}/pulls/{pr}/reviews?per_page=100'
  return api.get(url, token)


def review_inline_comments(
  owner: str, repo: str, pr: int, review_id: int, token: str
) -> list[dict[str, Any]]:
  url = (
    f'https://api.github.com/repos/{owner}/{repo}/pulls/{pr}/reviews/'
    f'{review_id}/comments?per_page=100'
  )
  return api.get(url, token)
