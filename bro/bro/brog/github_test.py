from datetime import UTC, datetime
from typing import Any, Optional

import pytest

import brog.github
import github.api
from brog.github import System, origin_repo
from brog.model import Comment

_REPO = 'octo/scratch'
_API = f'https://api.github.com/repos/{_REPO}'


def _issue(
  number: int = 5,
  title: str = 'fix the thing',
  state: str = 'open',
  state_reason: Optional[str] = None,
  body: Optional[str] = 'the description',
  labels: tuple[str, ...] = (),
  total_blocked_by: int = 0,
  pull_request: bool = False,
) -> dict[str, Any]:
  issue: dict[str, Any] = {
    'number': number,
    'title': title,
    'state': state,
    'state_reason': state_reason,
    'body': body,
    'labels': [{'name': name} for name in labels],
    'html_url': f'https://github.com/{_REPO}/issues/{number}',
    'issue_dependencies_summary': {
      'blocked_by': total_blocked_by,
      'total_blocked_by': total_blocked_by,
      'blocking': 0,
      'total_blocking': 0,
    },
  }
  if pull_request:
    issue['pull_request'] = {'url': f'{_API}/pulls/{number}'}
  return issue


def _comment(
  id: int, login: str, body: str, created_at: str = '2026-07-13T10:30:00Z'
) -> dict[str, Any]:
  return {'id': id, 'user': {'login': login}, 'body': body, 'created_at': created_at}


def _issue_url(number: int = 5) -> str:
  return f'{_API}/issues/{number}'


def _comments_url(number: int = 5, page: int = 1) -> str:
  return f'{_API}/issues/{number}/comments?per_page=100&page={page}'


def _blocked_by_url(number: int = 5) -> str:
  return f'{_API}/issues/{number}/dependencies/blocked_by?per_page=100'


def _list_url(state: str, page: int = 1) -> str:
  return f'{_API}/issues?state={state}&per_page=100&page={page}'


class _FakeAPI:
  """serves canned responses by url; records every call as (method, url, body)"""

  def __init__(self):
    self.responses: dict[str, Any] = {}
    self.calls: list[tuple[str, str, Optional[Any]]] = []
    self.tokens: set[str] = set()

  def get(self, url: str, token: str) -> Any:
    self.tokens.add(token)
    self.calls.append(('GET', url, None))
    return self.responses[url]

  def post(self, url: str, token: str, body: Any) -> Any:
    self.tokens.add(token)
    self.calls.append(('POST', url, body))
    return self.responses.get(url)

  def patch(self, url: str, token: str, body: Any) -> Any:
    self.tokens.add(token)
    self.calls.append(('PATCH', url, body))
    return self.responses.get(url)


@pytest.fixture
def api(monkeypatch) -> _FakeAPI:
  fake = _FakeAPI()
  monkeypatch.setattr(github.api, 'get', fake.get)
  monkeypatch.setattr(github.api, 'post', fake.post)
  monkeypatch.setattr(github.api, 'patch', fake.patch)
  return fake


def _system() -> System:
  return System(token=lambda: 't', repo=_REPO)


class TestConstruction:
  @pytest.mark.parametrize('repo', ['no-slash', 'a/b/c', 'a /b', ''])
  def test_malformed_repo_rejected(self, repo):
    with pytest.raises(ValueError, match='must be owner/name'):
      System(token=lambda: 't', repo=repo)

  def test_token_provider_consulted_per_call(self, api):
    api.responses[_issue_url(1)] = _issue(number=1)
    api.responses[_issue_url(2)] = _issue(number=2)
    tokens = iter(['t1', 't2'])
    system = System(token=lambda: next(tokens), repo=_REPO)
    system.get_task('1')
    system.get_task('2')
    # each API call reads the provider afresh, so a re-minted token is picked up
    assert api.tokens == {'t1', 't2'}


