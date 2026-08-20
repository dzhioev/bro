import os
import signal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import ride.claude.runner as ride_runner
from bro.launch.broxy import START_SESSION_BROXY_ENV
from bro.llm.llms import claude_code
from bro.monitor import trail_pointer
from bro.workspace.paths import workspace_dir
from ride.claude.claude_argv import ClaudeLaunch
from ride.claude.mcp import MCPEndpoint
from ride.session_test import _spec


class _Harness:
  """patches for driving run_in_place without spawning claude, servers, or
  touching ~/.claude; cwd must already be the fake workspace (monkeypatch.chdir)."""

  def __init__(self, tmp_path: Path):
    self.projects_dir = tmp_path / 'projects'
    self.claude_config_dir = tmp_path / 'claude-config'
    self.server = MagicMock()
    self.server.endpoint = MCPEndpoint(port=1234, token='tok')
    self.broxy = MagicMock()
    self.broxy.address = 'unix:/tmp/broxy-test.sock'

  def __enter__(self):
    self._patches = [
      patch.dict('os.environ', {}, clear=False),
      patch('ride.claude.runner.claude_projects_dir', return_value=self.projects_dir),
      patch('ride.claude.runner.start_session_mcp_server', return_value=self.server),
      patch(
        'ride.claude.runner.build_claude_launch',
        return_value=ClaudeLaunch(argv=['built'], system_prompt='sp'),
      ),
      patch('ride.claude.runner._run_claude', return_value=0),
      patch('ride.claude.runner.start_session_recorder'),
      patch('ride.claude.runner.apply_claude_auth'),
      patch('bro.launch.broxy._start_session_broxy', return_value=self.broxy),
      patch('ride.claude.runner.in_container', return_value=False),
      patch('ride.claude.runner.provision_host_claude_dir', return_value=self.claude_config_dir),
      patch('ride.claude.runner.project_root', return_value=Path('/main-repo')),
    ]
    entered = [p.__enter__() for p in self._patches]
    self.env = entered[0]
    self.env.pop('RIDE_BRO', None)
    self.env.pop('BRO_HOLD', None)
    self.env.pop('RIDE_RUNNER_PID', None)
    self.env.pop('BROKER_CHANNEL', None)
    self.env.pop(ride_runner.SUMMONED_ENV, None)
    self.env.pop(START_SESSION_BROXY_ENV, None)
    self.env.pop('CLAUDE_CONFIG_DIR', None)
    self.start_server = entered[2]
    self.build = entered[3]
    self.run_claude = entered[4]
    self.start_recorder = entered[5]
    self.apply_auth = entered[6]
    self.start_broxy = entered[7]
    self.in_container = entered[8]
    self.provision_claude_dir = entered[9]
    return self

  def __exit__(self, *exception):
    for p in reversed(self._patches):
      p.__exit__(*exception)
    return False


