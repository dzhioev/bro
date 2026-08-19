from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from bro.base import credentials
from bro.base.text_window import DEFAULT_LIMIT, MAX_LIMIT
from bro.brog.mcp import toolset
from bro.brog.model import Comment, Project, Task
from bro.mcp import mount

# tools are built once against a shared mock System (schema derivation is not
# free); the autouse fixture resets the mock between tests.
_SYSTEM = MagicMock()
_TOOLS = {t.name: t for t in toolset.tools(_SYSTEM)}


@pytest.fixture(autouse=True)
def fake_system():
  _SYSTEM.reset_mock(return_value=True, side_effect=True)
  return _SYSTEM


def tool(name):
  return _TOOLS[name]


def _task(**overrides) -> Task:
  fields = {
    'id': 'tid',
    'name': 'a task',
    'status': 'open',
    'url': 'https://tracker.example/tasks/tid',
    'tags': ['dev'],
    'project': None,
    'blocked_by': [],
  }
  fields.update(overrides)
  return Task(**fields)


class TestRoster:
  def test_tool_names(self):
    assert set(toolset.tool_names) == {
      'create_task',
      'get_task',
      'read_task',
      'read_comments',
      'update_task',
      'add_comment',
      'append_description',
      'edit_description',
      'list_tasks',
    }

  def test_static_secrets(self):
    assert mount(toolset).server_specs[0].needed_secrets == ('brog',)

  def test_scoped_subset_keeps_the_static_secrets(self):
    assert mount(toolset, 'get_task', 'read_task').server_specs[0].needed_secrets == ('brog',)

  def test_build(self, monkeypatch):
    monkeypatch.setattr(
      credentials,
      'get_json',
      lambda name: {'backend': 'github', 'token': 't', 'repo': 'owner/repository'},
    )
    server = toolset.build()
    assert server.namespace == 'brog'
    assert server.needed_secrets == ('brog',)


class TestCreateTask:
  @pytest.mark.asyncio
  async def test_returns_id_and_url(self, fake_system):
    fake_system.create_task.return_value = _task()
    result = await tool('create_task').call({'name': 'a task'})
    assert result == {'id': 'tid', 'url': 'https://tracker.example/tasks/tid'}
    fake_system.create_task.assert_called_once_with(name='a task', body=None, tags=None)

  @pytest.mark.asyncio
  async def test_body_and_tags_pass_through(self, fake_system):
    fake_system.create_task.return_value = _task()
    await tool('create_task').call({'name': 'a task', 'body': '## Goal', 'tags': ['dev']})
    fake_system.create_task.assert_called_once_with(name='a task', body='## Goal', tags=['dev'])

  @pytest.mark.asyncio
  async def test_name_required(self, fake_system):
    with pytest.raises(ValidationError):
      await tool('create_task').call({})
    fake_system.create_task.assert_not_called()


class TestGetTask:
  @pytest.mark.asyncio
  async def test_returns_task_with_project(self, fake_system):
    fake_system.get_task.return_value = _task(
      project=Project(id='pid', name='example', summary='example project'),
      blocked_by=['u1'],
    )
    result = await tool('get_task').call({'task_id': 'tid'})
    assert result == {
      'id': 'tid',
      'name': 'a task',
      'status': 'open',
      'url': 'https://tracker.example/tasks/tid',
      'tags': ['dev'],
      'project': {'id': 'pid', 'name': 'example', 'summary': 'example project'},
      'blocked_by': ['u1'],
    }
    fake_system.get_task.assert_called_once_with('tid')


class TestReadTask:
  @pytest.mark.asyncio
  async def test_numbers_lines_one_based(self, fake_system):
    fake_system.get_task_description.return_value = '## Goal\n\nsome body'
    result = await tool('read_task').call({'task_id': 'tid'})
    assert '    1\t## Goal' in result
    assert '    3\tsome body' in result
    assert 'skipped' not in result
    fake_system.get_task_description.assert_called_once_with('tid')

  @pytest.mark.asyncio
  async def test_offset_and_limit_window(self, fake_system):
    fake_system.get_task_description.return_value = 'a\nb\nc\nd\ne'
    result = await tool('read_task').call({'task_id': 'tid', 'offset': 2, 'limit': 2})
    assert '    3\tc' in result
    assert '    4\td' in result
    assert '    5\t' not in result
    assert 'skipped before: 2 lines' in result
    assert 'skipped after: 1 lines' in result

  @pytest.mark.asyncio
  async def test_default_limit_caps_long_documents(self, fake_system):
    fake_system.get_task_description.return_value = ''.join(
      f'line {i}\n' for i in range(DEFAULT_LIMIT + 50)
    )
    result = await tool('read_task').call({'task_id': 'tid'})
    assert 'skipped after: 50 lines' in result

  @pytest.mark.asyncio
  async def test_oversized_limit_clamped_with_note(self, fake_system):
    fake_system.get_task_description.return_value = 'a\nb'
    result = await tool('read_task').call({'task_id': 'tid', 'limit': MAX_LIMIT + 5})
    assert f'limit {MAX_LIMIT + 5:,} clamped to {MAX_LIMIT:,}' in result

  @pytest.mark.asyncio
  async def test_negative_offset_rejected(self, fake_system):
    with pytest.raises(ValidationError):
      await tool('read_task').call({'task_id': 'tid', 'offset': -1})
    fake_system.get_task_description.assert_not_called()


