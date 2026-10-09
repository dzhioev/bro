import io
import json
import subprocess
import sys

import pytest

import ride.claude.command_gate as command_gate
from bro import mcp
from ride.claude.claude_argv import gate_hooks

_LINES = [
  'git status',
  "printf '%s\\n' 'it'\"'\"'s' \"$(git rev-parse HEAD)\"",
  'printf "two\nlines" | tee out.txt && echo done; false || echo fallback &',
  'echo `date` $HOME ${PATH} \\$escaped !bang ~',
  '',
]


def _gate(monkeypatch, capsys, payload: dict, policy: str = '/state/brash-policy.json') -> dict:
  monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(payload)))
  assert command_gate.main(['command_gate', '/runtime/bin/brash', policy]) == 0
  return json.loads(capsys.readouterr().out)['hookSpecificOutput']


@pytest.fixture
def argv_echo(tmp_path) -> str:
  """a stand-in for brash that prints the argv it was started with."""
  program = tmp_path / 'brash'
  program.write_text(f'#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n')
  program.chmod(0o755)
  return str(program)


class TestGate:
  @pytest.mark.parametrize('line', _LINES)
  def test_a_shell_reads_the_rewritten_line_back_untouched(
    self, monkeypatch, capsys, argv_echo, line
  ):
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({'tool_input': {'command': line}})))
    assert command_gate.main(['command_gate', argv_echo, '/state/brash policy.json']) == 0
    rewritten = json.loads(capsys.readouterr().out)['hookSpecificOutput']['updatedInput']

    for shell in ('bash', 'sh'):
      started = subprocess.run(
        [shell, '-c', rewritten['command']], capture_output=True, text=True, check=True
      )
      assert json.loads(started.stdout) == ['--policy', '/state/brash policy.json', '-c', line]

  @pytest.mark.parametrize('tool_name', ['Bash', 'Monitor'])
  def test_the_rewrite_keeps_the_calls_other_fields_and_decides_nothing(
    self, monkeypatch, capsys, tool_name
  ):
    tool_input = {'command': 'git status', 'description': 'd', 'run_in_background': True}
    output = _gate(monkeypatch, capsys, {'tool_name': tool_name, 'tool_input': tool_input})

    assert output.keys() == {'hookEventName', 'updatedInput'}
    assert output['hookEventName'] == 'PreToolUse'
    assert {**output['updatedInput'], 'command': 'git status'} == tool_input

  def test_a_call_carrying_no_command_is_denied(self, monkeypatch, capsys):
    # Monitor's WebSocket form
    payload = {'tool_name': 'Monitor', 'tool_input': {'ws': {'url': 'wss://example.com/stream'}}}
    decision = _gate(monkeypatch, capsys, payload)

    assert decision['permissionDecision'] == 'deny'
    assert 'Monitor' in decision['permissionDecisionReason']
    assert 'updatedInput' not in decision

  def test_a_failing_gate_blocks_the_call(self):
    # Claude blocks a call whose PreToolUse hook exits 2, showing the model its stderr
    gate = subprocess.run(
      [sys.executable, '-m', 'ride.claude.command_gate', '/runtime/bin/brash', '/state/policy'],
      input='not json',
      capture_output=True,
      text=True,
    )
    assert gate.returncode == 2
    assert gate.stderr.startswith('command gate failed, denying the call')


def test_the_settings_command_runs_the_runtimes_gate_whatever_its_directory_holds(tmp_path):
  # Claude runs a hook in the session's working directory, an operated checkout
  # that may carry another version of ride
  shadow = tmp_path / 'ride' / 'claude'
  shadow.mkdir(parents=True)
  for package in (tmp_path / 'ride', shadow):
    (package / '__init__.py').write_text('')
  (shadow / 'command_gate.py').write_text('raise SystemExit("the checkout gate ran")\n')
  reach = mcp.Reach(files=mcp.Files(), brash=mcp.Brash(commands=('git status',)))
  (entry, _) = gate_hooks(reach, tmp_path / 'brash-policy.json')['PreToolUse']
  (hook,) = entry['hooks']

  gate = subprocess.run(
    hook['command'],
    shell=True,
    cwd=tmp_path,
    input=json.dumps({'tool_name': 'Bash', 'tool_input': {'command': 'git status'}}),
    capture_output=True,
    text=True,
  )

  assert gate.returncode == 0, gate.stderr
  assert 'updatedInput' in json.loads(gate.stdout)['hookSpecificOutput']
