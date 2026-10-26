import json
import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from bro import watches
from bro.claude import runner
from bro.claude.claude_config import claude_projects_dir
from bro.claude.fake_claude_test_helper import write_fake_claude
from bro.llm.llms.anthropic import LLMSpec
from bro.monitor import SESSION_DIR_ENV, health
from bro.trails.local import LocalStore
from bro.trails.store import local_root
from ride.do_ride import SessionRun
from ride.harness import get_harness

_FAKE_SESSION = """
import subprocess
import uuid
from pathlib import Path

arguments = sys.argv[1:]
settings = json.loads(arguments[arguments.index("--settings") + 1])
Path("launch.json").write_text(json.dumps(arguments))
health = Path(os.environ["RIDE_SESSION_DIR"]) / "session-recorder-health.json"
deadline = time.monotonic() + 20
while not health.exists():
  assert time.monotonic() < deadline, "the recorder did not become ready"
  time.sleep(0.02)
assert json.loads(health.read_text())["status"] == "ok", health.read_text()

from bro.claude.claude_config import claude_projects_dir
project = claude_projects_dir(Path.cwd())
segment = arguments[arguments.index("--resume") + 1] if "--resume" in arguments else "segment"
transcript = project / (segment + ".jsonl")
project.mkdir(parents=True, exist_ok=True)
result_file = project / segment / "result.txt"
result_file.parent.mkdir(exist_ok=True)
result_file.write_text("tool output")
payload = {"session_id": segment, "transcript_path": str(transcript),
           "tool_input": {"file_path": str(result_file)}}
for event in ("UserPromptSubmit", "PreToolUse", "PostToolUse"):
  for group in settings["hooks"].get(event, []):
    for hook in group["hooks"]:
      command = hook["command"]
      assert "ride.claude" not in command
      completed = subprocess.run(command, shell=True, input=json.dumps(payload),
                                 capture_output=True, text=True, timeout=10)
      assert completed.returncode == 0, completed.stderr
      assert completed.stdout == "", completed.stdout

with transcript.open("a") as output:
  for record in (
    {"type": "user", "message": {"role": "user", "content": prompt}},
    {"type": "assistant", "version": "2.1.300", "message": {"id": uuid.uuid4().hex,
      "role": "assistant", "model": "claude-opus-5", "stop_reason": "end_turn",
      "content": [{"type": "text", "text": "reply: " + prompt}],
      "usage": {"input_tokens": 5, "output_tokens": 2}}},
  ):
    output.write(json.dumps({**record, "uuid": uuid.uuid4().hex, "sessionId": segment}) + "\\n")
result("reply: " + prompt)
sys.stdin.read()
"""


def _session(tmp_path: Path) -> SessionRun:
  return SessionRun(
    name='session-route',
    repo=None,
    harness='claude',
    hold='unattended',
    llm=None,
    resolved_llm=LLMSpec(model=None, effort=None).dump(),
    solo=True,
    resume=False,
    bro='bro',
    prompt='first turn',
    activity_file=tmp_path / 'activity',
  )


@pytest.mark.parametrize('legacy_workspace', [False, True])
def test_managed_claude_launch_hooks_recording_and_resume(monkeypatch, tmp_path, legacy_workspace):
  tree = tmp_path / 'tree'
  tree.mkdir()
  monkeypatch.chdir(tree)
  state = tmp_path / 'session'
  state.mkdir()
  for name, value in {
    SESSION_DIR_ENV: str(state),
    'CLAUDE_CONFIG_DIR': str(tmp_path / 'claude'),
    'BRO_STORE': str(tmp_path / 'credentials'),
    'BRO_HOLD': 'unattended',
    'RIDE_BRO': 'bro',
    'RIDE_ISOLATION': 'unboxed',
    'RIDE_WORKSPACE': 'session-route',
    'RIDE_HOST': 'test-host',
    'RIDE_HOST_WORKSPACE': str(tmp_path),
    'RIDE_COMMAND': 'ride solo --harness claude bro',
  }.items():
    monkeypatch.setenv(name, value)
  with patch.dict(os.environ), watches.Owner.for_session():
    binary = write_fake_claude(tmp_path / 'claude-binary', _FAKE_SESSION)
    monkeypatch.setattr(runner, '_claude_binary', lambda: binary)
    harness = get_harness('claude')
    spec = _session(tmp_path)
    if legacy_workspace:
      project = claude_projects_dir(tree)
      project.mkdir(parents=True)
      records = [
        {
          'type': 'system',
          'subtype': 'hook_response',
          'stdout': 'ride.claude.watch_waiter',
          'command': 'python -m ride.claude.watch_waiter',
        },
        {'type': 'user', 'message': {'role': 'user', 'content': 'first turn'}},
        {
          'type': 'assistant',
          'version': '2.1.300',
          'message': {
            'id': 'old-message',
            'model': 'claude-opus-5',
            'content': [{'type': 'text', 'text': 'reply: first turn'}],
            'usage': {'input_tokens': 5, 'output_tokens': 2},
          },
        },
      ]
      (project / 'segment.jsonl').write_text(
        ''.join(
          json.dumps({**record, 'sessionId': 'segment', 'uuid': f'old-{index}'}) + '\n'
          for index, record in enumerate(records)
        )
      )
      spec = replace(spec, resume=True)
    else:
      assert harness.run_session(spec) == 0
      assert (tmp_path / 'activity').exists()
      assert health.problem() is None
      recorder_health = health.health_path()
      assert recorder_health is not None
      recorder_health.unlink()

    with LocalStore(local_root()) as store:
      earlier = list(store.iter_trails(harness='claude'))
      assert harness.run_session(replace(spec, resume=True, prompt='second turn')) == 0
      (resumed,) = list(store.iter_trails(harness='claude'))
      if earlier:
        assert resumed['id'] == earlier[0]['id']
      assert resumed['end']['reason'] == 'ok'
      assert resumed['native']['llm'] == spec.resolved_llm
      assert [
        message['content'] for message in store.iter_messages(resumed['id'], types={'assistant'})
      ] == [
        'reply: first turn',
        'reply: second turn',
      ]
      arguments = json.loads((tree / 'launch.json').read_text())
      assert arguments[arguments.index('--resume') + 1] == 'segment'
      assert 'ride.claude' not in arguments[arguments.index('--settings') + 1]
      assert (tmp_path / 'activity').exists()
      assert health.problem() is None