class TestReadComments:
  @pytest.mark.asyncio
  async def test_returns_structured_entries(self, fake_system):
    fake_system.get_task_comments.return_value = [
      Comment(
        topic='plan',
        author='dev',
        timestamp=datetime(2026, 7, 13, 2, 20, tzinfo=UTC),
        body='entry body',
      ),
      Comment(topic=None, author=None, timestamp=datetime(2026, 7, 13, 3, 0, tzinfo=UTC), body='x'),
    ]
    result = await tool('read_comments').call({'task_id': 'tid'})
    assert result == {
      'result': [
        {
          'topic': 'plan',
          'author': 'dev',
          'timestamp': '2026-07-13T02:20:00Z',
          'body': 'entry body',
        },
        {'topic': None, 'author': None, 'timestamp': '2026-07-13T03:00:00Z', 'body': 'x'},
      ]
    }
    fake_system.get_task_comments.assert_called_once_with('tid')

  @pytest.mark.asyncio
  async def test_no_comments_is_empty(self, fake_system):
    fake_system.get_task_comments.return_value = []
    assert await tool('read_comments').call({'task_id': 'tid'}) == {'result': []}


class TestUpdateTask:
  @pytest.mark.asyncio
  async def test_passes_properties(self, fake_system):
    result = await tool('update_task').call(
      {'task_id': 'tid', 'name': 'renamed', 'status': 'done', 'tags': ['shipped']}
    )
    assert result == 'ok'
    fake_system.update_task.assert_called_once_with(
      'tid', name='renamed', status='done', tags=['shipped']
    )

  @pytest.mark.asyncio
  async def test_omitted_properties_stay_none(self, fake_system):
    await tool('update_task').call({'task_id': 'tid', 'status': 'dropped'})
    fake_system.update_task.assert_called_once_with('tid', name=None, status='dropped', tags=None)

  @pytest.mark.asyncio
  async def test_invalid_status_rejected(self, fake_system):
    with pytest.raises(ValidationError):
      await tool('update_task').call({'task_id': 'tid', 'status': 'Live'})
    fake_system.update_task.assert_not_called()


class TestAddComment:
  @pytest.mark.asyncio
  async def test_passes_topic_and_body(self, fake_system):
    result = await tool('add_comment').call(
      {'task_id': 'tid', 'topic': 'fixed', 'body': 'all good'}
    )
    assert result == 'ok'
    fake_system.add_comment.assert_called_once_with('tid', 'fixed', 'all good')

  @pytest.mark.asyncio
  async def test_topic_and_body_required(self, fake_system):
    with pytest.raises(ValidationError):
      await tool('add_comment').call({'task_id': 'tid', 'topic': 'fixed'})
    with pytest.raises(ValidationError):
      await tool('add_comment').call({'task_id': 'tid', 'body': 'all good'})
    fake_system.add_comment.assert_not_called()


class TestDescriptionTools:
  @pytest.mark.asyncio
  async def test_append_description(self, fake_system):
    result = await tool('append_description').call({'task_id': 'tid', 'markdown': '## Deploy'})
    assert result == 'ok'
    fake_system.append_description.assert_called_once_with('tid', '## Deploy')

  @pytest.mark.asyncio
  async def test_edit_description_formats_the_count(self, fake_system):
    fake_system.edit_description.return_value = 1
    result = await tool('edit_description').call(
      {'task_id': 'tid', 'old_string': 'old', 'new_string': 'new'}
    )
    assert result == 'replaced 1 occurrence(s)'
    fake_system.edit_description.assert_called_once_with('tid', 'old', 'new', replace_all=False)

  @pytest.mark.asyncio
  async def test_edit_description_replace_all(self, fake_system):
    fake_system.edit_description.return_value = 3
    result = await tool('edit_description').call(
      {'task_id': 'tid', 'old_string': 'old', 'new_string': 'new', 'replace_all': True}
    )
    assert result == 'replaced 3 occurrence(s)'
    fake_system.edit_description.assert_called_once_with('tid', 'old', 'new', replace_all=True)

  @pytest.mark.asyncio
  async def test_comment_scope_errors_surface(self, fake_system):
    # the sentinel guard lives in the backend; this confirms the tool layer
    # propagates it rather than swallowing it
    fake_system.edit_description.side_effect = ValueError('comments are append-only history')
    with pytest.raises(ValueError, match='append-only'):
      await tool('edit_description').call(
        {'task_id': 'tid', 'old_string': 'old entry', 'new_string': 'new'}
      )


class TestListTasks:
  @pytest.mark.asyncio
  async def test_no_filters(self, fake_system):
    fake_system.list_tasks.return_value = [_task()]
    result = await tool('list_tasks').call({})
    assert result == {
      'result': [
        {
          'id': 'tid',
          'name': 'a task',
          'status': 'open',
          'url': 'https://tracker.example/tasks/tid',
          'tags': ['dev'],
          'project': None,
          'blocked_by': [],
        }
      ]
    }
    fake_system.list_tasks.assert_called_once_with(status=None, project=None, limit=20)

  @pytest.mark.asyncio
  async def test_filters_pass_through(self, fake_system):
    fake_system.list_tasks.return_value = []
    await tool('list_tasks').call({'status': 'open', 'project': 'pid', 'limit': 5})
    fake_system.list_tasks.assert_called_once_with(status='open', project='pid', limit=5)

  @pytest.mark.asyncio
  async def test_invalid_status_rejected(self, fake_system):
    with pytest.raises(ValidationError):
      await tool('list_tasks').call({'status': 'Live'})

  @pytest.mark.asyncio
  async def test_limit_bounds_enforced(self, fake_system):
    with pytest.raises(ValidationError):
      await tool('list_tasks').call({'limit': 0})
    with pytest.raises(ValidationError):
      await tool('list_tasks').call({'limit': 101})
    fake_system.list_tasks.assert_not_called()
