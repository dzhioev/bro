import json
from typing import Any, Optional, cast
from unittest.mock import MagicMock

import pytest

from base.ansi import Colors
from llm.tracker import HTTPStatusError
from trails.client import TrailsClient
from trails.rewind import (
  _command_grep,
  _command_show,
  _follow_steps,
  _format_bro_header,
  _format_step_summary,
  _format_trail_row,
  _render_claude_trail,
  _render_tree,
  _truncate_oneline,
  _with_default_command,
)

NO_COLOR = Colors(enabled=False)
LULID = '01kydtgppz-y7fdwep2-apw9ag3b'


class FakeClient:
  """in-memory stand-in for the TrailsClient read surface rewind drives."""

  def __init__(self):
    self.trails: dict[str, dict] = {}
    self.records: dict[str, list[str]] = {}
    self.steps: dict[str, list[dict]] = {}
    self.contexts: dict[str, Any] = {}

  def add_claude(self, trail_id: str, lines: list[str], **header: Any) -> None:
    self.trails[trail_id] = {
      'id': trail_id,
      'harness': 'claude',
      'bro': 'ppp-dev',
      'started_at': '2026-01-01T00:00:00Z',
      'end': None,
      'turn_count': 0,
      'native': {'segment': 'seg', 'llm': {'model': 'claude'}, 'line_count': len(lines)},
      'location': {'workspace': 'ws'},
      **header,
    }
    self.records[trail_id] = lines

  def add_bro(self, trail_id: str, steps: list[dict], **header: Any) -> None:
    self.trails[trail_id] = {
      'id': trail_id,
      'harness': 'bro',
      'bro': 'dev',
      'version': '2',
      'started_at': '2026-01-01T00:00:00Z',
      'end': None,
      'interactive': False,
      'surface': 'ask',
      'turn_count': 0,
      'native': {'llm': {'model': 'gpt-5'}},
      'usage': {},
      'models': [],
      **header,
    }
    self.steps[trail_id] = steps

  def get_trail(self, trail_id: str) -> dict:
    if trail_id not in self.trails:
      raise HTTPStatusError(404, f'trail not found: {trail_id}')
    return self.trails[trail_id]

  def iter_steps(self, trail_id: str, *, after: Optional[str] = None):
    if trail_id in self.steps:
      rows = self.steps[trail_id]
      started = after is None
      for row in rows:
        if started:
          yield row
        elif row['step_id'] == after:
          started = True
      return
    start = int(after) + 1 if after is not None else 0
    for index, raw in enumerate(self.records[trail_id][start:], start=start):
      try:
        record = json.loads(raw)
      except json.JSONDecodeError:
        record = None
      yield {'step_id': str(index), 'ts': None, 'raw': raw, 'record': record}

  def iter_trails(self, **filters: Any):
    forked_from = filters.get('forked_from')
    selected = []
    for trail in self.trails.values():
      if forked_from is not None:
        if trail.get('forked_from', {}).get('trail_id') != forked_from:
          continue
      elif filters.get('harness') is not None and trail['harness'] != filters['harness']:
        continue
      selected.append(trail)
    limit = filters.get('max_items')
    yield from selected[: limit if limit is not None else len(selected)]

  def get_launch_context(self, trail_id: str) -> Optional[Any]:
    return self.contexts.get(trail_id)


def _cast(client: FakeClient) -> 'TrailsClient':
  # FakeClient stands in for the TrailsClient surface structurally
  return cast('TrailsClient', client)


def _user(text: str, uuid: str = 'u1') -> str:
  return json.dumps(
    {
      'type': 'user',
      'uuid': uuid,
      'timestamp': '2026-01-01T00:00:01Z',
      'message': {'content': text},
    }
  )


def _assistant(text: str, uuid: str = 'a1') -> str:
  return json.dumps(
    {
      'type': 'assistant',
      'uuid': uuid,
      'timestamp': '2026-01-01T00:00:02Z',
      'message': {'model': 'claude', 'content': [{'type': 'text', 'text': text}]},
    }
  )


