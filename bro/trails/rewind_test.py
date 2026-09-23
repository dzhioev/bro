import contextlib
import io
import json
from typing import Any, Optional, cast

import pytest

import bro.trails.rewind as rewind
from bro.trails.backends import BACKENDS
from bro.trails.display import ColorMode, preset
from bro.trails.rewind import (
  _command_grep,
  _command_list,
  _command_show,
  _command_steps,
  _command_tree,
  _emit_document,
  _follow_batches,
  _with_default_command,
)
from bro.trails.store import TrailNotFound, TrailsStore

LULID = '01kydtgppz-y7fdwep2-apw9ag3b'


class FakeClient:
  """In-memory stand-in for the TrailsStore read surface rewind drives."""

  def __init__(self):
    self.trails: dict[str, dict[str, Any]] = {}
    self.records: dict[str, list[str]] = {}
    self.steps: dict[str, list[dict[str, Any]]] = {}
    self.contexts: dict[str, Any] = {}

  def add_claude(self, trail_id: str, lines: list[str], **header: Any) -> None:
    self.trails[trail_id] = {
      'id': trail_id,
      'harness': 'claude',
      'bro': 'dev',
      'version': '2',
      'started_at': '2026-01-01T00:00:00Z',
      'end': None,
      'interactive': True,
      'surface': 'ride',
      'turn_count': 0,
      'native': {
        'segment': 'segment',
        'llm': {'model': 'claude'},
        'harness_version': '2.1.0',
      },
      'location': {'workspace': 'ws'},
      'usage': {},
      'models': ['claude'],
      'extent': len(lines),
      **header,
    }
    self.records[trail_id] = lines

  def add_bro(self, trail_id: str, steps: list[dict[str, Any]], **header: Any) -> None:
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
      'models': ['gpt-5'],
      'extent': len(steps),
      **header,
    }
    self.steps[trail_id] = steps

  def get_trail(self, trail_id: str) -> dict[str, Any]:
    if trail_id not in self.trails:
      raise TrailNotFound(trail_id)
    return self.trails[trail_id]

  def iter_steps(self, trail_id: str, *, after: Optional[int] = None):
    if trail_id in self.steps:
      for row in self.steps[trail_id]:
        if after is None or row['step_id'] > after:
          yield row
      return
    for index, raw in enumerate(self.records[trail_id]):
      if after is not None and index <= after:
        continue
      yield {'step_id': index, 'ts': None, 'body': raw}

  def iter_messages(self, trail_id: str, *, after: Optional[int] = None):
    harness = self.trails[trail_id]['harness']
    for row in self.iter_steps(trail_id, after=after):
      yield from BACKENDS[harness].project(row)

  def collect_steps(self, trail_id: str, *, extent: int) -> list[dict[str, Any]]:
    return [row for row in self.iter_steps(trail_id) if row['step_id'] < extent]

  def collect_messages(
    self, trail_id: str, *, extent: int, types: Optional[set[str]] = None
  ) -> list[dict[str, Any]]:
    return [
      message
      for message in self.iter_messages(trail_id)
      if message['source']['step_id'] < extent and (types is None or message['type'] in types)
    ]

  def iter_trails(self, **filters: Any):
    selected = []
    for trail in self.trails.values():
      forked_from = filters.get('forked_from')
      if forked_from is not None:
        if trail.get('forked_from', {}).get('trail_id') != forked_from:
          continue
      elif filters.get('harness') is not None and trail['harness'] != filters['harness']:
        continue
      elif filters.get('bro') is not None and trail.get('bro') != filters['bro']:
        continue
      selected.append(trail)
    limit = filters.get('max_items')
    yield from selected[: limit if limit is not None else len(selected)]

  def get_launch_context(self, trail_id: str) -> Any:
    return self.contexts.get(trail_id)


def _client(fake: FakeClient) -> TrailsStore:
  return cast(TrailsStore, fake)


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
      'message': {
        'id': f'm-{uuid}',
        'model': 'claude',
        'usage': {},
        'content': [{'type': 'text', 'text': text}],
      },
    }
  )


