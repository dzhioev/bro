import io
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import cw.listen
from cw.listen import (
  Finding,
  NarratedActivity,
  Turn,
  _fabricated_calls,
  _final_turn,
  _mimics_transcript,
  _needs_audit,
  _synchronized_turn,
  listen,
)


def _user_prompt(text: str = 'do it', **extra) -> dict:
  return {'type': 'user', 'message': {'role': 'user', 'content': text}, **extra}


def _tool_result(**extra) -> dict:
  content = [{'type': 'tool_result', 'tool_use_id': 'call-1', 'content': 'ok'}]
  return {'type': 'user', 'message': {'role': 'user', 'content': content}, **extra}


def _assistant(blocks: list[dict], model: str = 'claude-fable-5', **extra) -> dict:
  return {'type': 'assistant', 'message': {'model': model, 'content': blocks}, **extra}


def _text(text: str) -> dict:
  return {'type': 'text', 'text': text}


def _tool_use(name: str) -> dict:
  return {'type': 'tool_use', 'name': name, 'input': {}}


def _write(tmp_path: Path, entries: list[dict]) -> Path:
  path = tmp_path / 'transcript.jsonl'
  path.write_text(''.join(json.dumps(entry) + '\n' for entry in entries))
  return path


class TestFinalTurn:
  def test_collects_text_and_tool_calls_since_last_prompt(self):
    turn = _final_turn(
      [
        _user_prompt(),
        _assistant([_text('looking')]),
        _assistant([_tool_use('mcp__flow__get_task_info')]),
        _tool_result(),
        _assistant([_text('done')]),
      ]
    )
    assert turn.text == 'looking\n\ndone'
    assert turn.tool_calls == ['mcp__flow__get_task_info']

  def test_earlier_turns_excluded(self):
    turn = _final_turn(
      [
        _user_prompt('first'),
        _assistant([_text('old answer')]),
        _user_prompt('second'),
        _assistant([_text('new answer')]),
      ]
    )
    assert turn.text == 'new answer'

  def test_text_block_prompt_is_a_boundary(self):
    # a real prompt can arrive as content blocks (e.g. with pasted attachments)
    prompt = {'type': 'user', 'message': {'role': 'user', 'content': [_text('go')]}}
    turn = _final_turn([_assistant([_text('old')]), prompt, _assistant([_text('new')])])
    assert turn.text == 'new'

  def test_meta_user_entry_is_not_a_boundary(self):
    injected = {
      'type': 'user',
      'isMeta': True,
      'message': {'role': 'user', 'content': [_text('reminder')]},
    }
    turn = _final_turn(
      [_user_prompt(), _assistant([_text('before')]), injected, _assistant([_text('after')])]
    )
    assert turn.text == 'before\n\nafter'

  def test_sidechain_and_synthetic_assistants_excluded(self):
    turn = _final_turn(
      [
        _user_prompt(),
        _assistant([_text('subagent chatter')], isSidechain=True),
        _assistant([_text('interrupt notice')], model='<synthetic>'),
        _assistant([_tool_use('Bash')], isSidechain=True),
        _assistant([_text('real')]),
      ]
    )
    assert turn.text == 'real'
    assert turn.tool_calls == []

  def test_thinking_blocks_excluded(self):
    turn = _final_turn(
      [_user_prompt(), _assistant([{'type': 'thinking', 'thinking': 'hmm'}, _text('answer')])]
    )
    assert turn.text == 'answer'

  def test_non_message_entry_types_ignored(self):
    turn = _final_turn(
      [_user_prompt(), _assistant([_text('answer')]), {'type': 'file-history-snapshot'}]
    )
    assert turn.text == 'answer'


class TestMimicsTranscript:
  def test_fires_on_narrated_call_record(self):
    text = 'name: mcp__flow__update_task\ninput: {"task_id": "x"}\nresult: ok'
    assert _mimics_transcript(text) is True

  def test_ignores_prose_mentioning_a_tool_name(self):
    assert _mimics_transcript('you could call mcp__flow__add_task for that') is False

  def test_ignores_field_lines_without_a_tool_token(self):
    assert _mimics_transcript('name: config\ninput: none\nresult: fine') is False


class TestNeedsAudit:
  def test_empty_text_never_audits(self):
    assert _needs_audit(Turn(text='', tool_calls=[])) is False

  def test_toolless_turn_with_text_audits(self):
    assert _needs_audit(Turn(text='I updated the task.', tool_calls=[])) is True

  def test_turn_with_real_calls_and_plain_text_passes(self):
    assert _needs_audit(Turn(text='done', tool_calls=['Bash'])) is False

  def test_turn_with_real_calls_but_mimicry_audits(self):
    text = 'name: mcp__flow__update_task\ninput: {}\nresult: ok'
    assert _needs_audit(Turn(text=text, tool_calls=['Bash'])) is True


