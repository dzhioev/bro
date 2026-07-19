import asyncio
from unittest.mock import MagicMock, patch

import pytest

from llm.tracker import HTTPStatusError
from trails import cli
from trails.cli import (
  _Colors,
  _follow_steps,
  _format_step_summary,
  _format_trail_header,
  _format_trail_row,
  _render_tree,
  _truncate_oneline,
)

NO_COLOR = _Colors(enabled=False)


class TestTruncateOneline:
  def test_short_string_passes_through(self):
    assert _truncate_oneline('hello') == 'hello'

  def test_none_renders_empty(self):
    assert _truncate_oneline(None) == ''

  def test_strings_flatten_newlines(self):
    assert _truncate_oneline('line\nline') == 'line line'

  def test_dict_renders_as_json(self):
    out = _truncate_oneline({'a': 1})
    assert out == '{"a": 1}'

  def test_long_string_truncates_with_marker(self):
    s = 'x' * 300
    out = _truncate_oneline(s, limit=100)
    assert out.startswith('x' * 100)
    assert '... <200 more chars>' in out

  def test_long_dict_truncates(self):
    body = {'k': 'v' * 1000}
    out = _truncate_oneline(body, limit=50)
    assert 'more chars>' in out


class TestFormatStepSummary:
  def test_inline_body_appears_inline(self):
    out = _format_step_summary(
      {
        'step_id': '01ARZ3NDEKTSV4RRFFQ69G5FAV',
        'kind': 'user_input',
        'body': 'hello',
        'ts': '2026-06-07T00:00:00.000000Z',
        'turn_index': 0,
      },
      NO_COLOR,
    )
    assert 'user_input' in out
    assert 'hello' in out
    assert 't0' in out

  def test_step_id_leads_the_line(self):
    out = _format_step_summary(
      {
        'step_id': '01ARZ3NDEKTSV4RRFFQ69G5FAV',
        'kind': 'llm_call',
        'body': None,
        'ts': '2026-06-07T00:00:00.000000Z',
        'turn_index': 1,
      },
      NO_COLOR,
    )
    assert out.startswith('01ARZ3NDEKTSV4RRFFQ69G5FAV  ')

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

  def test_tool_call_extras_render(self):
    out = _format_step_summary(
      {
        'kind': 'tool_call',
        'body': None,
        'ts': '2026-06-07T00:00:00.000000Z',
        'turn_index': 2,
        'tool_name': 'add_task',
        'call_id': 'c1',
        'arguments': {'name': 'x'},
      },
      NO_COLOR,
    )
    assert 'tool_name=add_task' in out
    assert 'call_id=c1' in out
    assert 'args=' in out

  def test_llm_call_token_extras_render(self):
    out = _format_step_summary(
      {
        'kind': 'llm_call',
        'body': {'request': {}, 'response': {}},
        'ts': '2026-06-07T00:00:00.000000Z',
        'turn_index': 1,
        'response_id': 'resp_abc',
        'tokens_in': 12,
        'tokens_out': 8,
        'tokens_reasoning': 4,
        'tokens_cached': 6,
      },
      NO_COLOR,
    )
    assert 'response_id=resp_abc' in out
    assert 'tokens_in=12' in out
    assert 'tokens_out=8' in out
    assert 'tokens_reasoning=4' in out
    assert 'tokens_cached=6' in out


