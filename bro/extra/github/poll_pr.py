#!/usr/bin/env python
"""poll a GitHub PR for merge status, merge conflicts, failing checks, new comments, and new reviews."""

import contextlib
import http.client
import json
import time
import urllib.error
from collections.abc import Callable
from typing import Any, Optional, TypeVar

from bro.base import credentials, log
from bro.base.args import ArgumentTypeError, Parser
from bro.extra.github import api

__cli_name__ = 'poll-pr'

_WATCH_FAILED_EXIT = 2

T = TypeVar('T')


def _emit(event: dict[str, Any]) -> None:
  print(json.dumps(event), flush=True)


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


def _fetch_check_runs(owner: str, repo: str, sha: str, token: str) -> list[dict[str, Any]]:
  url = f'https://api.github.com/repos/{owner}/{repo}/commits/{sha}/check-runs?per_page=100'
  data: dict[str, Any] = api.get(url, token)
  return data.get('check_runs', [])


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
  emit: Callable[[dict[str, Any]], None],
) -> None:
  """one polling cycle over comments and reviews, emitting what it finds.

  mutates `seen_comment_ids` / `seen_review_ids` so subsequent cycles skip
  what was already emitted; an id is marked only once its event is out, so a
  fetch that fails partway through the cycle costs a repeated fetch rather
  than a dropped event. inline review comments are bundled into the parent
  `review` event under a `comments` key — this kills the race where the
  inline-comments endpoint lags the reviews endpoint by milliseconds, so an
  APPROVED review with an attached "ship after fix" comment would otherwise
  emit as a comment-less APPROVED on one cycle and the comment on the next.
  """
  for c in _fetch_issue_comments(owner, repo, pr, token):
    if c['id'] in seen_comment_ids:
      continue
    if is_actionable(c.get('user', {}).get('login', '')):
      emit(_comment_event(c))
    seen_comment_ids.add(c['id'])

  for r in _fetch_reviews(owner, repo, pr, token):
    if r['id'] in seen_review_ids:
      continue
    login = r.get('user', {}).get('login', '')
    # fetch inline comments AFTER seeing the review so the window in which a
    # comment isn't yet indexed is as small as possible. mark them seen even
    # if the review's author is not actionable — we don't want them to fire
    # as standalone comment events on a later cycle.
    inline = _fetch_review_inline_comments(owner, repo, pr, r['id'], token)
    if is_actionable(login):
      emit(
        {
          'event': 'review',
          'id': r['id'],
          'user': login,
          'state': r.get('state', ''),
          'body': r.get('body', ''),
          'url': r.get('html_url', ''),
          'comments': [_bundled_comment(c) for c in inline],
        }
      )
    seen_review_ids.add(r['id'])
    seen_comment_ids.update(c['id'] for c in inline)

  # any inline review comments not bundled with a review fire standalone —
  # covers replies to existing review threads and comments that became
  # visible after the per-review fetch above.
  for c in _fetch_review_comments(owner, repo, pr, token):
    if c['id'] in seen_comment_ids:
      continue
    if is_actionable(c.get('user', {}).get('login', '')):
      emit(_comment_event(c))
    seen_comment_ids.add(c['id'])


class ConflictTracker:
  """edge-triggered merge-conflict detection over the PR's `mergeable` field:
  `update` returns True once when the PR turns conflicted (False ⇔ GitHub cannot
  create the merge commit), re-arms when it turns mergeable again, and treats
  None — GitHub still computing — as no information."""

  def __init__(self):
    self._conflicted = False

  def update(self, mergeable: Optional[bool]) -> bool:
    if mergeable is False and not self._conflicted:
      self._conflicted = True
      return True
    if mergeable is True:
      self._conflicted = False
    return False