class TestFabricatedCalls:
  def test_narrated_call_covered_by_a_real_one_across_spellings(self):
    assert _fabricated_calls(['mcp__flow__update_task'], ['flow::update_task']) == []
    assert _fabricated_calls(['Bash'], ['bash']) == []

  def test_each_real_call_covers_one_depiction(self):
    assert _fabricated_calls(['Bash'], ['Bash', 'Bash']) == ['Bash']
    assert _fabricated_calls(['Bash', 'Bash'], ['Bash', 'Bash']) == []

  def test_narration_order_is_not_held_against_the_turn(self):
    assert _fabricated_calls(['Read', 'Bash'], ['Bash', 'Read']) == []

  def test_uncovered_narrated_call_surfaces_verbatim(self):
    fabricated = _fabricated_calls(['Bash'], ['Bash', 'mcp__flow__update_task'])
    assert fabricated == ['mcp__flow__update_task']


class TestSynchronizedTurn:
  def test_returns_immediately_when_flushed(self, tmp_path):
    path = _write(tmp_path, [_user_prompt(), _assistant([_text('the final answer')])])
    with patch('cw.listen.time.sleep') as sleep:
      turn = _synchronized_turn(path, 'the final answer')
    assert turn is not None
    assert turn.text == 'the final answer'
    sleep.assert_not_called()

  def test_empty_marker_skips_synchronization(self, tmp_path):
    path = _write(tmp_path, [_user_prompt(), _assistant([_text('whatever')])])
    turn = _synchronized_turn(path, '')
    assert turn is not None
    assert turn.text == 'whatever'

  def test_marker_matches_across_whitespace_differences(self, tmp_path):
    path = _write(tmp_path, [_user_prompt(), _assistant([_text('two  lines\nof text')])])
    turn = _synchronized_turn(path, 'two lines of text')
    assert turn is not None

  def test_waits_for_the_lagging_tail(self, tmp_path):
    path = _write(tmp_path, [_user_prompt(), _assistant([_tool_use('Bash')])])

    def flush_tail(_seconds):
      with path.open('a') as transcript:
        transcript.write(json.dumps(_assistant([_text('flushed late')])) + '\n')

    with patch('cw.listen.time.sleep', side_effect=flush_tail):
      turn = _synchronized_turn(path, 'flushed late')
    assert turn is not None
    assert turn.text == 'flushed late'
    assert turn.tool_calls == ['Bash']

  def test_none_when_the_deadline_passes(self, tmp_path):
    path = _write(tmp_path, [_user_prompt(), _assistant([_text('stale')])])
    with (
      patch('cw.listen.time.sleep'),
      patch('cw.listen.time.monotonic', side_effect=[0.0, 1000.0]),
    ):
      assert _synchronized_turn(path, 'never written') is None


def _run(monkeypatch, tmp_path, entries: list[dict], hook_input: dict):
  hook_input.setdefault('transcript_path', str(_write(tmp_path, entries)))
  monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(hook_input)))
  return listen()


_FABRICATED_TURN = [_user_prompt(), _assistant([_text('I updated the task via flow.')])]


def _extraction(tool_calls: list[str], unattributed: bool = False) -> NarratedActivity:
  return NarratedActivity(tool_calls=tool_calls, unattributed_claims=unattributed)


class TestMechanism:
  def test_first_finding_blocks_with_the_handlers_texts(self, monkeypatch, tmp_path, capsys):
    finding = Finding(reason='handler feedback', notice='handler notice')
    calls = []

    def silent(turn):
      calls.append(turn)
      return None

    with patch.object(cw.listen, '_HANDLERS', (silent, lambda turn: finding)):
      _run(
        monkeypatch,
        tmp_path,
        _FABRICATED_TURN,
        {'last_assistant_message': 'I updated the task via flow.'},
      )
    assert len(calls) == 1
    output = json.loads(capsys.readouterr().out)
    assert output == {
      'decision': 'block',
      'reason': 'handler feedback',
      'systemMessage': 'handler notice',
    }

  def test_no_finding_stays_silent(self, monkeypatch, tmp_path, capsys):
    with patch.object(cw.listen, '_HANDLERS', (lambda turn: None,)):
      _run(
        monkeypatch,
        tmp_path,
        _FABRICATED_TURN,
        {'last_assistant_message': 'I updated the task via flow.'},
      )
    assert capsys.readouterr().out == ''