class TestFormatTrailRow:
  def test_live_trail(self):
    out = _format_trail_row(
      {
        'trail_id': 'T1234567890ABCDEF',
        'bro': 'dev',
        'llm_spec': {'model': 'gpt-5'},
        'started_at': '2026-06-07T22:14:03.000000Z',
        'ended_at': None,
        'end_reason': None,
        'parent': None,
      },
      NO_COLOR,
    )
    assert 'T1234567890ABCDEF' in out  # full id surfaced so user can copy
    assert 'gpt-5' in out
    assert 'live' in out
    assert 'fork-of' not in out

  def test_done_trail_with_parent(self):
    out = _format_trail_row(
      {
        'trail_id': 'T-child',
        'bro': 'dev',
        'llm_spec': {'model': 'gpt-5'},
        'started_at': '2026-06-07T22:14:03.000000Z',
        'ended_at': '2026-06-07T22:15:00.000000Z',
        'end_reason': 'terminal',
        'parent': {'trail_id': 'T-parent-xyz'},
      },
      NO_COLOR,
    )
    assert 'done:terminal' in out
    assert 'fork-of T-parent-xyz' in out

  def test_lost_trail(self):
    out = _format_trail_row(
      {
        'trail_id': 'T-lost',
        'bro': 'dev',
        'llm_spec': {'model': 'gpt-5'},
        'started_at': '2026-06-07T22:14:03.000000Z',
        'ended_at': '2026-06-07T22:15:00.000000Z',
        'end_reason': 'lost',
        'parent': None,
      },
      NO_COLOR,
    )
    assert 'lost' in out
    assert 'done:' not in out


class TestFormatTrailHeader:
  def test_render_includes_aggregates_and_parent(self):
    out = _format_trail_header(
      {
        'trail_id': 'T1',
        'bro': 'dev',
        'bro_version': 1,
        'llm_spec': {'type': 'chat_gpt', 'model': 'gpt-5'},
        'started_at': '2026-06-07T22:14:03.000000Z',
        'ended_at': '2026-06-07T22:15:00.000000Z',
        'end_reason': 'terminal',
        'interactive': False,
        'entry_point': 'cli:bro_run',
        'parent': {'trail_id': 'T-p', 'step_id': 'S5', 'relationship': 'fork'},
        'summoner': {'target': 'pm', 'trail_id': 'T-summoner'},
        'continuation': {'provider': 'openai', 'response_id': 'r1'},
        'aggregates': {
          'turn_count': 3,
          'tool_call_count': 2,
          'tokens_in': 100,
          'tokens_out': 50,
          'tokens_reasoning': 25,
          'step_counts_by_kind': {'reasoning': 3, 'tool_call': 2, 'user_input': 1, 'end': 0},
        },
      },
      NO_COLOR,
    )
    assert 'T1' in out
    assert 'gpt-5' in out
    assert 'turns=3' in out
    assert 'tokens_in=100' in out
    assert 'fork T-p' in out
    assert 'summoner   {"target": "pm", "trail_id": "T-summoner"}' in out
    assert 'reasoning=3' in out
    assert 'end=0' not in out  # zero counts pruned


class TestRenderTree:
  def test_single_node(self):
    client = MagicMock()
    client.iter_trails.return_value = iter([])
    lines: list[str] = []
    _render_tree(
      client,
      {'trail_id': 'TROOT', 'bro': 'dev', 'llm_spec': {'model': 'gpt-5'}, 'parent': None},
      '',
      is_last=True,
      lines=lines,
      colors=NO_COLOR,
      highlight='TROOT',
    )
    assert len(lines) == 1
    assert 'TROOT' in lines[0]
    assert '<-- here' in lines[0]

  def test_renders_children_and_highlight(self):
    client = MagicMock()

    def fake_iter(*, parent):
      if parent == 'TROOT':
        return iter(
          [
            {
              'trail_id': 'T-a',
              'bro': 'dev',
              'llm_spec': {'model': 'gpt-5'},
              'parent': {'trail_id': 'TROOT', 'step_id': 'S1'},
            },
            {
              'trail_id': 'T-b',
              'bro': 'dev',
              'llm_spec': {'model': 'gpt-5'},
              'parent': {'trail_id': 'TROOT', 'step_id': 'S2'},
            },
          ]
        )
      return iter([])

    client.iter_trails.side_effect = fake_iter
    lines: list[str] = []
    _render_tree(
      client,
      {'trail_id': 'TROOT', 'bro': 'dev', 'llm_spec': {'model': 'gpt-5'}, 'parent': None},
      '',
      is_last=True,
      lines=lines,
      colors=NO_COLOR,
      highlight='T-a',
    )
    assert 'TROOT' in lines[0]
    assert '<-- here' in '\n'.join(lines)
    assert any('T-a' in line for line in lines)
    assert any('T-b' in line for line in lines)