class TestTruncateOneline:
  def test_short_string_passes_through(self):
    assert _truncate_oneline('hello') == 'hello'

  def test_long_string_truncates_with_marker(self):
    out = _truncate_oneline('x' * 300, limit=100)
    assert out.startswith('x' * 100)
    assert '... <200 more chars>' in out


class TestFormatTrailRow:
  def test_bro_row_names_the_bro(self):
    out = _format_trail_row(
      {
        'id': 'T1',
        'harness': 'bro',
        'bro': 'dev',
        'native': {'llm': {'model': 'gpt-5'}},
        'started_at': '2026-06-07T22:14:03.000000Z',
        'end': None,
      },
      NO_COLOR,
    )
    assert 'T1' in out
    assert 'bro' in out
    assert 'dev' in out
    assert 'live' in out

  def test_claude_row_names_the_workspace_and_subject(self):
    out = _format_trail_row(
      {
        'id': 'T2',
        'harness': 'claude',
        'bro': 'ppp-dev',
        'location': {'workspace': 'my-feature'},
        'native': {'llm': {'model': 'claude'}},
        'started_at': '2026-06-07T22:14:03.000000Z',
        'end': {'at': '2026-06-07T23:00:00Z', 'reason': 'ok'},
        'subject': 'fix the recorder',
        'forked_from': {'trail_id': 'T1', 'step_id': '4'},
      },
      NO_COLOR,
    )
    assert 'claude' in out
    assert 'my-feature' in out
    assert 'done:ok' in out
    assert 'fork-of T1' in out
    assert 'fix the recorder' in out


class TestFormatStepSummary:
  def test_inline_body_and_extras(self):
    out = _format_step_summary(
      {
        'step_id': 'S1',
        'kind': 'tool_call',
        'body': None,
        'ts': '2026-06-07T00:00:00.000000Z',
        'turn_index': 2,
        'tool_name': 'add_task',
        'call_id': 'c1',
        'arguments': {'name': 'x'},
        'where': 'retired-writer',
      },
      NO_COLOR,
    )
    assert out.startswith('S1  ')
    assert 'tool_name=add_task' in out
    assert 'args=' in out
    assert 'where' not in out

  def test_historical_terminal_end_reason_renders_as_ok(self):
    out = _format_step_summary(
      {
        'step_id': 'S9',
        'kind': 'end',
        'body': {'reason': 'terminal'},
        'ts': '2026-06-07T00:00:00.000000Z',
      },
      NO_COLOR,
    )
    assert '"reason": "ok"' in out
    assert 'terminal' not in out

  def test_spilled_body_renders_size_and_url(self):
    out = _format_step_summary(
      {
        'kind': 'llm_call',
        'body': {'s3': 'trails/T/steps/S.json', 'url': 'https://example.com/x', 'size': 4096},
        'ts': '2026-06-07T00:00:00.000000Z',
        'turn_index': 1,
      },
      NO_COLOR,
    )
    assert '<4096 bytes spilled>' in out
    assert 'https://example.com/x' in out

  def test_body_with_only_an_s3_key_is_rendered_inline(self):
    out = _format_step_summary(
      {
        'kind': 'tool_result',
        'body': {'s3': 'a genuine field'},
        'ts': '2026-06-07T00:00:00.000000Z',
      },
      NO_COLOR,
    )
    assert '"s3": "a genuine field"' in out
    assert 'spilled' not in out