def _attachment(attachment: dict[str, Any], uuid: str = 'x1') -> str:
  return json.dumps({'type': 'attachment', 'uuid': uuid, 'attachment': attachment})


def _args(trail_id: str, **changes: Any) -> dict[str, Any]:
  return {
    'trail_id': trail_id,
    'color': 'never',
    'no_pager': True,
    'follow': False,
    'interval': 0,
    **changes,
  }


class TestShow:
  def test_renders_conversation_context_and_header_through_the_shared_path(self, capsys):
    client = FakeClient()
    client.add_claude('T1', [_user('hello'), _assistant('hi there')])
    client.contexts['T1'] = [{'title': 'git state', 'fields': {'branch': 'feature'}}]

    assert _command_show(_client(client), _args('T1')) == 0

    output = capsys.readouterr().out
    assert 'trail' in output and 'T1' in output
    assert 'SESSION CONTEXT' in output
    assert '▸ git state' in output
    assert '#1 USER' in output and 'hello' in output
    assert '#2 ASSISTANT' in output and 'hi there' in output

  def test_walks_the_fork_chain_through_the_exact_parent_anchor(self, capsys):
    client = FakeClient()
    client.add_claude('T1', [_user('hello'), _assistant('anchor'), _user('discarded')])
    client.add_claude(
      'T2',
      [_user('resumed')],
      forked_from={'trail_id': 'T1', 'step_id': 1, 'index': 1},
    )

    assert _command_show(_client(client), _args('T2')) == 0

    output = capsys.readouterr().out
    assert 'hello' in output and 'anchor' in output and 'resumed' in output
    assert 'discarded' not in output
    assert '── resumed as trail T2' in output

  def test_tool_results_are_retained_under_their_calls(self, capsys):
    call = json.dumps(
      {
        'type': 'assistant',
        'uuid': 'a1',
        'message': {
          'id': 'm1',
          'model': 'claude',
          'usage': {},
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

    assert _command_show(_client(client), _args('T1')) == 0

    output = capsys.readouterr().out
    assert '→ bash {cmd: ls}' in output
    assert 'file.txt' in output

  def test_renders_notification_steps_as_notices(self, capsys):
    client = FakeClient()
    client.add_bro(
      'T1',
      [
        {
          'step_id': 0,
          'kind': 'notification',
          'body': '[notification: a background job reported]\njob output',
          'ts': None,
        }
      ],
    )

    assert _command_show(_client(client), _args('T1')) == 0

    output = capsys.readouterr().out
    assert '[notification: a background job reported]' in output
    assert 'job output' in output
    assert '#1 USER' not in output

  def test_renders_claudes_attachments_as_notices_named_by_their_type(self, capsys):
    client = FakeClient()
    client.add_claude(
      'T1',
      [
        _attachment({'type': 'hook_additional_context', 'content': ['the watched lines']}),
        _attachment({'type': 'total_tokens_reminder', 'text': 'budget left'}),
      ],
    )

    assert _command_show(_client(client), _args('T1')) == 0

    output = capsys.readouterr().out
    assert 'NOTICE · hook_additional_context' in output
    assert 'the watched lines' in output
    assert 'NOTICE · total_tokens_reminder' in output
    assert 'budget left' in output

  def test_a_task_notification_is_a_notice_whether_it_opened_a_turn_or_was_queued(self, capsys):
    opening = json.dumps(
      {
        'type': 'user',
        'uuid': 'u1',
        'origin': {'kind': 'task-notification'},
        'message': {'content': 'the first task ended'},
      }
    )
    queued = _attachment(
      {
        'type': 'queued_command',
        'prompt': 'the second task ended',
        'commandMode': 'task-notification',
      }
    )
    client = FakeClient()
    client.add_claude('T1', [opening, queued])

    assert _command_show(_client(client), _args('T1')) == 0

    output = capsys.readouterr().out
    assert output.count('NOTICE · task-notification') == 2
    assert 'the first task ended' in output and 'the second task ended' in output
    assert 'USER' not in output

  def test_a_prompt_typed_mid_turn_is_the_users(self, capsys):
    queued = _attachment(
      {
        'type': 'queued_command',
        'prompt': 'also check the docs',
        'commandMode': 'prompt',
        'origin': {'kind': 'human'},
      }
    )
    client = FakeClient()
    client.add_claude('T1', [queued])

    assert _command_show(_client(client), _args('T1')) == 0

    output = capsys.readouterr().out
    assert '#1 USER' in output and 'also check the docs' in output

  def test_unknown_id_propagates_not_found(self):
    with pytest.raises(TrailNotFound, match='trail not found'):
      _command_show(_client(FakeClient()), _args('missing'))


class TestSteps:
  def test_native_view_keeps_step_ids_bodies_and_extras(self, capsys):
    client = FakeClient()
    client.add_bro(
      'T1',
      [
        {
          'step_id': 0,
          'kind': 'llm_call',
          'body': {'response': {'id': 'r1'}},
          'response_id': 'r1',
          'ts': '2026-01-01T00:00:00Z',
        }
      ],
    )

    assert _command_steps(_client(client), _args('T1')) == 0

    output = capsys.readouterr().out
    assert '0  ' in output
    assert 'llm_call' in output
    assert '{response: {id: r1}}' in output
    assert 'response_id=r1' in output

  def test_prints_every_record_whole(self, capsys):
    padding = 'x' * 5000
    client = FakeClient()
    client.add_claude('T1', [_user(f'{padding} the end of the body')])
    client.add_bro(
      'T2',
      [
        {
          'step_id': 0,
          'kind': 'tool_result',
          'body': 'ok',
          'tool_name': f'{padding} the end of the attribute',
          'ts': None,
        }
      ],
    )

    assert _command_steps(_client(client), _args('T1')) == 0
    assert _command_steps(_client(client), _args('T2')) == 0

    output = capsys.readouterr().out
    assert 'the end of the body' in output
    assert 'the end of the attribute' in output
    assert 'more chars' not in output

  def test_spilled_body_is_an_omission_marker_and_is_not_fetched(self, capsys):
    client = FakeClient()
    client.add_bro(
      'T1',
      [
        {
          'step_id': 0,
          'kind': 'llm_call',
          'body': {'s3': 'key', 'url': 'https://example.test/body', 'size': 4096},
          'ts': None,
        }
      ],
    )

    assert _command_steps(_client(client), _args('T1')) == 0

    output = capsys.readouterr().out
    assert '<4096 bytes spilled> https://example.test/body' in output


class TestListAndTree:
  def test_list_uses_structural_rows_for_status_owner_subject_and_fork(self, capsys):
    client = FakeClient()
    client.add_bro('T1', [], subject='root')
    client.add_claude(
      'T2',
      [],
      end={'at': '2026-01-01T00:01:00Z', 'reason': 'ok'},
      subject='child',
      forked_from={'trail_id': 'T1', 'step_id': 0},
    )
    args = {
      'color': 'never',
      'no_pager': True,
      'harness': None,
      'bro': None,
      'forked_from': None,
      'since': None,
      'until': None,
      'limit': 50,
    }

    assert _command_list(_client(client), args) == 0

    output = capsys.readouterr().out
    assert 'T1' in output and 'live' in output and 'root' in output
    assert 'T2' in output and 'done:ok' in output and 'fork-of T1' in output
    assert 'ws' in output

  def test_tree_uses_lineage_records_and_highlights_the_selected_node(self, capsys):
    client = FakeClient()
    client.add_bro('TROOT', [])
    client.add_claude(LULID, [], forked_from={'trail_id': 'TROOT', 'step_id': 0})

    assert _command_tree(_client(client), {'trail_id': LULID, 'color': 'never'}) == 0

    output = capsys.readouterr().out
    assert 'TROOT' in output and LULID in output
    assert 'ws/claude' in output
    assert '<-- here' in output


class TestGrep:
  def _grep_args(self, pattern: str, **changes: Any) -> dict[str, Any]:
    return {
      'pattern': pattern,
      'trails': [],
      'color': 'never',
      'context': None,
      'after_context': None,
      'before_context': None,
      **changes,
    }

  def test_matches_the_plain_rewind_show_document_across_harnesses(self, capsys):
    client = FakeClient()
    client.add_claude('T-claude', [_user('the needle is here')])
    client.add_bro(
      'T-bro', [{'step_id': 0, 'kind': 'user_input', 'body': 'needle too', 'ts': None}]
    )

    assert _command_grep(_client(client), self._grep_args('needle')) == 0

    output = capsys.readouterr().out
    assert 'T-claude:' in output and 'T-bro:' in output
    assert '\x1b[' not in output

  def test_matches_notification_output(self, capsys):
    client = FakeClient()
    client.add_bro(
      'T-bro',
      [
        {
          'step_id': 0,
          'kind': 'notification',
          'body': '[notification: a background job reported]\nthe watch needle',
          'ts': None,
        }
      ],
    )

    assert _command_grep(_client(client), self._grep_args('watch needle')) == 0
    assert 'T-bro:' in capsys.readouterr().out

  def test_searches_the_same_fork_chain_and_keeps_the_whole_trail_id(self, capsys):
    client = FakeClient()
    client.add_claude('parent', [_user('parent needle'), _assistant('anchor')])
    client.add_claude(
      LULID,
      [_user('child')],
      forked_from={'trail_id': 'parent', 'step_id': 1},
    )

    assert _command_grep(_client(client), self._grep_args('parent needle', trails=[LULID])) == 0
    assert f'{LULID}:' in capsys.readouterr().out

  def test_no_match_exits_one(self):
    client = FakeClient()
    client.add_claude('T1', [_user('nothing')])
    assert _command_grep(_client(client), self._grep_args('absent')) == 1

  def test_explicit_unknown_id_propagates_not_found(self):
    with pytest.raises(TrailNotFound, match='trail not found'):
      _command_grep(_client(FakeClient()), self._grep_args('needle', trails=['missing']))

  def test_an_unrenderable_trail_is_named_and_the_rest_of_the_sweep_still_reports(self, capsys):
    client = FakeClient()
    client.add_bro('T-broken', [{'step_id': 0, 'kind': 'user_input', 'body': 17, 'ts': None}])
    client.add_claude('T-good', [_user('the needle is here')])

    assert _command_grep(_client(client), self._grep_args('needle')) == 2

    captured = capsys.readouterr()
    assert 'T-good:' in captured.out
    assert 'T-broken' in captured.err and 'user input' in captured.err

  def test_a_structured_error_body_is_searchable(self, capsys):
    client = FakeClient()
    client.add_bro(
      'T-bro',
      [
        {
          'step_id': 0,
          'kind': 'error',
          'ts': None,
          'body': {'message': 'overloaded', 'traceback': 'File "openai.py", line 652, in send'},
        }
      ],
    )

    assert _command_grep(_client(client), self._grep_args('line 652')) == 0
    assert 'T-bro:' in capsys.readouterr().out


class FollowClient(FakeClient):
  def __init__(self):
    super().__init__()
    self.add_bro('T1', [], extent=1)
    self.result_sent = False

  def iter_messages(self, trail_id: str, *, after: Optional[int] = None):
    del trail_id
    if after is None:
      yield {
        'type': 'tool_call',
        'ts': None,
        'source': {'step_id': 0, 'index': 0},
        'call_id': 'call',
        'tool_name': 'read',
        'arguments': {},
      }
    elif after == 0 and not self.result_sent:
      self.result_sent = True
      yield {
        'type': 'tool_result',
        'ts': None,
        'source': {'step_id': 1, 'index': 0},
        'call_id': 'call',
        'content': 'followed result',
      }

  def get_trail(self, trail_id: str) -> dict[str, Any]:
    header = super().get_trail(trail_id)
    if self.result_sent:
      return {**header, 'end': {'at': '2026-01-01T00:01:00Z', 'reason': 'ok'}}
    return header


class TestFollow:
  def test_show_follow_streams_late_result_continuations_without_cursor_updates(self, capsys):
    client = FollowClient()

    assert _command_show(_client(client), _args('T1', follow=True)) == 0

    output = capsys.readouterr().out
    assert '→ read\n' in output
    assert '← followed result' in output
    assert '\x1b[' not in output

  def test_header_completion_drains_a_message_stream(self):
    class DrainClient(FakeClient):
      def __init__(self):
        super().__init__()
        self.add_bro('T1', [], end={'at': 'x', 'reason': 'ok'})
        self.queries = 0

      def iter_messages(self, trail_id: str, *, after: Optional[int] = None):
        del trail_id, after
        self.queries += 1
        if self.queries == 2:
          yield {'type': 'assistant', 'source': {'step_id': 3}}

    client = DrainClient()
    batches = list(
      _follow_batches(
        _client(client),
        'T1',
        iterator=lambda trail_id, after: client.iter_messages(trail_id, after=after),
        cursor=lambda message: message['source']['step_id'],
        interval=0,
        sleep=lambda _: None,
      )
    )
    assert [message['source']['step_id'] for batch in batches for message in batch] == [3]


class TTYBuffer(io.StringIO):
  def isatty(self) -> bool:
    return True


class TestCapabilities:
  def test_pager_is_only_used_for_a_finite_retained_tty_document(self, monkeypatch):
    target = TTYBuffer()
    paged: list[str] = []
    monkeypatch.setattr(rewind.sys, 'stdout', target)
    monkeypatch.setattr(rewind.pager, 'page', paged.append)
    configuration = preset('rewind-show', color=ColorMode.NEVER)

    _emit_document('finite', {'no_pager': False, 'follow': False}, configuration)
    _emit_document('follow', {'no_pager': False, 'follow': True}, configuration)

    assert paged == ['finite']
    assert target.getvalue() == 'follow'

  @pytest.mark.parametrize(
    'command',
    [['list'], ['show', 'T1'], ['steps', 'T1'], ['grep', 'hello', 'T1'], ['tree', 'T1']],
  )
  def test_every_command_pages_a_tty_document_that_no_pager_prints(self, monkeypatch, command):
    client = FakeClient()
    client.add_claude('T1', [_user('hello')])
    monkeypatch.setattr(rewind, 'default_store', lambda: contextlib.nullcontext(_client(client)))
    paged: list[str] = []
    monkeypatch.setattr(rewind.pager, 'page', paged.append)
    target = TTYBuffer()
    monkeypatch.setattr(rewind.sys, 'stdout', target)

    assert rewind.main(['rewind', *command]) == 0
    assert target.getvalue() == ''
    assert rewind.main(['rewind', *command, '--no-pager']) == 0

    assert 'T1' in target.getvalue()
    assert paged == [target.getvalue()]

  def test_rewind_no_longer_owns_trail_formatting_helpers(self):
    for name in (
      '_ConversationTimeline',
      '_format_header',
      '_format_step_summary',
      '_format_trail_row',
      '_render_tree',
    ):
      assert not hasattr(rewind, name)


class TestWithDefaultCommand:
  def test_bare_id_gets_show(self):
    assert _with_default_command(['rewind', 'trail']) == ['rewind', 'show', 'trail']

  def test_explicit_commands_and_help_are_kept(self):
    assert _with_default_command(['rewind', 'list']) == ['rewind', 'list']
    assert _with_default_command(['rewind', 'grep', 'p']) == ['rewind', 'grep', 'p']
    assert _with_default_command(['rewind', '--help']) == ['rewind', '--help']

  def test_leading_flag_gets_show(self):
    argv = ['rewind', '--color=never', 'trail']
    assert _with_default_command(argv) == ['rewind', 'show', '--color=never', 'trail']