class TestRefs:
  @pytest.mark.parametrize(
    'ref', ['5', '#5', f'https://github.com/{_REPO}/issues/5'], ids=['number', 'hash', 'url']
  )
  def test_natural_refs_accepted(self, api, ref):
    api.responses[_issue_url(5)] = _issue(number=5)
    task = _system().get_task(ref)
    assert task.id == '5'

  def test_url_outside_the_configured_repo_rejected(self, api):
    with pytest.raises(ValueError, match='not in the configured repo'):
      _system().get_task('https://github.com/other/repo/issues/5')
    assert api.calls == []

  @pytest.mark.parametrize('ref', ['abc', '5.0', f'https://github.com/{_REPO}/pull/5', ''])
  def test_unrecognized_ref_rejected(self, api, ref):
    with pytest.raises(ValueError, match='unrecognized GitHub issue ref'):
      _system().get_task(ref)
    assert api.calls == []

  def test_pull_request_ref_rejected_on_every_op(self, api):
    api.responses[_issue_url(7)] = _issue(number=7, pull_request=True)
    system = _system()
    for operation in (
      lambda: system.get_task('7'),
      lambda: system.get_task_description('7'),
      lambda: system.get_task_comments('7'),
      lambda: system.update_task('7', name='renamed'),
      lambda: system.add_comment('7', 'plan', 'body'),
      lambda: system.append_description('7', 'more'),
      lambda: system.edit_description('7', 'the', 'a'),
    ):
      with pytest.raises(ValueError, match='is a pull request'):
        operation()
    # the rejection happens on the pre-fetch; nothing was written
    assert all(method == 'GET' for method, _, _ in api.calls)


class TestStatusMapping:
  @pytest.mark.parametrize(
    ('state', 'state_reason', 'expected'),
    [
      ('open', None, 'open'),
      ('closed', 'completed', 'done'),
      ('closed', None, 'done'),
      ('closed', 'not_planned', 'dropped'),
      ('closed', 'duplicate', 'dropped'),
    ],
  )
  def test_read(self, api, state, state_reason, expected):
    api.responses[_issue_url()] = _issue(state=state, state_reason=state_reason)
    assert _system().get_task('5').status == expected

  @pytest.mark.parametrize(
    ('status', 'payload'),
    [
      ('done', {'state': 'closed', 'state_reason': 'completed'}),
      ('dropped', {'state': 'closed', 'state_reason': 'not_planned'}),
      ('open', {'state': 'open'}),
    ],
  )
  def test_write(self, api, status, payload):
    api.responses[_issue_url()] = _issue()
    _system().update_task('5', status=status)
    assert api.calls[-1] == ('PATCH', _issue_url(), payload)


class TestGetTask:
  def test_maps_issue_fields(self, api):
    api.responses[_issue_url()] = _issue(title='a bug', labels=('dev', 'urgent'))
    task = _system().get_task('5')
    assert task.id == '5'
    assert task.name == 'a bug'
    assert task.url == f'https://github.com/{_REPO}/issues/5'
    assert task.tags == ['dev', 'urgent']
    assert task.project is None
    assert api.tokens == {'t'}

  def test_no_dependencies_short_circuits_blocked_by(self, api):
    api.responses[_issue_url()] = _issue(total_blocked_by=0)
    assert _system().get_task('5').blocked_by == []
    assert api.calls == [('GET', _issue_url(), None)]

  def test_blocked_by_lists_open_blockers_only(self, api):
    api.responses[_issue_url()] = _issue(total_blocked_by=2)
    api.responses[_blocked_by_url()] = [
      _issue(number=8, state='open'),
      _issue(number=9, state='closed', state_reason='completed'),
    ]
    assert _system().get_task('5').blocked_by == ['8']


class TestCreateTask:
  def test_minimal_payload(self, api):
    api.responses[f'{_API}/issues'] = _issue(number=11, title='new task', body=None)
    task = _system().create_task(name='new task')
    assert api.calls == [('POST', f'{_API}/issues', {'title': 'new task'})]
    assert task.id == '11'
    assert task.status == 'open'
    assert task.url == f'https://github.com/{_REPO}/issues/11'
    assert task.blocked_by == []

  def test_body_and_tags_forwarded(self, api):
    api.responses[f'{_API}/issues'] = _issue(number=11, title='new task', labels=('dev',))
    task = _system().create_task(name='new task', body='details', tags=['dev'])
    assert api.calls == [
      ('POST', f'{_API}/issues', {'title': 'new task', 'body': 'details', 'labels': ['dev']})
    ]
    assert task.tags == ['dev']


class TestUpdateTask:
  def test_name_and_tags_map_to_title_and_labels(self, api):
    api.responses[_issue_url()] = _issue()
    _system().update_task('5', name='renamed', tags=['a', 'b'])
    assert api.calls[-1] == ('PATCH', _issue_url(), {'title': 'renamed', 'labels': ['a', 'b']})

  def test_nothing_to_update_makes_no_calls(self, api):
    _system().update_task('5')
    assert api.calls == []