class TestBroHeader:
  def test_render_includes_aggregates_and_forked_from(self):
    out = _format_bro_header(
      {
        'id': 'T1',
        'bro': 'dev',
        'harness': 'bro',
        'version': '1',
        'native': {
          'llm': {'type': 'chat_gpt', 'model': 'gpt-5'},
          'step_counts_by_kind': {'reasoning': 3, 'end': 0},
        },
        'started_at': '2026-06-07T22:14:03.000000Z',
        'end': {'at': '2026-06-07T22:15:00.000000Z', 'reason': 'ok'},
        'interactive': False,
        'surface': 'ask',
        'forked_from': {'trail_id': 'T-p', 'step_id': 'S5'},
        'turn_count': 3,
        'usage': {'gpt-5': {'input': 100, 'cache_write': 0, 'cache_read': 0, 'output': 50}},
        'models': ['gpt-5'],
      },
      NO_COLOR,
    )
    assert 'T-p @ step S5' in out
    assert '"input": 100' in out
    assert 'reasoning=3' in out
    assert 'end=0' not in out  # zero counts pruned


class TestClaudeRendering:
  def test_renders_conversation_with_context_preamble(self):
    client = FakeClient()
    client.add_claude('T1', [_user('hello'), _assistant('hi there')])
    client.contexts['T1'] = [{'title': 'git state', 'fields': {'branch': 'b'}}]
    out = _render_claude_trail(_cast(client), client.get_trail('T1'), NO_COLOR)
    assert 'SESSION CONTEXT' in out
    assert '▸ git state' in out
    assert '#1 USER' in out
    assert 'hello' in out
    assert '#2 ASSISTANT' in out
    assert 'hi there' in out

  def test_walks_the_fork_chain_through_parent_anchors(self):
    client = FakeClient()
    client.add_claude('T1', [_user('hello', 'u1'), _assistant('hi', 'a1'), _user('lost', 'u2')])
    client.add_claude(
      'T2',
      [_user('resumed', 'u3')],
      forked_from={'trail_id': 'T1', 'step_id': '1'},
    )
    out = _render_claude_trail(_cast(client), client.get_trail('T2'), NO_COLOR)
    assert 'hello' in out
    assert 'hi' in out
    # past the parent anchor: the fork did not carry this record
    assert 'lost' not in out
    assert 'resumed' in out
    assert '── resumed as trail T2' in out
    # turn numbering continues across the chain
    assert '#3 USER' in out

  def test_tool_results_inline_under_their_call(self):
    call = json.dumps(
      {
        'type': 'assistant',
        'uuid': 'a1',
        'message': {
          'model': 'claude',
          'content': [{'type': 'tool_use', 'id': 'tu1', 'name': 'bash', 'input': {'cmd': 'ls'}}],
        },
      }
    )
    result = json.dumps(
      {
        'type': 'user',
        'uuid': 'u2',
        'message': {
          'content': [{'type': 'tool_result', 'tool_use_id': 'tu1', 'content': 'file.txt'}]
        },
      }
    )
    client = FakeClient()
    client.add_claude('T1', [call, result])
    out = _render_claude_trail(_cast(client), client.get_trail('T1'), NO_COLOR)
    assert '→ bash({"cmd": "ls"})' in out
    assert 'file.txt' in out


class TestShow:
  def test_bro_trail_renders_header_and_steps(self, capsys):
    client = FakeClient()
    client.add_bro(
      'T1',
      [{'step_id': 'S1', 'kind': 'user_input', 'body': 'hi', 'ts': '2026-01-01T00:00:00Z'}],
    )
    args = {'trail_id': 'T1', 'color': 'never', 'no_pager': True}
    assert _command_show(_cast(client), args, NO_COLOR) == 0
    out = capsys.readouterr().out
    assert 'trail      T1' in out
    assert 'user_input' in out

  def test_unknown_id_propagates_not_found(self):
    client = FakeClient()
    args = {'trail_id': 'b2249daa', 'color': 'never', 'no_pager': True}
    with pytest.raises(HTTPStatusError, match='trail not found'):
      _command_show(_cast(client), args, NO_COLOR)


