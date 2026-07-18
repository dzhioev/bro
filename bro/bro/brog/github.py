"""GitHub Issues backend of the brog System: brog ops mapped onto the REST API.

One issue per task; labels are tags; there are no projects. The comment stream is
the issue's native comments, and the comment metadata is native too — the author is
the comment's login (the configured token's account is the acting identity), the
timestamp its creation time; only the topic needs a place, written as a leading
`### <topic>` heading in the body. Pull requests share the issues numbering and
listings; every op rejects a ref resolving to one.
"""

import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Optional

import brog.system
from brog.model import Comment, Status, Task
from github import api

_PAGE_SIZE = 100

# write side of the status mapping: the PATCH payload each brog status selects.
# the read side (_status_from_issue) folds every closed flavor into done/dropped.
_STATUS_TO_PATCH: dict[Status, dict[str, str]] = {
  'open': {'state': 'open'},
  'done': {'state': 'closed', 'state_reason': 'completed'},
  'dropped': {'state': 'closed', 'state_reason': 'not_planned'},
}

# closed state_reasons that mean won't-happen; every other closed issue
# (completed, legacy null) reads as done
_DROPPED_STATE_REASONS = ('not_planned', 'duplicate')

# the shape add_comment writes the topic in: the body's leading heading line
_TOPIC_HEADING_RE = re.compile(r'### (?P<topic>.+)')

_ORIGIN_URL_RE = re.compile(
  r'(?:git@github\.com:|(?:https|ssh)://(?:[^@/]+@)?github\.com/)'
  r'(?P<repo>[^/]+/[^/]+?)(?:\.git)?/?'
)


def origin_repo() -> str:
  """the `owner/name` of the working directory's `origin` remote"""
  result = subprocess.run(['git', 'remote', 'get-url', 'origin'], capture_output=True, text=True)
  if result.returncode != 0:
    raise ValueError('the workspace has no origin remote to derive the GitHub repo from')
  url = result.stdout.strip()
  match = _ORIGIN_URL_RE.fullmatch(url)
  if match is None:
    raise ValueError(f'cannot derive owner/name from the origin remote {url!r}')
  return match.group('repo')


def _status_from_issue(issue: dict[str, Any]) -> Status:
  if issue['state'] == 'open':
    return 'open'
  if issue.get('state_reason') in _DROPPED_STATE_REASONS:
    return 'dropped'
  return 'done'


def _to_comment(native: dict[str, Any]) -> Comment:
  """one native comment as a stream entry: author and timestamp are the comment's own
  login and creation time; the topic is the body's leading `### <topic>` heading, and
  a headingless comment (not written through brog) reads whole with no topic"""
  body = native['body'] if native['body'] is not None else ''
  first_line, _, rest = body.partition('\n')
  heading = _TOPIC_HEADING_RE.fullmatch(first_line.rstrip())
  return Comment(
    topic=heading.group('topic') if heading is not None else None,
    author=native['user']['login'],
    timestamp=datetime.fromisoformat(native['created_at']).astimezone(UTC),
    body=rest.strip() if heading is not None else body,
  )


