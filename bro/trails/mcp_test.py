from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

import bro.trails.mcp as trails_mcp
from bro.base import credentials
from bro.base.text_window import DEFAULT_LIMIT, MAX_LIMIT
from bro.llm.mcp import mount
from bro.trails.mcp import toolset
from bros.lead import Lead

_CLIENT = MagicMock()
_TOOLS = {tool.name: tool for tool in toolset.tools(_CLIENT)}


@pytest.fixture(autouse=True)
def fake_client():
  _CLIENT.reset_mock(return_value=True, side_effect=True)
  return _CLIENT


def tool(name):
  return _TOOLS[name]


class TestRoster:
  def test_tool_names(self):
    assert set(toolset.tool_names) == {'list', 'show', 'steps', 'grep', 'tree'}

  def test_static_secret(self):
    assert mount(toolset).server_specs[0].needed_secrets == ('trails',)

  def test_build(self, monkeypatch):
    monkeypatch.setattr(
      credentials,
      'get_json',
      lambda name: {'base_url': 'https://trails.example', 'token': 'secret'},
    )
    server = toolset.build()
    assert server.namespace == 'trails'
    assert server.needed_secrets == ('trails',)
    server.close()

  def test_lead_mounts_trails(self):
    assert {'brog', 'trails'} <= set(Lead().needed_secrets())


class TestList:
  @pytest.mark.asyncio
  async def test_returns_the_rewind_summary_fields(self, fake_client):
    fake_client.list_trails.return_value = {
      'trails': [
        {
          'id': 'T1',
          'started_at': '2026-01-01T00:00:00Z',
          'harness': 'claude',
          'location': {'workspace': 'feature'},
          'native': {'llm': {'model': 'claude-opus'}},
          'end': None,
          'forked_from': {'trail_id': 'T0', 'step_id': 4},
          'subject': 'implement the feature',
        },
        {
          'id': 'T2',
          'started_at': '2026-01-02T00:00:00Z',
          'harness': 'bro',
          'bro': 'reviewer',
          'native': {'llm': {'model': 'gpt-5'}},
          'end': {'at': '2026-01-02T00:01:00Z', 'reason': 'ok'},
        },
      ]
    }

    result = await tool('list').call({'harness': 'claude', 'limit': 2})

    assert result == {
      'result': [
        {
          'id': 'T1',
          'started_at': '2026-01-01T00:00:00Z',
          'harness': 'claude',
          'owner': 'feature',
          'model': 'claude-opus',
          'status': 'live',
          'forked_from': 'T0',
          'subject': 'implement the feature',
        },
        {
          'id': 'T2',
          'started_at': '2026-01-02T00:00:00Z',
          'harness': 'bro',
          'owner': 'reviewer',
          'model': 'gpt-5',
          'status': 'done:ok',
          'forked_from': None,
          'subject': None,
        },
      ]
    }
    fake_client.list_trails.assert_called_once_with(
      harness='claude',
      bro=None,
      since=None,
      until=None,
      forked_from=None,
      limit=2,
    )

  @pytest.mark.asyncio
  async def test_rejects_multiple_indexed_filters(self, fake_client):
    with pytest.raises(ValueError, match='mutually exclusive'):
      await tool('list').call({'harness': 'bro', 'bro': 'dev'})
    fake_client.list_trails.assert_not_called()

  @pytest.mark.asyncio
  async def test_subject_is_bounded(self, fake_client):
    fake_client.list_trails.return_value = {
      'trails': [
        {
          'id': 'T1',
          'started_at': '2026-01-01T00:00:00Z',
          'harness': 'bro',
          'bro': 'dev',
          'native': {'llm': {'model': 'gpt-5'}},
          'end': None,
          'subject': 'x' * 1000,
        }
      ]
    }

    result = await tool('list').call({})

    subject = result['result'][0]['subject']
    assert subject == ('x' * 60) + '... <940 more chars>'

  @pytest.mark.asyncio
  async def test_limit_is_bounded(self, fake_client):
    with pytest.raises(ValidationError):
      await tool('list').call({'limit': 101})
    fake_client.list_trails.assert_not_called()


class TestTextViews:
  @pytest.mark.asyncio
  @pytest.mark.parametrize(
    ('tool_name', 'renderer_name'),
    [('show', '_show_document'), ('steps', '_steps_document'), ('tree', '_tree_document')],
  )
  async def test_returns_an_oriented_bounded_window(self, monkeypatch, tool_name, renderer_name):
    renderer = MagicMock(return_value='first\nsecond\nthird\n')
    monkeypatch.setattr(trails_mcp, renderer_name, renderer)

    result = await tool(tool_name).call({'trail_id': 'T1', 'offset': 1, 'limit': 1})

    assert 'skipped before: 1 lines' in result
    assert '    2\tsecond' in result
    assert 'skipped after: 1 lines' in result
    renderer.assert_called_once_with(_CLIENT, 'T1')

  @pytest.mark.asyncio
  async def test_clamps_a_fat_finger_limit(self, monkeypatch):
    monkeypatch.setattr(trails_mcp, '_show_document', lambda *args: 'one line')
    result = await tool('show').call({'trail_id': 'T1', 'limit': MAX_LIMIT + 1})
    assert f'limit {MAX_LIMIT + 1:,} clamped to {MAX_LIMIT:,}' in result

  @pytest.mark.asyncio
  async def test_defaults_to_the_shared_limit(self, monkeypatch):
    monkeypatch.setattr(
      trails_mcp,
      '_steps_document',
      lambda *args: ''.join(f'line {index}\n' for index in range(DEFAULT_LIMIT + 1)),
    )
    result = await tool('steps').call({'trail_id': 'T1'})
    assert 'skipped after: 1 lines' in result


class TestGrep:
  @pytest.mark.asyncio
  async def test_bounds_matches_and_passes_search_scope(self, monkeypatch):
    renderer = MagicMock(return_value='T1:1:first\nT1:2:second\nT1:3:third\n')
    monkeypatch.setattr(trails_mcp, '_grep_document', renderer)

    result = await tool('grep').call(
      {
        'pattern': 'needle',
        'trails': ['T1'],
        'ignore_case': True,
        'before_context': 2,
        'after_context': 3,
        'offset': 1,
        'limit': 1,
      }
    )

    assert 'T1:2:second' in result
    assert 'T1:1:first' not in result
    renderer.assert_called_once_with(
      _CLIENT,
      'needle',
      trails=['T1'],
      harness=None,
      ignore_case=True,
      before_context=2,
      after_context=3,
      trail_limit=20,
    )

  @pytest.mark.asyncio
  async def test_search_scope_is_bounded(self, fake_client):
    with pytest.raises(ValidationError):
      await tool('grep').call({'pattern': 'x', 'trail_limit': 101})
    fake_client.iter_trails.assert_not_called()