class TestGrep:
  def _args(self, pattern: str, **overrides) -> dict:
    return {'pattern': pattern, 'trails': [], 'color': 'never', **overrides}

  def test_matches_across_harnesses(self, capsys):
    client = FakeClient()
    client.add_claude('T-claude', [_user('the needle is here')])
    client.add_bro(
      'T-bro',
      [{'step_id': 'S1', 'kind': 'assistant', 'body': 'needle too', 'ts': None}],
    )
    assert _command_grep(_cast(client), self._args('needle'), NO_COLOR) == 0
    out = capsys.readouterr().out
    assert 'T-claude:' in out
    assert 'T-bro:' in out

  def test_line_name_is_the_whole_trail_id(self, capsys):
    client = FakeClient()
    client.add_claude(LULID, [_user('the needle is here')])
    assert _command_grep(_cast(client), self._args('needle'), NO_COLOR) == 0
    assert f'{LULID}:' in capsys.readouterr().out

  def test_no_match_exits_1(self, capsys):
    client = FakeClient()
    client.add_claude('T1', [_user('nothing to see')])
    assert _command_grep(_cast(client), self._args('absent-pattern'), NO_COLOR) == 1
    del capsys

  def test_explicit_unknown_id_propagates_not_found(self):
    client = FakeClient()
    args = self._args('needle', trails=['b2249daa'])
    with pytest.raises(HTTPStatusError, match='trail not found'):
      _command_grep(_cast(client), args, NO_COLOR)


class TestFollowSteps:
  def test_stops_at_the_end_step(self):
    client = MagicMock()
    client.iter_steps.return_value = iter(
      [
        {'step_id': 'S1', 'kind': 'assistant'},
        {'step_id': 'S2', 'kind': 'end'},
        {'step_id': 'S3', 'kind': 'assistant'},
      ]
    )
    rows = list(_follow_steps(_cast(client), 'T1', interval=0, sleep=lambda _: None))
    assert [row['step_id'] for row in rows] == ['S1', 'S2']

  def test_header_end_terminates_a_stream_without_end_steps(self):
    client = FakeClient()
    client.add_claude('T1', [_user('hello')], end={'at': 'x', 'reason': 'ok'})
    rows = list(_follow_steps(_cast(client), 'T1', interval=0, sleep=lambda _: None))
    assert [row['step_id'] for row in rows] == ['0']


class TestRenderTree:
  def test_renders_children_and_highlight(self):
    client = FakeClient()
    client.add_bro('TROOT', [])
    client.add_claude(LULID, [_user('x')], forked_from={'trail_id': 'TROOT', 'step_id': 'S1'})
    lines: list[str] = []
    _render_tree(
      _cast(client),
      client.get_trail('TROOT'),
      '',
      is_last=True,
      lines=lines,
      colors=NO_COLOR,
      highlight=LULID,
    )
    joined = '\n'.join(lines)
    assert 'TROOT' in joined
    assert LULID in joined
    assert 'ws/claude' in joined  # claude nodes label by workspace
    assert '<-- here' in joined


class TestWithDefaultCommand:
  def test_bare_id_gets_show(self):
    assert _with_default_command(['rewind', 'b2249daa']) == ['rewind', 'show', 'b2249daa']

  def test_explicit_commands_kept(self):
    assert _with_default_command(['rewind', 'list']) == ['rewind', 'list']
    assert _with_default_command(['rewind', 'grep', 'p']) == ['rewind', 'grep', 'p']

  def test_no_args_and_help_kept(self):
    assert _with_default_command(['rewind']) == ['rewind']
    assert _with_default_command(['rewind', '--help']) == ['rewind', '--help']

  def test_leading_flag_gets_show(self):
    argv = ['rewind', '--color=never', 'b2249daa']
    assert _with_default_command(argv) == ['rewind', 'show', '--color=never', 'b2249daa']


@pytest.mark.parametrize(
  'body,expected',
  [
    ({'reason': 'terminal'}, 'ok'),
    ({'reason': 'raised', 'detail': 'x'}, 'raised'),
  ],
)
def test_end_reason_mapping_only_touches_terminal(body, expected):
  out = _format_step_summary(
    {'step_id': 'S', 'kind': 'end', 'body': body, 'ts': None},
    NO_COLOR,
  )
  assert f'"reason": "{expected}"' in out