class TestForkRepl:
  def test_initial_message_sent_then_input_loop(self):
    bro = MagicMock()
    bro.name = 'dev'

    async def fake_send(msg: str) -> str:
      return f'reply-to-{msg}'

    bro.send.side_effect = fake_send

    messages = iter(['second', 'third'])

    def read():
      try:
        return next(messages)
      except StopIteration:
        raise EOFError

    emitted: list[str] = []
    asyncio.run(cli._fork_repl(bro, 'first', read_line=read, emit=emitted.append))
    assert emitted == ['reply-to-first', 'reply-to-second', 'reply-to-third']

  def test_skips_initial_when_empty(self):
    bro = MagicMock()
    bro.name = 'dev'

    async def fake_send(msg: str) -> str:
      return f'reply-to-{msg}'

    bro.send.side_effect = fake_send

    def read():
      raise EOFError

    emitted: list[str] = []
    asyncio.run(cli._fork_repl(bro, None, read_line=read, emit=emitted.append))
    assert emitted == []

  def test_blank_lines_skipped(self):
    bro = MagicMock()
    bro.name = 'dev'

    async def fake_send(msg: str) -> str:
      return f'reply-to-{msg}'

    bro.send.side_effect = fake_send

    messages = iter(['', '  ', 'real'])

    def read():
      try:
        return next(messages)
      except StopIteration:
        raise EOFError

    emitted: list[str] = []
    asyncio.run(cli._fork_repl(bro, None, read_line=read, emit=emitted.append))
    # blank '' is skipped; non-empty '  ' goes through (truthy len)
    assert emitted == ['reply-to-  ', 'reply-to-real']


class TestFollowSteps:
  def test_stops_at_end_step_without_polling_again(self):
    client = MagicMock()
    client.iter_steps.return_value = iter(
      [{'step_id': 's1', 'kind': 'user_input'}, {'step_id': 's2', 'kind': 'end'}]
    )
    sleeps: list[float] = []
    rows = list(_follow_steps(client, 'T1', interval=2.0, sleep=sleeps.append))
    assert [r['step_id'] for r in rows] == ['s1', 's2']
    assert sleeps == []
    client.get_trail.assert_not_called()

  def test_polls_from_the_last_seen_step(self):
    client = MagicMock()
    client.iter_steps.side_effect = [
      iter([{'step_id': 's1', 'kind': 'user_input'}]),
      iter([]),
      iter([{'step_id': 's2', 'kind': 'end'}]),
    ]
    client.get_trail.return_value = {'ended_at': None}
    sleeps: list[float] = []
    rows = list(_follow_steps(client, 'T1', interval=1.5, sleep=sleeps.append))
    assert [r['step_id'] for r in rows] == ['s1', 's2']
    assert sleeps == [1.5, 1.5]
    afters = [call.kwargs['after'] for call in client.iter_steps.call_args_list]
    assert afters == [None, 's1', 's1']

  def test_ended_header_terminates_a_trail_without_end_step(self):
    client = MagicMock()
    client.iter_steps.side_effect = [
      iter([{'step_id': 's1', 'kind': 'user_input'}]),
      iter([]),
      iter([]),
    ]
    client.get_trail.side_effect = [
      {'ended_at': None},
      {'ended_at': '2026-06-07T00:00:01.000000Z'},
    ]
    sleeps: list[float] = []
    rows = list(_follow_steps(client, 'T1', interval=1.0, sleep=sleeps.append))
    assert [r['step_id'] for r in rows] == ['s1']
    assert sleeps == [1.0]

  def test_ended_header_drains_steps_landed_after_the_poll(self):
    client = MagicMock()
    client.iter_steps.side_effect = [
      iter([]),
      iter([{'step_id': 's9', 'kind': 'end'}]),
    ]
    client.get_trail.return_value = {'ended_at': '2026-06-07T00:00:01.000000Z'}
    rows = list(_follow_steps(client, 'T1', interval=1.0, sleep=lambda _: None))
    assert [r['step_id'] for r in rows] == ['s9']

  def test_transient_error_retried_on_next_tick(self):
    client = MagicMock()
    client.iter_steps.side_effect = [
      ConnectionError('blip'),
      iter([{'step_id': 's1', 'kind': 'end'}]),
    ]
    sleeps: list[float] = []
    rows = list(_follow_steps(client, 'T1', interval=1.0, sleep=sleeps.append))
    assert [r['step_id'] for r in rows] == ['s1']
    assert sleeps == [1.0]

  def test_retryable_http_status_retried_on_next_tick(self):
    client = MagicMock()
    client.iter_steps.side_effect = [
      HTTPStatusError(503, 'unavailable'),
      iter([{'step_id': 's1', 'kind': 'end'}]),
    ]
    rows = list(_follow_steps(client, 'T1', interval=1.0, sleep=lambda _: None))
    assert [r['step_id'] for r in rows] == ['s1']

  def test_deterministic_http_status_propagates(self):
    client = MagicMock()
    client.iter_steps.side_effect = HTTPStatusError(404, 'not found')
    with pytest.raises(HTTPStatusError):
      list(_follow_steps(client, 'T1', interval=1.0, sleep=lambda _: None))


