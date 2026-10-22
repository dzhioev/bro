#!/usr/bin/env python
"""poll a GitHub PR for merge status, merge conflicts, check results, pushes,
new comments, and new reviews.

Comment and review events fire for the parties to the PR's review — the PR
author, the repo owner, and anyone who has posted a review on the PR — minus
the account the watch's own token acts for. Reviewing admits a login to the
parties, so the same watch serves both sides of a review: the author's watch
hears whoever starts reviewing, and a reviewer's watch hears the author's
replies — including an author that is a bot account, which is why there is no
blanket bot filter: a bot that never reviews is not a party and stays silent
anyway.

Events go to stdout as text: a headline of the event name and its `key=value`
fields, followed by indented lines for its body and its list items. The log goes
to the watch's diagnostics file when run as a watch producer, and to stderr
otherwise, so nothing but events reaches a watcher.

A push fires unless a branch of the local checkout already points at the new
head: a push from the checkout sends one of its own branch tips, while a push
from anywhere else lands on a commit no local branch holds — even once a fetch
has brought its objects in.
"""

import contextlib
import datetime
import functools
import http.client
import time
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional, TypeVar

from bro import watches
from bro.base import credentials, log
from bro.base.args import Parser
from bro.extra.github import api, pulls
from bro.workspace.git import git_out, git_run

__cli_name__ = 'poll-pr'

_WATCH_FAILED_EXIT = 2

T = TypeVar('T')


_INDENT = '  '


def _field_lines(fields: dict[str, Any], depth: int) -> list[str]:
  pad = _INDENT * depth
  lines: list[str] = []
  for key, value in fields.items():
    if value is None:
      lines.append(f'{pad}{key}: none')
    elif isinstance(value, list):
      if len(value) == 0:
        continue
      lines.append(f'{pad}{key}:')
      for item in value:
        item_lines = _field_lines(item, depth + 2)
        lines.append(f'{_INDENT * (depth + 1)}- {item_lines[0][len(_INDENT) * (depth + 2) :]}')
        lines += item_lines[1:]
    elif isinstance(value, str) and (key == 'body' or '\n' in value):
      if value.strip() == '':
        continue
      lines.append(f'{pad}{key}: |')
      lines += [f'{_INDENT * (depth + 1)}{line}'.rstrip() for line in value.splitlines()]
    elif value != '':
      lines.append(f'{pad}{key}: {value}{"s" if key.endswith("_for") else ""}')
  return lines


def render(event: dict[str, Any]) -> str:
  """the text of one event: `<kind>:` on a first line, then its fields indented
  beneath as `key: value`. A `body` or any multi-line value is a `|` block, a
  list is `-` items of fields, a missing value is `none`, an empty one is left
  out, and a `_for` field counts seconds. `pr` is left out: a watch follows one."""
  fields = {key: value for key, value in event.items() if key not in ('event', 'pr')}
  return '\n'.join([f'{event["event"]}:', *_field_lines(fields, 1)])


def _timestamp() -> str:
  return datetime.datetime.now(datetime.UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def _emit(event: dict[str, Any]) -> None:
  stamped = {'event': event['event'], 'at': _timestamp(), **event}
  print(render(stamped), flush=True)


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
  parties: set[str],
  self_login: str,
  emit: Callable[[dict[str, Any]], None],
) -> None:
  """one polling cycle over comments and reviews, emitting what it finds.

  a comment or review fires when its author is in `parties` and is not
  `self_login`. posting a review admits the author to `parties`, and reviews
  are processed before comments so a first-time reviewer's comments fire in
  the same cycle as the review that admitted them.

  mutates `seen_comment_ids` / `seen_review_ids` so subsequent cycles skip
  what was already emitted; an id is marked only once its event is out, so a
  fetch that fails partway through the cycle costs a repeated fetch rather
  than a dropped event. inline review comments are bundled into the parent
  `review` event under a `comments` key — this kills the race where the
  inline-comments endpoint lags the reviews endpoint by milliseconds, so an
  APPROVED review with an attached "ship after fix" comment would otherwise
  emit as a comment-less APPROVED on one cycle and the comment on the next.
  every `review` event carries the PR's `review_decision` beside the reviewer's
  own `state`.
  """

  def actionable(login: str) -> bool:
    return login != self_login and login in parties

  @functools.cache
  def decision() -> Optional[str]:
    # lazy: a cycle emitting no review must not pay the extra request
    return pulls.review_decision(owner, repo, pr, token)

  for r in pulls.reviews(owner, repo, pr, token):
    login = r.get('user', {}).get('login', '')
    parties.add(login)
    if r['id'] in seen_review_ids:
      continue
    # fetch inline comments AFTER seeing the review so the window in which a
    # comment isn't yet indexed is as small as possible. mark them seen even
    # if the review's author is not actionable — we don't want them to fire
    # as standalone comment events on a later cycle.
    inline = pulls.review_inline_comments(owner, repo, pr, r['id'], token)
    if actionable(login):
      emit(
        {
          'event': 'review',
          'id': r['id'],
          'user': login,
          'state': r.get('state', ''),
          'review_decision': decision(),
          'body': r.get('body', ''),
          'url': r.get('html_url', ''),
          'comments': [_bundled_comment(c) for c in inline],
        }
      )
    seen_review_ids.add(r['id'])
    seen_comment_ids.update(c['id'] for c in inline)

  for c in pulls.issue_comments(owner, repo, pr, token):
    if c['id'] in seen_comment_ids:
      continue
    if actionable(c.get('user', {}).get('login', '')):
      emit(_comment_event(c))
    seen_comment_ids.add(c['id'])

  # any inline review comments not bundled with a review fire standalone —
  # covers replies to existing review threads and comments that became
  # visible after the per-review fetch above.
  for c in pulls.review_comments(owner, repo, pr, token):
    if c['id'] in seen_comment_ids:
      continue
    if actionable(c.get('user', {}).get('login', '')):
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


