import io
import json

import pytest

import bro.cw.watch_guard as watch_guard

_ARGV = ['watch_guard', 'Monitor', 'summon watch', 'tail the log']


def _gate(monkeypatch, capsys, tool_input) -> str:
  monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({'tool_input': tool_input})))
  assert watch_guard.main(_ARGV) == 0
  return capsys.readouterr().out


def _denial(monkeypatch, capsys, tool_input) -> dict:
  return json.loads(_gate(monkeypatch, capsys, tool_input))['hookSpecificOutput']


class TestGate:
  @pytest.mark.parametrize('command', ['summon watch', 'tail the log', '  summon watch\n'])
  def test_a_declared_command_passes_silently(self, monkeypatch, capsys, command):
    assert _gate(monkeypatch, capsys, {'command': command}) == ''

  @pytest.mark.parametrize(
    'command',
    [
      'summon watch; rm -rf /',
      'summon watch | tee /tmp/out',
      'summon watch --verbose',
      'echo summon watch',
      '',
    ],
  )
  def test_anything_else_is_denied(self, monkeypatch, capsys, command):
    decision = _denial(monkeypatch, capsys, {'command': command})
    assert decision['permissionDecision'] == 'deny'
    assert 'summon watch' in decision['permissionDecisionReason']

  def test_a_call_carrying_no_command_is_denied(self, monkeypatch, capsys):
    # Monitor takes a websocket in place of a command; nothing declares one
    decision = _denial(monkeypatch, capsys, {'ws': {'url': 'wss://example.com/stream'}})
    assert decision['permissionDecision'] == 'deny'

  def test_a_gate_with_no_declared_command_refuses_to_run(self, monkeypatch):
    monkeypatch.setattr('sys.stdin', io.StringIO('{}'))
    with pytest.raises(ValueError, match='at least one allowed command'):
      watch_guard.main(['watch_guard', 'Monitor'])