class TestGetTaskDescription:
  def test_returns_the_issue_body(self, api):
    api.responses[_issue_url()] = _issue(body='just the body')
    assert _system().get_task_description('5') == 'just the body'
    # the description is the issue body alone; comments are never fetched
    assert api.calls == [('GET', _issue_url(), None)]

  def test_null_body_reads_as_empty(self, api):
    api.responses[_issue_url()] = _issue(body=None)
    assert _system().get_task_description('5') == ''


class TestGetTaskComments:
  def test_no_comments_is_empty(self, api):
    api.responses[_issue_url()] = _issue()
    api.responses[_comments_url()] = []
    assert _system().get_task_comments('5') == []

  def test_topic_from_the_leading_heading(self, api):
    api.responses[_issue_url()] = _issue()
    api.responses[_comments_url()] = [
      _comment(1, 'octo-dev', '### plan\n\nthe plan body', created_at='2026-07-13T10:30:00Z')
    ]
    assert _system().get_task_comments('5') == [
      Comment(
        topic='plan',
        author='octo-dev',
        timestamp=datetime(2026, 7, 13, 10, 30, tzinfo=UTC),
        body='the plan body',
      )
    ]

  def test_headingless_comment_reads_whole_with_no_topic(self, api):
    api.responses[_issue_url()] = _issue()
    api.responses[_comments_url()] = [
      _comment(1, 'reviewer', 'drive-by note', created_at='2026-07-13T09:05:00Z')
    ]
    assert _system().get_task_comments('5') == [
      Comment(
        topic=None,
        author='reviewer',
        timestamp=datetime(2026, 7, 13, 9, 5, tzinfo=UTC),
        body='drive-by note',
      )
    ]

  def test_comment_pages_are_read_to_exhaustion(self, api):
    api.responses[_issue_url()] = _issue()
    api.responses[_comments_url(page=1)] = [
      _comment(i, 'octo-dev', f'### note {i}\n\nx') for i in range(100)
    ]
    api.responses[_comments_url(page=2)] = [_comment(100, 'octo-dev', '### last\n\nx')]
    comments = _system().get_task_comments('5')
    assert len(comments) == 101
    assert comments[-1].topic == 'last'
    assert ('GET', _comments_url(page=2), None) in api.calls


class TestAddComment:
  def test_posts_the_topic_as_a_leading_heading(self, api):
    api.responses[_issue_url()] = _issue()
    _system().add_comment('5', 'plan', 'the plan body')
    assert api.calls[-1] == (
      'POST',
      f'{_API}/issues/5/comments',
      {'body': '### plan\n\nthe plan body'},
    )


class TestAppendDescription:
  def test_appends_to_the_issue_body(self, api):
    api.responses[_issue_url()] = _issue(body='existing')
    _system().append_description('5', '## Design\n\nnew section')
    assert api.calls[-1] == (
      'PATCH',
      _issue_url(),
      {'body': 'existing\n\n## Design\n\nnew section'},
    )

  def test_empty_body_becomes_the_markdown(self, api):
    api.responses[_issue_url()] = _issue(body=None)
    _system().append_description('5', 'fresh')
    assert api.calls[-1] == ('PATCH', _issue_url(), {'body': 'fresh'})


class TestEditDescription:
  def test_replaces_a_unique_match(self, api):
    api.responses[_issue_url()] = _issue(body='alpha beta gamma')
    count = _system().edit_description('5', 'beta', 'delta')
    assert count == 1
    assert api.calls[-1] == ('PATCH', _issue_url(), {'body': 'alpha delta gamma'})

  def test_absent_old_string_errors(self, api):
    api.responses[_issue_url()] = _issue(body='alpha')
    with pytest.raises(ValueError, match='not found'):
      _system().edit_description('5', 'missing', 'x')

  def test_ambiguous_match_requires_replace_all(self, api):
    api.responses[_issue_url()] = _issue(body='dup and dup')
    with pytest.raises(ValueError, match='occurs 2 times'):
      _system().edit_description('5', 'dup', 'x')

  def test_replace_all_replaces_every_occurrence(self, api):
    api.responses[_issue_url()] = _issue(body='dup and dup')
    count = _system().edit_description('5', 'dup', 'x', replace_all=True)
    assert count == 2
    assert api.calls[-1] == ('PATCH', _issue_url(), {'body': 'x and x'})

  def test_empty_old_string_rejected(self, api):
    with pytest.raises(ValueError, match='must not be empty'):
      _system().edit_description('5', '', 'x')
    assert api.calls == []

  def test_identical_strings_rejected(self, api):
    with pytest.raises(ValueError, match='identical'):
      _system().edit_description('5', 'same', 'same')
    assert api.calls == []