class HeadTracker:
  """edge-triggered head-movement detection over the PR's head sha: `update`
  returns the sha once when it differs from the last one seen, and treats a
  missing sha as no information."""

  def __init__(self, sha: Optional[str]):
    self._sha = sha

  def update(self, sha: Optional[str]) -> Optional[str]:
    if sha is None or sha == self._sha:
      return None
    moved = self._sha is not None
    self._sha = sha
    return sha if moved else None


@dataclass(frozen=True)
class CheckTransition:
  state: Literal['red', 'green', 'pending']
  failed_runs: list[dict[str, Any]]


class CheckTracker:
  """edge-triggered check detection over each head commit.

  `update` returns a `red` transition with the failed runs when checks turn
  red, a `green` one when every run has concluded without failure — or, before
  any run reports, when GitHub's merge state holds the merge for none — a
  `pending` one when a green head stops being green without failing, and None
  while there is no new edge. A new head re-arms both edges.
  """

  def __init__(self):
    self._sha: Optional[str] = None
    self._failing = False
    self._green = False

  def update(
    self, sha: str, check_runs: list[dict[str, Any]], merge_state: str
  ) -> Optional[CheckTransition]:
    if sha != self._sha:
      self._sha = sha
      self._failing = False
      self._green = False

    states = [api.check_state(run.get('status'), run.get('conclusion')) for run in check_runs]
    failed = [run for run, state in zip(check_runs, states, strict=True) if state == 'failed']
    if len(failed) > 0:
      self._green = False
      if self._failing:
        return None
      self._failing = True
      return CheckTransition('red', failed)

    self._failing = False
    if len(check_runs) == 0 and merge_state.lower() == 'unknown':
      # GitHub has not computed the merge state yet: no edge either way
      return None
    green = 'pending' not in states and (len(check_runs) > 0 or api.expects_no_check(merge_state))
    if green == self._green:
      return None
    self._green = green
    return CheckTransition('green' if green else 'pending', [])


def _checks_event(pr: int, head: str, transition: CheckTransition) -> dict[str, Any]:
  return {
    'event': 'checks',
    'pr': pr,
    'head': head,
    'state': transition.state,
    'failing': [
      {
        'name': run.get('name', ''),
        'conclusion': run.get('conclusion', ''),
        'url': run.get('html_url') or run.get('details_url', ''),
      }
      for run in transition.failed_runs
    ],
  }


def _merged_event(
  owner: str, repo: str, pr: int, pr_data: dict[str, Any], token: str
) -> dict[str, Any]:
  """the merge's `merged_by` and `merge_commit` (the base's tip after it), and
  the `commits` the PR carried — a rebase merge rewrites them on the base. The
  commit list is best effort: a failed read leaves it out rather than the event."""
  event: dict[str, Any] = {'event': 'merged', 'pr': pr}
  merged_by = (pr_data.get('merged_by') or {}).get('login')
  for key, value in (('merged_by', merged_by), ('merge_commit', pr_data.get('merge_commit_sha'))):
    if value is not None:
      event[key] = value
  try:
    carried = pulls.pull_commits(owner, repo, pr, token)
  except _REMOTE_FAILURES as error:
    log.warning(f'merged: reading the PR commits failed: {_failure_reason(error)}')
    return event
  if len(carried) > 0:
    event['commits'] = [
      {'sha': c['sha'], 'subject': c['commit']['message'].splitlines()[0]} for c in carried
    ]
  return event


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


# what a fetch raises when GitHub is the one failing. A local error belongs to
# none of it and stays fatal, so a malformed payload surfaces where it is made
# rather than as a source the watch reports on.
_REMOTE_FAILURES = (http.client.HTTPException, OSError, api.GraphQLError)