class TestRunInPlace:
  def test_resume_without_session_errors(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert ride_runner.run_in_place(_spec(resume=True)) == 1
      assert h.run_claude.call_count == 0

  def test_resume_prepends_latest_session_id(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.projects_dir.mkdir()
      old = h.projects_dir / 'old.jsonl'
      old.write_text('{}')
      os.utime(old, (1, 1))
      (h.projects_dir / 'newer.jsonl').write_text('{}')
      assert ride_runner.run_in_place(_spec(resume=True, arguments=['--foo'])) == 0
      assert h.build.call_args.kwargs['claude_args'] == ['--resume', 'newer', '--foo']

  def test_recorder_runs_for_the_session_and_stops_after(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert ride_runner.run_in_place(_spec()) == 0
      assert h.start_recorder.call_args.args[0] == 'w'
      # the launch recipe lands on the trail header as native.llm
      assert h.start_recorder.call_args.kwargs['llm'] == claude_code.LLMSpec().dump()
      # spawned after the session context is set, so the daemon inherits it
      assert 'RIDE_SESSION_CONTEXT' in h.start_recorder.call_args.args[2]
      assert h.start_recorder.return_value.stop.call_count == 1

  def test_recorder_carries_the_launch_recipe(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert ride_runner.run_in_place(_spec(llm=':fable5:high')) == 0
      assert h.start_recorder.call_args.kwargs['llm'] == {
        'type': 'claude-code',
        'model': 'claude-fable-5',
        'effort': 'high',
        'fast_mode': False,
      }

  def test_recorder_start_failure_does_not_block_the_launch(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.start_recorder.return_value = None
      assert ride_runner.run_in_place(_spec()) == 0
      assert h.run_claude.call_count == 1

  def test_no_recorder_when_trails_are_disabled(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.env['TRAILS_DISABLED'] = '1'
      assert ride_runner.run_in_place(_spec()) == 0
      assert h.start_recorder.call_count == 0
      assert h.run_claude.call_count == 1

  def test_raw_session_serves_health_gates_and_syncs(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert ride_runner.run_in_place(_spec(bro='dev', raw=True)) == 0
      assert h.start_server.call_args[0][0] == 'bro:dev'
      assert h.server.wait_healthy.call_count == 1
      assert h.server.stop.call_count == 1
      assert h.start_recorder.call_count == 1
      assert h.build.call_args.kwargs['endpoint'] == h.server.endpoint

  def test_ride_session_serves_the_persona_and_health_gates(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert ride_runner.run_in_place(_spec(bro='dev')) == 0
      assert h.start_server.call_args[0][0] == 'persona:dev'
      assert h.server.wait_healthy.call_count == 1
      assert h.server.stop.call_count == 1
      assert h.start_recorder.call_count == 1
      assert h.build.call_args.kwargs['endpoint'] == h.server.endpoint

  def test_ride_session_uses_the_project_default_bro(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert ride_runner.run_in_place(_spec()) == 0
      assert h.start_server.call_args[0][0] == 'persona:bro-dev'

  def test_server_start_failure_returns_1(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.start_server.side_effect = RuntimeError('did not bind')
      assert ride_runner.run_in_place(_spec()) == 1
      assert h.run_claude.call_count == 0

  def test_health_gate_failure_stops_server_and_returns_1(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.server.wait_healthy.side_effect = RuntimeError('not healthy')
      assert ride_runner.run_in_place(_spec(bro='dev', raw=True)) == 1
      assert h.run_claude.call_count == 0
      assert h.server.stop.call_count == 1

  def test_exports_the_session_hold(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert ride_runner.run_in_place(_spec(hold='unattended')) == 0
      assert h.env['BRO_HOLD'] == 'unattended'

  def test_overwrites_the_ambient_hold(self, monkeypatch, tmp_path):
    # a session launched from inside an unattended one must not inherit the
    # hold (the MCP server would otherwise mount `raise` for an attended session)
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.env['BRO_HOLD'] = 'unattended'
      assert ride_runner.run_in_place(_spec(hold='attended')) == 0
      assert h.env['BRO_HOLD'] == 'attended'

  def test_exports_its_own_pid_as_the_raise_kill_target(self, monkeypatch, tmp_path):
    # overwriting the ambient value: an inherited pid would name a foreign runner
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.env['RIDE_RUNNER_PID'] = '1'
      assert ride_runner.run_in_place(_spec()) == 0
      assert h.env['RIDE_RUNNER_PID'] == str(os.getpid())

  def test_session_context_set_next_to_claude(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert ride_runner.run_in_place(_spec()) == 0
      assert 'RIDE_SESSION_CONTEXT' in h.env

  def test_claude_exit_code_propagates(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.run_claude.return_value = 42
      assert ride_runner.run_in_place(_spec()) == 42

  def test_ride_session_applies_auth_with_warning(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert ride_runner.run_in_place(_spec()) == 0
      assert h.apply_auth.call_args.kwargs == {'warn_when_missing': True}
      # the transformed env is the one claude is spawned with
      assert h.apply_auth.call_args.args[0] is h.run_claude.call_args.args[1]

  def test_raw_session_applies_auth_without_warning(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert ride_runner.run_in_place(_spec(bro='dev', raw=True)) == 0
      assert h.apply_auth.call_args.kwargs == {'warn_when_missing': False}

  def test_extends_claudes_mcp_tool_call_timeout(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert ride_runner.run_in_place(_spec()) == 0
      assert h.run_claude.call_args.args[1]['MCP_TOOL_TIMEOUT'] == '600000'

  def test_host_session_provisions_and_exports_the_claude_config_dir(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert ride_runner.run_in_place(_spec()) == 0
      h.provision_claude_dir.assert_called_once_with(
        workspace_dir(Path('/main-repo'), 'w'), tmp_path, Path('/main-repo')
      )
      env = h.run_claude.call_args.args[1]
      assert env['CLAUDE_CONFIG_DIR'] == str(h.claude_config_dir)

  def test_container_session_keeps_the_default_claude_config(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.in_container.return_value = True
      assert ride_runner.run_in_place(_spec()) == 0
      h.provision_claude_dir.assert_not_called()
      assert 'CLAUDE_CONFIG_DIR' not in h.run_claude.call_args.args[1]


class TestRunClaude:
  def test_forwards_sigterm_and_returns_child_exit_code(self, tmp_path):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    fake = bin_dir / 'claude'
    # the fake claude SIGTERMs the runner process; the runner's handler must
    # forward it back down, which the trap converts to exit 7
    fake.write_text(
      '#!/usr/bin/env bash\n'
      'trap "exit 7" TERM\n'
      'sleep 0.2\n'
      'kill -TERM $PPID\n'
      'while true; do sleep 0.05; done\n'
    )
    fake.chmod(0o755)
    previous = signal.getsignal(signal.SIGTERM)
    env = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}'}
    assert ride_runner._run_claude([], env) == 7
    assert signal.getsignal(signal.SIGTERM) == previous


def _fake_claude(tmp_path: Path, script: str) -> dict[str, str]:
  bin_dir = tmp_path / 'bin'
  bin_dir.mkdir(exist_ok=True)
  fake = bin_dir / 'claude'
  fake.write_text(f'#!/usr/bin/env bash\n{script}')
  fake.chmod(0o755)
  return {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}'}


class _RecordingChannel:
  def __init__(self, events: list):
    self._events = events

  def started(self, trail_id: str, *, workspace=None) -> None:
    self._events.append(('started', trail_id, workspace))

  def completed(self, result, end_reason) -> None:
    self._events.append(('completed', result, end_reason))

  def close(self) -> None:
    self._events.append(('close',))


class TestRunClaudeSummoned:
  @pytest.fixture
  def channel_events(self, monkeypatch) -> list:
    events: list = []

    class FakeChannel:
      @classmethod
      def from_env(cls):
        return _RecordingChannel(events)

    monkeypatch.setattr(ride_runner, 'BroChannel', FakeChannel)
    return events

  @pytest.fixture
  def session_state(self, monkeypatch, tmp_path) -> Path:
    session = tmp_path / 'session'
    monkeypatch.setenv('RIDE_SESSION_DIR', str(session))
    return session

  def test_clean_exit_relays_the_reply_and_lifecycle(
    self, tmp_path, session_state, channel_events, capfd
  ):
    trail_pointer.write(session_state / trail_pointer.FILENAME, 't-child')
    env = _fake_claude(tmp_path, 'echo "THE REPLY"\n')
    assert ride_runner._run_claude_summoned([], env, 'w') == 0
    assert channel_events == [
      ('started', 't-child', 'w'),
      ('close',),
      ('completed', 'THE REPLY', 'ok'),
      ('close',),
    ]
    # the reply is echoed so the child's captured output tail carries it too
    assert capfd.readouterr().out == 'THE REPLY\n'

  def test_started_lands_while_claude_still_runs(
    self, tmp_path, session_state, channel_events, monkeypatch
  ):
    monkeypatch.setattr(ride_runner, '_TRAIL_POLL_SECONDS', 0.05)
    trail_pointer.write(session_state / trail_pointer.FILENAME, 't-child')
    env = _fake_claude(tmp_path, 'sleep 0.4\necho LATE\n')
    assert ride_runner._run_claude_summoned([], env, 'w') == 0
    assert channel_events == [
      ('started', 't-child', 'w'),
      ('close',),
      ('completed', 'LATE', 'ok'),
      ('close',),
    ]

  def test_an_unpublished_trail_still_delivers_the_terminal(
    self, tmp_path, session_state, channel_events
  ):
    env = _fake_claude(tmp_path, 'echo DONE\n')
    assert ride_runner._run_claude_summoned([], env, 'w') == 0
    assert channel_events == [('completed', 'DONE', 'ok'), ('close',)]

  def test_failed_exit_emits_no_terminal_but_echoes(
    self, tmp_path, session_state, channel_events, capfd
  ):
    env = _fake_claude(tmp_path, 'echo PARTIAL\nexit 3\n')
    assert ride_runner._run_claude_summoned([], env, 'w') == 3
    assert channel_events == []
    assert capfd.readouterr().out == 'PARTIAL\n'

  def test_a_forwarded_sigterm_suppresses_the_terminal(
    self, tmp_path, session_state, channel_events
  ):
    trail_pointer.write(session_state / trail_pointer.FILENAME, 't-child')
    env = _fake_claude(
      tmp_path,
      'trap "exit 0" TERM\nsleep 0.2\nkill -TERM $PPID\nwhile true; do sleep 0.05; done\n',
    )
    assert ride_runner._run_claude_summoned([], env, 'w') == 0
    assert not [event for event in channel_events if event[0] == 'completed']

  def test_without_a_channel_the_run_still_completes(
    self, tmp_path, session_state, monkeypatch, capfd
  ):
    monkeypatch.delenv('BROKER_CHANNEL', raising=False)
    trail_pointer.write(session_state / trail_pointer.FILENAME, 't-child')
    env = _fake_claude(tmp_path, 'echo OK\n')
    assert ride_runner._run_claude_summoned([], env, 'w') == 0
    assert capfd.readouterr().out == 'OK\n'


class TestRunClaudeSummonedInteractive:
  def test_started_is_announced_and_the_exit_leaves_no_terminal(
    self, tmp_path, session_state, channel_events, monkeypatch
  ):
    monkeypatch.setattr(ride_runner, '_TRAIL_POLL_SECONDS', 0.05)
    trail_pointer.write(session_state / trail_pointer.FILENAME, 't-manual')
    env = _fake_claude(tmp_path, 'sleep 0.4\n')
    assert ride_runner._run_claude_summoned_interactive([], env, 'my-manual') == 0
    # the terminal belongs to the `answer` service tool; an exit without it
    # surfaces to the summoner as the channel-gone failure
    assert channel_events == [('started', 't-manual', 'my-manual'), ('close',)]

  @pytest.fixture
  def channel_events(self, monkeypatch) -> list:
    events: list = []

    class FakeChannel:
      @classmethod
      def from_env(cls):
        return _RecordingChannel(events)

    monkeypatch.setattr(ride_runner, 'BroChannel', FakeChannel)
    return events

  @pytest.fixture
  def session_state(self, monkeypatch, tmp_path) -> Path:
    session = tmp_path / 'session'
    monkeypatch.setenv('RIDE_SESSION_DIR', str(session))
    return session


class TestSummonedSession:
  def test_a_summoned_child_routes_to_the_lifecycle_runner(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.env['RIDE_SUMMONED'] = '1'
      with patch('ride.claude.runner._run_claude_summoned', return_value=5) as summoned:
        assert ride_runner.run_in_place(_spec(solo=True)) == 5
      summoned.assert_called_once()
      h.run_claude.assert_not_called()

  def test_a_summoned_along_session_routes_to_the_interactive_runner(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.env['RIDE_SUMMONED'] = '1'
      with patch(
        'ride.claude.runner._run_claude_summoned_interactive', return_value=7
      ) as interactive:
        assert ride_runner.run_in_place(_spec()) == 7
      assert interactive.call_args.args[2] == 'w'
      h.run_claude.assert_not_called()

  def test_summoner_attribution_is_dropped_from_claudes_env(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.env['RIDE_SUMMONER'] = '{"trail_id":"t-parent"}'
      assert ride_runner.run_in_place(_spec()) == 0
      # the recorder daemon starts before the drop, so its snapshot carries it
      assert h.start_recorder.called
      assert 'RIDE_SUMMONER' not in os.environ
      assert 'RIDE_SUMMONER' not in h.run_claude.call_args.args[1]