class TestToolUseGuard:
  def test_blocks_a_toolless_turn_narrating_a_call(self, monkeypatch, tmp_path, capsys):
    with (
      patch.object(cw.listen.credentials, 'available', return_value=True),
      patch.object(
        cw.listen, '_narrated_activity', return_value=_extraction(['mcp__flow__update_task'])
      ),
    ):
      _run(
        monkeypatch,
        tmp_path,
        _FABRICATED_TURN,
        {'last_assistant_message': 'I updated the task via flow.'},
      )
    output = json.loads(capsys.readouterr().out)
    assert output['decision'] == 'block'
    assert 'zero real tool calls' in output['reason']
    assert 'mcp__flow__update_task' in output['reason']
    assert 'tool_use guard' in output['systemMessage']

  def test_blocks_a_toolless_turn_claiming_unattributed_actions(
    self, monkeypatch, tmp_path, capsys
  ):
    with (
      patch.object(cw.listen.credentials, 'available', return_value=True),
      patch.object(cw.listen, '_narrated_activity', return_value=_extraction([], True)),
    ):
      _run(
        monkeypatch,
        tmp_path,
        _FABRICATED_TURN,
        {'last_assistant_message': 'I updated the task via flow.'},
      )
    output = json.loads(capsys.readouterr().out)
    assert output['decision'] == 'block'
    assert 'asserts actions were performed' in output['reason']

  def test_reason_names_the_real_calls_when_some_were_made(self, monkeypatch, tmp_path, capsys):
    entries = [
      _user_prompt(),
      _assistant([_tool_use('Bash')]),
      _tool_result(),
      _assistant([_text('name: mcp__flow__update_task\ninput: {}\nresult: ok')]),
    ]
    with (
      patch.object(cw.listen.credentials, 'available', return_value=True),
      patch.object(
        cw.listen,
        '_narrated_activity',
        return_value=_extraction(['Bash', 'mcp__flow__update_task']),
      ),
    ):
      _run(monkeypatch, tmp_path, entries, {'last_assistant_message': 'result: ok'})
    output = json.loads(capsys.readouterr().out)
    assert 'are: Bash' in output['reason']
    assert 'does not record: mcp__flow__update_task' in output['reason']

  def test_narration_covered_by_real_calls_stays_silent(self, monkeypatch, tmp_path, capsys):
    entries = [
      _user_prompt(),
      _assistant([_tool_use('mcp__flow__get_projects')]),
      _tool_result(),
      _assistant([_text('name: mcp__flow__get_projects\ninput: {}\nresult: 3 projects')]),
    ]
    # unattributed claims alongside real calls pass too — loose prose plausibly
    # describes those calls
    with (
      patch.object(cw.listen.credentials, 'available', return_value=True),
      patch.object(
        cw.listen, '_narrated_activity', return_value=_extraction(['mcp__flow__get_projects'], True)
      ),
    ):
      _run(monkeypatch, tmp_path, entries, {'last_assistant_message': 'result: 3 projects'})
    assert capsys.readouterr().out == ''

  def test_clean_extraction_stays_silent(self, monkeypatch, tmp_path, capsys):
    with (
      patch.object(cw.listen.credentials, 'available', return_value=True),
      patch.object(cw.listen, '_narrated_activity', return_value=_extraction([])),
    ):
      _run(
        monkeypatch,
        tmp_path,
        _FABRICATED_TURN,
        {'last_assistant_message': 'I updated the task via flow.'},
      )
    assert capsys.readouterr().out == ''

  def test_stop_hook_active_short_circuits(self, monkeypatch, tmp_path, capsys):
    extract = MagicMock()
    with patch.object(cw.listen, '_narrated_activity', extract):
      _run(monkeypatch, tmp_path, _FABRICATED_TURN, {'stop_hook_active': True})
    extract.assert_not_called()
    assert capsys.readouterr().out == ''

  def test_gate_skips_grounded_turns_without_extracting(self, monkeypatch, tmp_path, capsys):
    entries = [
      _user_prompt(),
      _assistant([_tool_use('Read')]),
      _tool_result(),
      _assistant([_text('the file looks fine')]),
    ]
    extract = MagicMock()
    with patch.object(cw.listen, '_narrated_activity', extract):
      _run(monkeypatch, tmp_path, entries, {'last_assistant_message': 'the file looks fine'})
    extract.assert_not_called()
    assert capsys.readouterr().out == ''

  def test_abstains_without_the_llm_key(self, monkeypatch, tmp_path, capsys):
    extract = MagicMock()
    with (
      patch.object(cw.listen.credentials, 'available', return_value=False),
      patch.object(cw.listen, '_narrated_activity', extract),
    ):
      _run(
        monkeypatch,
        tmp_path,
        _FABRICATED_TURN,
        {'last_assistant_message': 'I updated the task via flow.'},
      )
    extract.assert_not_called()
    assert capsys.readouterr().out == ''

  def test_abstains_when_the_transcript_never_catches_up(self, monkeypatch, tmp_path, capsys):
    extract = MagicMock()
    with (
      patch.object(cw.listen, '_narrated_activity', extract),
      patch('cw.listen.time.sleep'),
      patch('cw.listen.time.monotonic', side_effect=[0.0, 1000.0]),
    ):
      _run(
        monkeypatch,
        tmp_path,
        _FABRICATED_TURN,
        {'last_assistant_message': 'text the transcript never flushed'},
      )
    extract.assert_not_called()
    assert capsys.readouterr().out == ''