class TestListTasks:
  def test_project_filter_rejected(self, api):
    with pytest.raises(ValueError, match='has no projects'):
      _system().list_tasks(project='p-1')
    assert api.calls == []

  def test_no_status_lists_all_states(self, api):
    api.responses[_list_url('all')] = [_issue(number=1), _issue(number=2, state='closed')]
    tasks = _system().list_tasks()
    assert [task.id for task in tasks] == ['1', '2']

  def test_pull_requests_are_filtered_out(self, api):
    api.responses[_list_url('open')] = [
      _issue(number=1),
      _issue(number=2, pull_request=True),
      _issue(number=3),
    ]
    tasks = _system().list_tasks(status='open')
    assert [task.id for task in tasks] == ['1', '3']

  def test_done_excludes_not_planned_closures(self, api):
    api.responses[_list_url('closed')] = [
      _issue(number=1, state='closed', state_reason='completed'),
      _issue(number=2, state='closed', state_reason='not_planned'),
      _issue(number=3, state='closed', state_reason='duplicate'),
    ]
    tasks = _system().list_tasks(status='done')
    assert [task.id for task in tasks] == ['1']

  def test_dropped_selects_not_planned_and_duplicate(self, api):
    api.responses[_list_url('closed')] = [
      _issue(number=1, state='closed', state_reason='completed'),
      _issue(number=2, state='closed', state_reason='not_planned'),
      _issue(number=3, state='closed', state_reason='duplicate'),
    ]
    tasks = _system().list_tasks(status='dropped')
    assert [task.id for task in tasks] == ['2', '3']

  def test_pages_past_pr_polluted_pages_until_limit(self, api):
    api.responses[_list_url('open', page=1)] = [
      _issue(number=i, pull_request=True) for i in range(100)
    ]
    api.responses[_list_url('open', page=2)] = [_issue(number=200), _issue(number=201)]
    tasks = _system().list_tasks(status='open', limit=2)
    assert [task.id for task in tasks] == ['200', '201']

  def test_stops_at_limit(self, api):
    api.responses[_list_url('open')] = [_issue(number=i) for i in range(1, 8)]
    tasks = _system().list_tasks(status='open', limit=3)
    assert [task.id for task in tasks] == ['1', '2', '3']

  def test_short_page_ends_the_listing(self, api):
    api.responses[_list_url('open')] = [_issue(number=1)]
    tasks = _system().list_tasks(status='open', limit=20)
    assert [task.id for task in tasks] == ['1']
    assert api.calls == [('GET', _list_url('open'), None)]


class _FakeCompletedProcess:
  def __init__(self, returncode: int, stdout: str = ''):
    self.returncode = returncode
    self.stdout = stdout


class TestOriginRepo:
  @pytest.mark.parametrize(
    'url',
    [
      'git@github.com:octo/scratch.git',
      'git@github.com:octo/scratch',
      'https://github.com/octo/scratch.git',
      'https://github.com/octo/scratch',
      'ssh://git@github.com/octo/scratch.git',
    ],
  )
  def test_remote_url_forms(self, monkeypatch, url):
    monkeypatch.setattr(
      brog.github.subprocess, 'run', lambda *args, **kwargs: _FakeCompletedProcess(0, f'{url}\n')
    )
    assert origin_repo() == 'octo/scratch'

  def test_no_origin_remote_errors(self, monkeypatch):
    monkeypatch.setattr(
      brog.github.subprocess, 'run', lambda *args, **kwargs: _FakeCompletedProcess(2)
    )
    with pytest.raises(ValueError, match='no origin remote'):
      origin_repo()

  def test_non_github_remote_errors(self, monkeypatch):
    monkeypatch.setattr(
      brog.github.subprocess,
      'run',
      lambda *args, **kwargs: _FakeCompletedProcess(0, 'https://gitlab.com/octo/scratch.git\n'),
    )
    with pytest.raises(ValueError, match='cannot derive owner/name'):
      origin_repo()