@contextlib.contextmanager
def _watch_failure(source: str):
  """report a failed request inside the block as `WatchFailed` against `source`."""
  try:
    yield
  except _REMOTE_FAILURES as error:
    raise WatchFailed(source, _failure_reason(error), 0.0)


@dataclass(frozen=True)
class Failure:
  source: str
  reason: str
  since: float


class FailureTracker:
  """per-source failure clock over the poll cycle's fetches.

  `probe` runs one event source's fetch: on success it returns the result and
  clears that source's clock, on failure it returns None so the rest of the
  cycle still serves the other sources. Every failed attempt of the fetch's
  requests is recorded as it happens, so the clock starts when the first failed
  request began and runs through the retries inside it, and `on_failure` is
  called after each record. A source that keeps failing for longer than `grace` seconds raises
  `WatchFailed` — a source that cannot be served ends the watch rather than
  leaving its events indistinguishable from a quiet PR.

  The clock covers every failure, 404 included: GitHub returns one for minutes
  at a time on an endpoint that answered seconds earlier (observed on
  `pulls/{n}/reviews`), and a resource that is genuinely gone keeps failing
  until the window ends the watch anyway.
  """

  def __init__(self, grace: float, on_failure: Callable[[], None]):
    self._grace = grace
    self._on_failure = on_failure
    self._failing: dict[str, Failure] = {}

  def probe(self, source: str, fetch: Callable[..., T], *args: Any) -> Optional[T]:
    started = time.monotonic()
    observed: list[Exception] = []

    def observe(error: Exception) -> None:
      if isinstance(error, _REMOTE_FAILURES):
        observed.append(error)
        self._record(source, error, started)

    try:
      with api.observing_failures(observe):
        result = fetch(*args)
    except _REMOTE_FAILURES as error:
      if len(observed) == 0 or observed[-1] is not error:
        self._record(source, error, started)
      return None
    self._failing.pop(source, None)
    return result

  def longest_failing(self) -> Optional[Failure]:
    """the source that has been failing since the earliest request, if any."""
    return min(self._failing.values(), key=lambda failure: failure.since, default=None)

  def _record(self, source: str, error: Exception, started: float) -> None:
    reason = _failure_reason(error)
    since = self._failing[source].since if source in self._failing else started
    self._failing[source] = Failure(source, reason, since)
    failing_for = time.monotonic() - since
    if failing_for >= self._grace:
      raise WatchFailed(source, reason, failing_for)
    log.warning(
      f'{source}: {reason}; failing for {failing_for:.0f}s of {self._grace:.0f}s tolerated'
    )
    self._on_failure()


class ConnectionTracker:
  """edge-triggered connectivity detection over the failure clock: `update`
  returns a `disconnected` event once, when the longest failing source has been
  failing for `after` seconds, and a `reconnected` one once, when no source
  fails any more."""

  def __init__(self, pr: int, after: float):
    self._pr = pr
    self._after = after
    self._disconnected_since: Optional[float] = None

  def update(self, failing: Optional[Failure]) -> Optional[dict[str, Any]]:
    now = time.monotonic()
    if self._disconnected_since is None:
      if failing is None or now - failing.since < self._after:
        return None
      self._disconnected_since = failing.since
      return {
        'event': 'disconnected',
        'pr': self._pr,
        'source': failing.source,
        'reason': failing.reason,
        'failing_for': round(now - failing.since),
      }
    if failing is not None:
      return None
    disconnected_for = now - self._disconnected_since
    self._disconnected_since = None
    return {'event': 'reconnected', 'pr': self._pr, 'disconnected_for': round(disconnected_for)}


# what one cycle's requests may take beyond the poll interval before the watch counts as stalled
_CYCLE_SLACK_SECONDS = 60


