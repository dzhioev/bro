import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import ride.claude.runner as ride_runner
from bro.launch.broxy import START_SESSION_BROXY_ENV
from bro.llm.llms import claude_code
from bro.monitor import trail_pointer
from bro.summon import SUMMONED_ENV
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
    self.broxy.address = 'tcp://broxy-token@127.0.0.1:8'

  def __enter__(self):
    self._patches = [
      patch.dict('os.environ', {}, clear=False),
      patch('ride.claude.runner.claude_projects_dir', return_value=self.projects_dir),
      patch('ride.claude.runner.start_session_mcp_server', return_value=self.server),
      patch(
        'ride.claude.runner.build_claude_launch',
        return_value=ClaudeLaunch(argv=['built'], system_prompt='sp'),
      ),
      patch(
        'ride.claude.runner._run_claude',
        return_value=ride_runner.InteractiveRun(0, stopped=False),
      ),
      patch('ride.claude.runner.start_session_recorder'),
      patch('ride.claude.runner.apply_claude_auth'),
      patch('bro.launch.broxy._start_session_broxy', return_value=self.broxy),
      patch('ride.claude.runner.in_container', return_value=False),
      patch('ride.claude.runner.provision_host_claude_dir', return_value=self.claude_config_dir),
      patch('ride.claude.runner.start_statusline_projector'),
    ]
    entered = [p.__enter__() for p in self._patches]
    self.env = entered[0]
    self.env.pop('RIDE_BRO', None)
    self.env.pop('BRO_HOLD', None)
    self.env.pop('RIDE_RUNNER_PID', None)
    self.env.pop('BROKER_CHANNEL', None)
    self.env.pop(SUMMONED_ENV, None)
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
    self.start_statusline_projector = entered[10]
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

  def test_claude_is_run_against_the_sessions_transcripts(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert ride_runner.run_in_place(_spec()) == 0
      assert h.run_claude.call_args.args[2] == h.projects_dir

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

  def test_statusline_projector_runs_for_the_session_and_stops_after(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as harness:
      assert ride_runner.run_in_place(_spec()) == 0
      assert harness.start_statusline_projector.call_count == 1
      assert harness.start_statusline_projector.return_value.stop.call_count == 1

  def test_statusline_projector_start_failure_leaves_the_session_running(
    self, monkeypatch, tmp_path, caplog
  ):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as harness:
      harness.start_statusline_projector.side_effect = RuntimeError('projector failed')
      assert ride_runner.run_in_place(_spec()) == 0
    assert 'projector failed' in caplog.text

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

  def test_a_recorder_that_cannot_start_ends_the_launch(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.start_recorder.side_effect = RuntimeError('cannot start the session recorder: nope')
      assert ride_runner.run_in_place(_spec()) == 1
      assert h.run_claude.call_count == 0

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

  def test_session_context_set_next_to_claude(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert ride_runner.run_in_place(_spec()) == 0
      assert 'RIDE_SESSION_CONTEXT' in h.env

  def test_claude_exit_code_propagates(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.run_claude.return_value = ride_runner.InteractiveRun(42, stopped=False)
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

  def test_full_session_skips_claudes_fast_mode_org_check(self, monkeypatch, tmp_path):
    # pins the name claude itself reads
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert ride_runner.run_in_place(_spec()) == 0
      assert h.run_claude.call_args.args[1]['CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK'] == '1'

  def test_raw_session_keeps_claudes_fast_mode_org_check(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert ride_runner.run_in_place(_spec(bro='dev', raw=True)) == 0
      assert 'CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK' not in h.run_claude.call_args.args[1]

  def test_host_session_provisions_and_exports_the_claude_config_dir(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert ride_runner.run_in_place(_spec()) == 0
      h.provision_claude_dir.assert_called_once_with(workspace_dir('w'), tmp_path, tmp_path)
      env = h.run_claude.call_args.args[1]
      assert env['CLAUDE_CONFIG_DIR'] == str(h.claude_config_dir)

  def test_container_session_keeps_the_default_claude_config(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.in_container.return_value = True
      assert ride_runner.run_in_place(_spec()) == 0
      h.provision_claude_dir.assert_not_called()
      assert 'CLAUDE_CONFIG_DIR' not in h.run_claude.call_args.args[1]


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

  def trail(self, trail_id: str) -> None:
    self._events.append(('trail', trail_id))

  def completed(self, result, end_reason, *, trail_id=None) -> None:
    self._events.append(('completed', result, end_reason, trail_id))

  def close(self) -> None:
    self._events.append(('close',))


class TestRunClaudeRootSolo:
  def test_clean_exit_reports_success_without_replacing_the_streamed_reply(
    self, monkeypatch, tmp_path
  ):
    events = []

    class FakeChannel:
      @classmethod
      def from_env(cls):
        return _RecordingChannel(events)

    session = tmp_path / 'session'
    monkeypatch.setenv('RIDE_SESSION_DIR', str(session))
    trail_pointer.write(session / trail_pointer.FILENAME, 't-root')
    monkeypatch.setattr(ride_runner, 'RunLifecycle', FakeChannel)
    run_claude = MagicMock(return_value=ride_runner.InteractiveRun(0, stopped=False))
    monkeypatch.setattr(ride_runner, '_run_claude', run_claude)
    transcripts = tmp_path / 'projects'

    assert ride_runner._run_claude_root_solo(['built'], {'ENV': 'yes'}, transcripts) == 0
    assert run_claude.call_args.args == (['built'], {'ENV': 'yes'}, transcripts)
    assert events == [
      ('trail', 't-root'),
      ('close',),
      ('completed', None, 'ok', 't-root'),
      ('close',),
    ]

  def test_zero_exit_stop_emits_no_terminal(self, monkeypatch, tmp_path):
    events = []

    class FakeChannel:
      @classmethod
      def from_env(cls):
        events.append('opened')
        return _RecordingChannel(events)

    monkeypatch.setattr(ride_runner, 'RunLifecycle', FakeChannel)
    monkeypatch.setattr(
      ride_runner,
      '_run_claude',
      MagicMock(return_value=ride_runner.InteractiveRun(0, stopped=True)),
    )

    assert ride_runner._run_claude_root_solo([], {}, tmp_path / 'projects') == 0
    assert events == []


class TestRunClaudeSummoned:
  @pytest.fixture
  def channel_events(self, monkeypatch) -> list:
    events: list = []

    class FakeChannel:
      @classmethod
      def from_env(cls):
        return _RecordingChannel(events)

    monkeypatch.setattr(ride_runner, 'RunLifecycle', FakeChannel)
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
    assert ride_runner._run_claude_summoned([], env) == 0
    assert channel_events == [
      ('trail', 't-child'),
      ('close',),
      ('completed', 'THE REPLY', 'ok', 't-child'),
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
    assert ride_runner._run_claude_summoned([], env) == 0
    assert channel_events == [
      ('trail', 't-child'),
      ('close',),
      ('completed', 'LATE', 'ok', 't-child'),
      ('close',),
    ]

  def test_an_unpublished_trail_still_delivers_the_terminal(
    self, tmp_path, session_state, channel_events
  ):
    env = _fake_claude(tmp_path, 'echo DONE\n')
    assert ride_runner._run_claude_summoned([], env) == 0
    assert channel_events == [('completed', 'DONE', 'ok', None), ('close',)]

  def test_failed_exit_emits_no_terminal_but_echoes(
    self, tmp_path, session_state, channel_events, capfd
  ):
    env = _fake_claude(tmp_path, 'echo PARTIAL\nexit 3\n')
    assert ride_runner._run_claude_summoned([], env) == 3
    assert channel_events == []
    assert capfd.readouterr().out == 'PARTIAL\n'

  def test_a_stopped_run_suppresses_the_terminal(self, tmp_path, session_state, channel_events):
    trail_pointer.write(session_state / trail_pointer.FILENAME, 't-child')
    # exit 5 on TERM: the stop must reach claude as the interrupt, not as the
    # kill the stop falls back to when the interrupt goes unanswered
    env = _fake_claude(
      tmp_path,
      'trap "exit 0" INT\ntrap "exit 5" TERM\nsleep 0.2\nkill -TERM $PPID\n'
      'while true; do sleep 0.05; done\n',
    )
    assert ride_runner._run_claude_summoned([], env) == 0
    assert not [event for event in channel_events if event[0] == 'completed']

  def test_without_a_channel_the_run_still_completes(
    self, tmp_path, session_state, monkeypatch, capfd
  ):
    monkeypatch.delenv('BROKER_CHANNEL', raising=False)
    trail_pointer.write(session_state / trail_pointer.FILENAME, 't-child')
    env = _fake_claude(tmp_path, 'echo OK\n')
    assert ride_runner._run_claude_summoned([], env) == 0
    assert capfd.readouterr().out == 'OK\n'


class TestRunClaudeSummonedInteractive:
  def test_started_is_announced_and_the_exit_leaves_no_terminal(
    self, tmp_path, session_state, channel_events, monkeypatch
  ):
    monkeypatch.setattr(ride_runner, '_TRAIL_POLL_SECONDS', 0.05)
    trail_pointer.write(session_state / trail_pointer.FILENAME, 't-manual')

    def _linger(*_arguments) -> ride_runner.InteractiveRun:
      time.sleep(0.4)
      return ride_runner.InteractiveRun(0, stopped=False)

    monkeypatch.setattr(ride_runner, 'run_interactive', _linger)
    transcripts = tmp_path / 'projects'
    assert ride_runner._run_claude_summoned_interactive([], {}, transcripts) == 0
    # the terminal belongs to the `answer` service tool; an exit without it
    # surfaces to the summoner as the channel-gone failure
    assert channel_events == [('trail', 't-manual'), ('close',)]

  @pytest.fixture
  def channel_events(self, monkeypatch) -> list:
    events: list = []

    class FakeChannel:
      @classmethod
      def from_env(cls):
        return _RecordingChannel(events)

    monkeypatch.setattr(ride_runner, 'RunLifecycle', FakeChannel)
    return events

  @pytest.fixture
  def session_state(self, monkeypatch, tmp_path) -> Path:
    session = tmp_path / 'session'
    monkeypatch.setenv('RIDE_SESSION_DIR', str(session))
    return session


class TestSoloSession:
  def test_a_root_routes_to_the_lifecycle_runner(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as harness:
      with patch('ride.claude.runner._run_claude_root_solo', return_value=5) as solo:
        assert ride_runner.run_in_place(_spec(solo=True)) == 5
      solo.assert_called_once()
      harness.run_claude.assert_not_called()

  def test_a_summoned_child_routes_to_the_lifecycle_runner(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as harness:
      harness.env['RIDE_SUMMONED'] = '1'
      with patch('ride.claude.runner._run_claude_summoned', return_value=5) as solo:
        assert ride_runner.run_in_place(_spec(solo=True)) == 5
      solo.assert_called_once()
      harness.run_claude.assert_not_called()


class TestSummonedSession:
  def test_a_summoned_along_session_routes_to_the_interactive_runner(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.env['RIDE_SUMMONED'] = '1'
      with patch(
        'ride.claude.runner._run_claude_summoned_interactive', return_value=7
      ) as interactive:
        assert ride_runner.run_in_place(_spec()) == 7
      assert interactive.call_args.args[2] == h.projects_dir
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