class TestCmdShowFollow:
  def test_streams_header_then_steps_and_exits_on_end(self, capsys):
    client = MagicMock()
    client.get_trail.return_value = {
      'trail_id': 'T1',
      'bro': 'dev',
      'bro_version': 1,
      'llm_spec': {'model': 'gpt-5'},
      'started_at': '2026-06-07T22:14:03.000000Z',
      'ended_at': None,
      'end_reason': None,
      'interactive': False,
      'entry_point': 'cli:bro_run',
      'parent': None,
      'aggregates': {},
    }
    client.iter_steps.return_value = iter(
      [
        {
          'step_id': 'S1',
          'kind': 'user_input',
          'body': 'hi',
          'ts': '2026-06-07T22:14:04.000000Z',
        },
        {
          'step_id': 'S2',
          'kind': 'end',
          'body': {'reason': 'terminal'},
          'ts': '2026-06-07T22:14:05.000000Z',
        },
      ]
    )
    rc = cli._command_show(client, {'trail_id': 'T1', 'follow': True, 'interval': 2.0}, NO_COLOR)
    assert rc == 0
    out = capsys.readouterr().out
    assert 'T1' in out
    assert 'user_input' in out
    assert '"reason": "terminal"' in out


class TestCmdList:
  def test_no_trails_returns_zero(self, capsys):
    client = MagicMock()
    client.iter_trails.return_value = iter([])
    rc = cli._command_list(
      client, {'bro': None, 'parent': None, 'since': None, 'until': None, 'limit': 10}, NO_COLOR
    )
    assert rc == 0
    error = capsys.readouterr().err
    assert '(no trails)' in error


class TestMain:
  def test_no_command_prints_help(self, capsys):
    with patch('trails.cli.default_client') as default_client:
      rc = cli.main(['trails'])
    assert rc == 1
    # default_client must not be called when no subcommand is given
    default_client.assert_not_called()

  def test_list_dispatches(self, capsys):
    with patch('trails.cli.default_client') as default_client:
      fake = MagicMock()
      fake.iter_trails.return_value = iter([])
      default_client.return_value = fake
      rc = cli.main(['trails', 'list', '--no-pager'])
    assert rc == 0