class System(brog.system.System):
  """brog ops over the GitHub Issues REST API of one `owner/name` repo

  The token's account is the acting identity: issues and comments are created
  under it, so comments carry no embedded author segment (authorship is native).
  `token` is a provider consulted per API call, so a short-lived minted credential
  (a GitHub App installation token) stays fresh for the System's whole lifetime.
  """

  def __init__(self, *, token: Callable[[], str], repo: str):
    if re.fullmatch(r'[^/\s]+/[^/\s]+', repo) is None:
      raise ValueError(f'GitHub repo must be owner/name, got {repo!r}')
    self._token = token
    self._repo = repo

  def _url(self, path: str) -> str:
    return f'https://api.github.com/repos/{self._repo}{path}'

  def _issue_number(self, ref: str) -> int:
    plain = re.fullmatch(r'#?(\d+)', ref)
    if plain is not None:
      return int(plain.group(1))
    url = re.fullmatch(r'https://github\.com/([^/]+/[^/]+)/issues/(\d+)', ref)
    if url is not None:
      if url.group(1) != self._repo:
        raise ValueError(f'issue URL {ref!r} is not in the configured repo {self._repo!r}')
      return int(url.group(2))
    raise ValueError(
      f'unrecognized GitHub issue ref {ref!r}; accepted: issue number, #N, or issue URL'
    )

  def _issue(self, ref: str) -> dict[str, Any]:
    """resolve a task ref to its issue object, rejecting pull requests

    every op starts here — the issues API happily serves and mutates PRs, which
    brog does not track.
    """
    issue = api.get(self._url(f'/issues/{self._issue_number(ref)}'), self._token())
    if 'pull_request' in issue:
      raise ValueError(f'#{issue["number"]} is a pull request, not an issue')
    return issue

  def _blocked_by(self, issue: dict[str, Any]) -> list[str]:
    if issue['issue_dependencies_summary']['total_blocked_by'] == 0:
      return []
    blockers = api.get(
      self._url(f'/issues/{issue["number"]}/dependencies/blocked_by?per_page={_PAGE_SIZE}'),
      self._token(),
    )
    return [str(blocker['number']) for blocker in blockers if blocker['state'] == 'open']

  def _to_task(self, issue: dict[str, Any]) -> Task:
    return Task(
      id=str(issue['number']),
      name=issue['title'],
      status=_status_from_issue(issue),
      url=issue['html_url'],
      tags=[label['name'] for label in issue['labels']],
      project=None,
      blocked_by=self._blocked_by(issue),
    )

  def create_task(
    self, *, name: str, body: Optional[str] = None, tags: Optional[list[str]] = None
  ) -> Task:
    payload: dict[str, Any] = {'title': name}
    if body is not None:
      payload['body'] = body
    if tags is not None:
      payload['labels'] = tags
    issue = api.post(self._url('/issues'), self._token(), payload)
    # a just-created issue is open with no dependencies; no follow-up reads needed
    return Task(
      id=str(issue['number']),
      name=issue['title'],
      status='open',
      url=issue['html_url'],
      tags=[label['name'] for label in issue['labels']],
      project=None,
      blocked_by=[],
    )

  def get_task(self, task_id: str) -> Task:
    return self._to_task(self._issue(task_id))

  def get_task_description(self, task_id: str) -> str:
    issue = self._issue(task_id)
    return issue['body'] if issue['body'] is not None else ''

  def get_task_comments(self, task_id: str) -> list[Comment]:
    issue = self._issue(task_id)
    return [_to_comment(comment) for comment in self._comments(issue['number'])]

  def _comments(self, number: int) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    page = 1
    while True:
      batch = api.get(
        self._url(f'/issues/{number}/comments?per_page={_PAGE_SIZE}&page={page}'), self._token()
      )
      comments.extend(batch)
      if len(batch) < _PAGE_SIZE:
        return comments
      page += 1

  def update_task(
    self,
    task_id: str,
    *,
    name: Optional[str] = None,
    status: Optional[Status] = None,
    tags: Optional[list[str]] = None,
  ) -> None:
    payload: dict[str, Any] = {}
    if name is not None:
      payload['title'] = name
    if status is not None:
      payload.update(_STATUS_TO_PATCH[status])
    if tags is not None:
      payload['labels'] = tags
    if len(payload) == 0:
      return
    issue = self._issue(task_id)
    api.patch(self._url(f'/issues/{issue["number"]}'), self._token(), payload)

  def add_comment(self, task_id: str, topic: str, body: str) -> None:
    issue = self._issue(task_id)
    entry = f'### {topic}\n\n{body}'
    api.post(self._url(f'/issues/{issue["number"]}/comments'), self._token(), {'body': entry})

  def append_description(self, task_id: str, markdown: str) -> None:
    issue = self._issue(task_id)
    body = issue['body'] if issue['body'] is not None else ''
    combined = markdown if body == '' else f'{body}\n\n{markdown}'
    api.patch(self._url(f'/issues/{issue["number"]}'), self._token(), {'body': combined})

  def edit_description(
    self, task_id: str, old_string: str, new_string: str, replace_all: bool = False
  ) -> int:
    if old_string == '':
      raise ValueError('old_string must not be empty')
    if old_string == new_string:
      raise ValueError('old_string and new_string are identical; nothing to change')
    issue = self._issue(task_id)
    body = issue['body'] if issue['body'] is not None else ''
    count = body.count(old_string)
    if count == 0:
      raise ValueError('old_string not found in the task description')
    if count > 1 and not replace_all:
      raise ValueError(
        f'old_string occurs {count} times in the description; pass replace_all=True or '
        'expand old_string with more context to make it unique'
      )
    api.patch(
      self._url(f'/issues/{issue["number"]}'),
      self._token(),
      {'body': body.replace(old_string, new_string)},
    )
    return count

  def list_tasks(
    self,
    *,
    status: Optional[Status] = None,
    project: Optional[str] = None,
    limit: int = 20,
  ) -> list[Task]:
    if project is not None:
      raise ValueError('the GitHub backend has no projects; the project filter is not supported')
    state = (
      'all' if status is None else {'open': 'open', 'done': 'closed', 'dropped': 'closed'}[status]
    )
    tasks: list[Task] = []
    page = 1
    while len(tasks) < limit:
      batch = api.get(
        self._url(f'/issues?state={state}&per_page={_PAGE_SIZE}&page={page}'), self._token()
      )
      for issue in batch:
        # the issues listing includes pull requests; brog tracks issues only
        if 'pull_request' in issue:
          continue
        # done vs dropped both live under state=closed; the reason splits client-side
        if status is not None and _status_from_issue(issue) != status:
          continue
        tasks.append(self._to_task(issue))
        if len(tasks) == limit:
          break
      if len(batch) < _PAGE_SIZE:
        break
      page += 1
    return tasks