class CheckTracker:
  """edge-triggered failing-check detection: `update` returns the failed runs
  once when the head commit's checks turn red, stays quiet while they stay red,
  and re-arms once nothing is failing — a re-run or a new push that goes green.
  Runs still in progress are no information; only concluded failures fire."""

  def __init__(self):
    self._failing = False

  def update(self, check_runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    failed = [
      run
      for run in check_runs
      if api.check_state(run.get('status'), run.get('conclusion')) == 'failed'
    ]
    if len(failed) == 0:
      self._failing = False
      return []
    if self._failing:
      return []
    self._failing = True
    return failed


def _checks_event(pr: int, failed: list[dict[str, Any]]) -> dict[str, Any]:
  return {
    'event': 'checks',
    'pr': pr,
    'failing': [
      {
        'name': run.get('name', ''),
        'conclusion': run.get('conclusion', ''),
        'url': run.get('html_url') or run.get('details_url', ''),
      }
      for run in failed
    ],
  }


class WatchFailed(Exception):
  """an event source the watch was asked to serve stopped working."""

  def __init__(self, source: str, reason: str, failing_for: float):
    super().__init__(f'{source}: {reason}, failing for {failing_for:.0f}s')
    self.source = source
    self.reason = reason
    self.failing_for = failing_for


def _failure_reason(error: Exception) -> str:
  if isinstance(error, urllib.error.HTTPError):
    return f'HTTP {error.code}'
  if isinstance(error, urllib.error.URLError):
    return str(error.reason)
  return f'{type(error).__name__}: {error}'


@contextlib.contextmanager
def _watch_failure(source: str):
  """report a failed request inside the block as `WatchFailed` against `source`."""
  try:
    yield
  except (http.client.HTTPException, OSError) as error:
    raise WatchFailed(source, _failure_reason(error), 0.0)


class FailureTracker:
  """per-source failure clock over the poll cycle's fetches.

  `probe` runs one event source's fetch: on success it returns the result and
  clears that source's clock, on failure it returns None so the rest of the
  cycle still serves the other sources. A source that keeps failing for longer
  than `grace` seconds raises `WatchFailed` — a source that cannot be served
  ends the watch rather than leaving its events indistinguishable from a quiet
  PR.

  The clock covers every failure, 404 included: GitHub returns one for minutes
  at a time on an endpoint that answered seconds earlier (observed on
  `pulls/{n}/reviews`), and a resource that is genuinely gone keeps failing
  until the window ends the watch anyway.
  """

  def __init__(self, grace: float):
    self._grace = grace
    self._failing_since: dict[str, float] = {}

  def probe(self, source: str, fetch: Callable[..., T], *args: Any) -> Optional[T]:
    try:
      result = fetch(*args)
    except (http.client.HTTPException, OSError) as error:
      self._record(source, error)
      return None
    self._failing_since.pop(source, None)
    return result

  def _record(self, source: str, error: Exception) -> None:
    reason = _failure_reason(error)
    now = time.monotonic()
    failing_for = now - self._failing_since.setdefault(source, now)
    if failing_for >= self._grace:
      raise WatchFailed(source, reason, failing_for)
    log.warning(
      f'{source}: {reason}; failing for {failing_for:.0f}s of {self._grace:.0f}s tolerated'
    )


def poll_pr(
  owner: str,
  repo: str,
  pr: int,
  token: Callable[[], str],
  interval: int,
  self_login: Optional[str],
  failure_grace: float,
) -> int:
  seen_comment_ids: set[int] = set()
  seen_review_ids: set[int] = set()
  conflicts = ConflictTracker()
  checks = CheckTracker()
  sources = FailureTracker(failure_grace)

  try:
    # the baseline decides what counts as pre-existing, so it is all-or-nothing:
    # a watch that starts without it would replay the whole PR as new events
    with _watch_failure('baseline'):
      startup_token = token()
      repo_owner_login = _owner_login(owner, repo, startup_token)
      log.info(f'repo owner: {repo_owner_login}')

      for comments in (
        _fetch_issue_comments(owner, repo, pr, startup_token),
        _fetch_review_comments(owner, repo, pr, startup_token),
      ):
        for c in comments:
          seen_comment_ids.add(c['id'])
      for r in _fetch_reviews(owner, repo, pr, startup_token):
        seen_review_ids.add(r['id'])
      log.info(
        f'existing comments: {len(seen_comment_ids)}, existing reviews: {len(seen_review_ids)}'
      )

    def is_actionable(login: str) -> bool:
      if self_login is not None and login == self_login:
        return False
      if login.endswith('[bot]'):
        return False
      return login == repo_owner_login

    while True:
      # re-read per cycle so a short-lived minted credential stays fresh across
      # a watch that outlives it
      cycle_token = token()
      pr_data = sources.probe('pull-request', _fetch_pr, owner, repo, pr, cycle_token)

      if pr_data is not None:
        if pr_data.get('merged'):
          _emit({'event': 'merged', 'pr': pr})
          return 0

        if pr_data.get('state') == 'closed':
          _emit({'event': 'closed', 'pr': pr})
          return 1

        # derived here rather than at startup so the derivation rides the loop's
        # transient-error tolerance
        if self_login is None:
          self_login = pr_data['user']['login']
          log.info(f'self: {self_login} (the PR author)')

        if conflicts.update(pr_data.get('mergeable')):
          _emit({'event': 'conflicts', 'pr': pr})

        head_sha = pr_data.get('head', {}).get('sha')
        if head_sha is not None:
          check_runs = sources.probe(
            'checks', _fetch_check_runs, owner, repo, head_sha, cycle_token
          )
          if check_runs is not None:
            failed = checks.update(check_runs)
            if len(failed) > 0:
              _emit(_checks_event(pr, failed))

      sources.probe(
        'reviews',
        emit_cycle,
        owner,
        repo,
        pr,
        cycle_token,
        seen_comment_ids,
        seen_review_ids,
        is_actionable,
        _emit,
      )

      time.sleep(interval)
  except WatchFailed as failure:
    log.error(f'{failure}; ending the watch')
    _emit(
      {
        'event': 'watch_failed',
        'pr': pr,
        'source': failure.source,
        'reason': failure.reason,
        'failing_for': round(failure.failing_for),
      }
    )
    return _WATCH_FAILED_EXIT


def _owner_repo(arg: str) -> tuple[str, str]:
  parts = arg.split('/')
  if len(parts) != 2:
    raise ArgumentTypeError('repo must be in owner/repo format')
  return parts[0], parts[1]


def _token_provider(credential: str) -> Callable[[], str]:
  # storage-addressed: the flag may name a kind or a kind+instance variant
  return lambda: credentials.default_store().get_instance(credential)


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(
    description='poll a GitHub PR for merge status, merge conflicts, failing checks, '
    'new comments, and new reviews'
  )
  parser.add_argument(
    'repo', type=_owner_repo, metavar='owner/repo', help='target repo (e.g. owner/repository)'
  )
  parser.add_argument('pr', type=int, help='PR number')
  parser.add_argument(
    '--credential',
    default='github',
    help='credential-store secret resolved into the token at every poll cycle '
    '(fresh across short-lived minted tokens)',
  )
  parser.add_argument('--interval', type=int, default=10, help='poll interval in seconds')
  parser.add_argument(
    '--failure-grace',
    type=int,
    default=300,
    help='seconds an event source may keep failing before the watch reports it and exits',
  )
  parser.add_argument(
    '--self',
    dest='self_login',
    help='login to filter out (your own comments); defaults to the PR author',
  )
  namespace = parser.parse(argv)
  owner, repo = namespace['repo']
  return poll_pr(
    owner=owner,
    repo=repo,
    pr=namespace['pr'],
    token=_token_provider(namespace['credential']),
    interval=namespace['interval'],
    self_login=namespace['self_login'],
    failure_grace=namespace['failure_grace'],
  )