def poll_pr(
  owner: str,
  repo: str,
  pr: int,
  token: Callable[[], str],
  interval: int,
  failure_grace: float,
  disconnect_after: float,
  local_tip: Callable[[str], bool],
) -> int:
  seen_comment_ids: set[int] = set()
  seen_review_ids: set[int] = set()
  parties: set[str] = set()
  conflicts = ConflictTracker()
  checks = CheckTracker()
  connection = ConnectionTracker(pr, disconnect_after)

  def report_connectivity() -> None:
    connectivity = connection.update(sources.longest_failing())
    if connectivity is not None:
      _emit(connectivity)

  sources = FailureTracker(failure_grace, report_connectivity)
  beat_promise = interval + _CYCLE_SLACK_SECONDS
  watches.beat(beat_promise)

  try:
    # the baseline decides what counts as pre-existing, so it is all-or-nothing:
    # a watch that starts without it would replay the whole PR as new events
    with _watch_failure('baseline'):
      startup_token = token()
      self_login = api.viewer_login(startup_token)
      log.info(f'self: {self_login}')
      pr_data = pulls.pull_request(owner, repo, pr, startup_token)
      head = HeadTracker(pr_data.get('head', {}).get('sha'))
      parties.add(pr_data['user']['login'])
      parties.add(_owner_login(owner, repo, startup_token))

      for comments in (
        pulls.issue_comments(owner, repo, pr, startup_token),
        pulls.review_comments(owner, repo, pr, startup_token),
      ):
        for c in comments:
          seen_comment_ids.add(c['id'])
      for r in pulls.reviews(owner, repo, pr, startup_token):
        seen_review_ids.add(r['id'])
        parties.add(r.get('user', {}).get('login', ''))
      log.info(
        f'existing comments: {len(seen_comment_ids)}, existing reviews: {len(seen_review_ids)}, '
        f'parties: {sorted(parties)}'
      )

    while True:
      watches.beat(beat_promise)
      # re-read per cycle so a short-lived minted credential stays fresh across
      # a watch that outlives it
      cycle_token = token()
      pr_data = sources.probe('pull-request', pulls.pull_request, owner, repo, pr, cycle_token)

      if pr_data is not None:
        if pr_data.get('merged'):
          _emit(_merged_event(owner, repo, pr, pr_data, cycle_token))
          return 0

        if pr_data.get('state') == 'closed':
          _emit({'event': 'closed', 'pr': pr})
          return 1

        if conflicts.update(pr_data.get('mergeable')):
          _emit({'event': 'conflicts', 'pr': pr})

        head_sha = pr_data.get('head', {}).get('sha')
        moved_to = head.update(head_sha)
        if moved_to is not None and not local_tip(moved_to):
          _emit({'event': 'pushed', 'pr': pr, 'head': moved_to})
        if head_sha is not None:
          check_runs = sources.probe(
            'checks', _fetch_check_runs, owner, repo, head_sha, cycle_token
          )
          if check_runs is not None:
            transition = checks.update(head_sha, check_runs, pr_data['mergeable_state'])
            if transition is not None:
              _emit(_checks_event(pr, head_sha, transition))

      sources.probe(
        'reviews',
        emit_cycle,
        owner,
        repo,
        pr,
        cycle_token,
        seen_comment_ids,
        seen_review_ids,
        parties,
        self_login,
        _emit,
      )

      report_connectivity()

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


def branch_tip_probe(checkout: Optional[Path]) -> Callable[[str], bool]:
  """a predicate telling whether a branch of the git repository at `checkout`
  points at a sha.

  `None` stands for the working directory, where no repository is a condition
  rather than an error: every sha then answers False. A named `checkout` must
  be a repository."""
  if checkout is None:
    located = git_run('rev-parse', '--absolute-git-dir')
    if located.returncode != 0:
      log.info(f'no local repository ({located.stderr.strip()}); every push fires')
      return lambda sha: False
    git_dir = located.stdout.strip()
  else:
    git_dir = git_out('rev-parse', '--absolute-git-dir', cwd=str(checkout))

  def points_at(sha: str) -> bool:
    return (
      git_out(
        '--git-dir',
        git_dir,
        'for-each-ref',
        f'--points-at={sha}',
        '--format=%(refname)',
        'refs/heads',
      )
      != ''
    )

  return points_at


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(
    description='poll a GitHub PR for merge status, merge conflicts, check results, pushes, '
    'new comments, and new reviews'
  )
  parser.add_argument(
    'repo',
    type=pulls.owner_repo,
    metavar='owner/repo',
    help='target repo (e.g. owner/repository)',
  )
  parser.add_argument('pr', type=int, help='PR number')
  parser.add_argument('--interval', type=int, default=5, help='poll interval in seconds')
  parser.add_argument(
    '--failure-grace',
    type=int,
    default=300,
    help='seconds an event source may keep failing before the watch reports it and exits',
  )
  parser.add_argument(
    '--disconnect-after',
    type=int,
    default=60,
    help='seconds an event source may keep failing before the watch reports it as disconnected',
  )
  parser.add_argument(
    '--repo',
    dest='checkout',
    type=Path,
    metavar='path',
    env=False,
    help='local checkout whose branch tips mark a push as its own and silence it '
    '(default: the working directory, where no repository means every push fires)',
  )
  namespace = parser.parse(argv)
  diagnostics = watches.diagnostics_path()
  if diagnostics is not None:
    log.to_file(diagnostics)
  owner, repo = namespace['repo']
  return poll_pr(
    owner=owner,
    repo=repo,
    pr=namespace['pr'],
    token=lambda: credentials.get('github'),
    interval=namespace['interval'],
    failure_grace=namespace['failure_grace'],
    disconnect_after=namespace['disconnect_after'],
    local_tip=branch_tip_probe(namespace['checkout']),
  )
