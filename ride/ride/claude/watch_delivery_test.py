import json
import subprocess
import sys
from pathlib import Path

import pytest

import ride.claude.watch_delivery as watch_delivery


def _bash_call(tool_use_id: str, command: str) -> dict:
  return {
    'type': 'assistant',
    'message': {
      'role': 'assistant',
      'content': [
        {
          'type': 'tool_use',
          'id': tool_use_id,
          'name': 'Bash',
          'input': {'command': command, 'run_in_background': True},
        }
      ],
    },
  }


def _notification(task_id: str, tool_use_id: str, output_file: Path) -> str:
  return (
    '<task-notification>\n'
    f'<task-id>{task_id}</task-id>\n'
    f'<tool-use-id>{tool_use_id}</tool-use-id>\n'
    f'<output-file>{output_file}</output-file>\n'
    '<status>completed</status>\n'
    '<summary>Background command "wait" completed (exit code 0)</summary>\n'
    '</task-notification>'
  )


def _payload(tmp_path: Path, prompt: str, *calls: dict) -> dict:
  transcript = tmp_path / 'transcript.jsonl'
  transcript.write_text(''.join(json.dumps(call) + '\n' for call in calls))
  return {
    'hook_event_name': 'UserPromptSubmit',
    'prompt': prompt,
    'transcript_path': str(transcript),
  }


def _output(tmp_path: Path, name: str, content: str) -> Path:
  path = tmp_path / f'{name}.output'
  path.write_text(content)
  return path


def test_a_finished_watch_next_brings_its_lines_labeled_by_command_and_task(tmp_path):
  output = _output(tmp_path, 'b1', '[quest watch] summon ended ok\n\n[exited with code 0]\n')
  payload = _payload(
    tmp_path, _notification('b1', 'toolu_1', output), _bash_call('toolu_1', 'watch-next')
  )

  assert watch_delivery.context(payload) == (
    'Lines from `watch-next` (task b1):\n[quest watch] summon ended ok\n'
  )


def test_only_the_watch_next_among_several_notified_tasks_brings_lines(tmp_path):
  build = _output(tmp_path, 'b1', 'compiled\n[exited with code 0]\n')
  watch = _output(tmp_path, 'b2', '[poll-pr o/r 1] {"event": "checks"}\n[exited with code 0]\n')
  payload = _payload(
    tmp_path,
    _notification('b1', 'toolu_1', build) + '\n' + _notification('b2', 'toolu_2', watch),
    _bash_call('toolu_1', 'make build'),
    _bash_call('toolu_2', 'watch-next poll-pr o/r 1'),
  )

  assert watch_delivery.context(payload) == (
    'Lines from `watch-next poll-pr o/r 1` (task b2):\n[poll-pr o/r 1] {"event": "checks"}\n'
  )


@pytest.mark.parametrize('command', ['echo watch-next', 'rg watch-next src', 'watch-next-ish'])
def test_a_command_that_only_names_watch_next_brings_nothing(tmp_path, command):
  output = _output(tmp_path, 'b1', 'not a watch result\n[exited with code 0]\n')
  payload = _payload(
    tmp_path, _notification('b1', 'toolu_1', output), _bash_call('toolu_1', command)
  )

  assert watch_delivery.context(payload) is None


def test_a_prefixed_watch_next_brings_its_lines(tmp_path):
  output = _output(tmp_path, 'b1', 'line\n[exited with code 0]\n')
  command = 'PATH=/venv/bin:$PATH watch-next'
  payload = _payload(
    tmp_path, _notification('b1', 'toolu_1', output), _bash_call('toolu_1', command)
  )

  assert watch_delivery.context(payload) == f'Lines from `{command}` (task b1):\nline\n'


def test_a_prompt_that_notifies_no_task_brings_nothing(tmp_path):
  assert watch_delivery.context(_payload(tmp_path, 'fix the test')) is None


def test_a_killed_watch_next_with_no_lines_brings_nothing(tmp_path):
  output = _output(tmp_path, 'b1', '\n[killed]\n')
  payload = _payload(
    tmp_path, _notification('b1', 'toolu_1', output), _bash_call('toolu_1', 'watch-next')
  )

  assert watch_delivery.context(payload) is None


def _run_hook(payload: dict) -> subprocess.CompletedProcess:
  return subprocess.run(
    [sys.executable, '-m', 'ride.claude.watch_delivery'],
    input=json.dumps(payload),
    capture_output=True,
    text=True,
    env={'PATH': '', 'PYTHONPATH': ':'.join(sys.path)},
    check=False,
  )


def test_the_hook_returns_the_lines_as_prompt_context(tmp_path):
  output = _output(tmp_path, 'b1', 'line\n[exited with code 0]\n')
  payload = _payload(
    tmp_path, _notification('b1', 'toolu_1', output), _bash_call('toolu_1', 'watch-next')
  )

  completed = _run_hook(payload)

  assert completed.returncode == 0, completed.stderr
  hook_output = json.loads(completed.stdout)['hookSpecificOutput']
  assert hook_output['hookEventName'] == 'UserPromptSubmit'
  assert hook_output['additionalContext'] == 'Lines from `watch-next` (task b1):\nline\n'


def test_a_failure_lets_the_notification_through_with_the_reason_on_stderr(tmp_path):
  payload = _payload(
    tmp_path,
    _notification('b1', 'toolu_1', tmp_path / 'absent.output'),
    _bash_call('toolu_1', 'watch-next'),
  )

  completed = _run_hook(payload)

  assert completed.returncode == watch_delivery.FAILURE_STATUS
  assert completed.stdout == ''
  assert 'absent.output' in completed.stderr
