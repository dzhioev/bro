import json
import os
import signal
import subprocess
import time
from collections.abc import Generator
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

import ride.claude.runner as ride_runner
from bro import brash_policy, watches
from bro.brash import Policy
from bro.bro import BaseBro
from bro.llm.llms import claude_code
from bro.mcp import brash
from bro.monitor import SESSION_DIR_ENV, trail_pointer
from bro.summon import RUNTIME_ENV, SUMMONED_ENV
from ride.claude.claude_argv import ClaudeLaunch
from ride.claude.fake_claude_test_helper import fake_claude_env
from ride.claude.interrupt import StreamedRun
from ride.claude.mcp import MCPEndpoint
from ride.claude.shell_prefix import SHELL_PREFIX_ENV
from ride.claude.waiter_state import WaiterState
from ride.session_test import _spec

_PINNED_CLAUDE = Path('/pinned/claude')


class _ListedBro(BaseBro):
  name = 'listed'
  description = 'd'
  tools: ClassVar = [brash('git status')]

  def __init__(self):
    super().__init__(system_prompt='')


def _fake_claude(environment: dict[str, str]) -> Path:
  return Path(environment['PATH'].partition(':')[0]) / 'claude'


class _Harness:
  """patches for driving run_session without spawning claude, servers, or
  touching ~/.claude; cwd must already be the fake workspace (monkeypatch.chdir)."""

  def __init__(self, tmp_path: Path):
    self.projects_dir = tmp_path / 'projects'
    self.claude_config_dir = tmp_path / 'claude-config'
    self.session_dir = tmp_path / 'session'
    self.pinned_claude = tmp_path / 'pinned-claude'
    self.server = MagicMock()
    self.server.endpoint = MCPEndpoint(port=1234, token='tok')

  def __enter__(self):
    self._patches = [
      patch.dict('os.environ', {}, clear=False),
      patch('ride.claude.runner.claude_projects_dir', return_value=self.projects_dir),
      patch('ride.claude.runner._claude_binary', return_value=self.pinned_claude),
      patch('ride.claude.runner.start_session_mcp_server', return_value=self.server),
      patch(
        'ride.claude.runner.build_claude_launch',
        return_value=ClaudeLaunch(argv=['built'], prompt='go'),
      ),
      patch(
        'ride.claude.runner._run_claude',
        return_value=ride_runner.Run(0, stopped=False),
      ),
      patch('ride.claude.runner.start_session_recorder'),
      patch('ride.claude.runner.apply_claude_auth'),
      patch('ride.claude.runner.start_statusline_projector'),
    ]
    entered = [p.__enter__() for p in self._patches]
    self.env = entered[0]
    self.env.pop('RIDE_BRO', None)
    self.env.pop('BRO_HOLD', None)
    self.env.pop('RIDE_RUNNER_PID', None)
    self.env.pop('BROKER_CHANNEL', None)
    self.env.pop(SUMMONED_ENV, None)
    self.env['CLAUDE_CONFIG_DIR'] = str(self.claude_config_dir)
    self.env[SESSION_DIR_ENV] = str(self.session_dir)
    self.claude_binary = entered[2]
    self.start_server = entered[3]
    self.build = entered[4]
    self.run_claude = entered[5]
    self.start_recorder = entered[6]
    self.apply_auth = entered[7]
    self.start_statusline_projector = entered[8]
    return self

  def __exit__(self, *exception):
    for p in reversed(self._patches):
      p.__exit__(*exception)
    return False


class TestClaudeBinary:
  def test_boxed_session_uses_the_image_install(self, monkeypatch):
    monkeypatch.setenv('RIDE_ISOLATION', 'boxed')
    cached = MagicMock()
    monkeypatch.setattr(ride_runner.claude_release, 'cached_binary', cached)

    assert ride_runner._claude_binary() == Path('/opt/claude-code/claude')
    cached.assert_not_called()

  def test_unboxed_session_uses_the_pinned_host_release(self, monkeypatch, tmp_path):
    monkeypatch.setenv('RIDE_ISOLATION', 'unboxed')
    monkeypatch.setenv(RUNTIME_ENV, str(tmp_path / 'runtime'))
    monkeypatch.setattr(ride_runner, 'claude_code_version', lambda: '2.1.280')
    monkeypatch.setattr(ride_runner.claude_release, 'host_platform', lambda: 'linux-x64')
    binary = tmp_path / 'claude'
    cached = MagicMock(return_value=binary)
    monkeypatch.setattr(ride_runner.claude_release, 'cached_binary', cached)

    assert ride_runner._claude_binary() == binary
    cached.assert_called_once_with('2.1.280', 'linux-x64')

  def test_unboxed_session_uses_a_release_carried_by_its_runtime(self, monkeypatch, tmp_path):
    monkeypatch.setenv('RIDE_ISOLATION', 'unboxed')
    runtime = tmp_path / 'runtime'
    carried = runtime / 'claude' / 'claude'
    carried.parent.mkdir(parents=True)
    carried.touch()
    monkeypatch.setenv(RUNTIME_ENV, str(runtime))
    verified = MagicMock(return_value=carried)
    cached = MagicMock()
    monkeypatch.setattr(ride_runner.claude_release, 'verified_binary', verified)
    monkeypatch.setattr(ride_runner.claude_release, 'cached_binary', cached)

    assert ride_runner._claude_binary() == carried
    verified.assert_called_once_with(carried)
    cached.assert_not_called()


class TestSessionRun:
  def test_resume_without_session_errors(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert ride_runner.run_session(_spec(resume=True)) == 1
      assert h.run_claude.call_count == 0

  def test_resume_prepends_latest_session_id(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.projects_dir.mkdir()
      old = h.projects_dir / 'old.jsonl'
      old.write_text('{}')
      os.utime(old, (1, 1))
      (h.projects_dir / 'newer.jsonl').write_text('{}')
      assert ride_runner.run_session(_spec(resume=True, arguments=['--foo'])) == 0
      assert h.build.call_args.kwargs['claude_args'] == ['--resume', 'newer', '--foo']

  def test_the_waiter_starts_afresh_and_is_stood_down_once_claude_exits(
    self, monkeypatch, tmp_path
  ):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      earlier = WaiterState.for_session()
      earlier.reset()
      earlier.stand_down()
      stood_down_during_the_run = []

      def _run(*_arguments) -> ride_runner.Run:
        stood_down_during_the_run.append(WaiterState.for_session().stood_down())
        return ride_runner.Run(0, stopped=False)

      h.run_claude.side_effect = _run
      assert ride_runner.run_session(_spec()) == 0
      assert stood_down_during_the_run == [False]
      assert WaiterState.for_session().stood_down()

  def test_resume_reuses_the_workspace_claude_temp_root(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.projects_dir.mkdir()
      (h.projects_dir / 'session-id.jsonl').write_text('{}')
      assert ride_runner.run_session(_spec()) == 0
      temp_dir = Path(h.run_claude.call_args.args[2]['CLAUDE_CODE_TMPDIR'])
      assert temp_dir.stat().st_mode & 0o777 == 0o700
      scratchpad = temp_dir / 'session-id' / 'scratchpad'
      scratchpad.mkdir(parents=True)
      working_file = scratchpad / 'working.json'
      working_file.write_text('{}')

      assert ride_runner.run_session(_spec(resume=True)) == 0
      resumed_temp_dir = Path(h.run_claude.call_args.args[2]['CLAUDE_CODE_TMPDIR'])
      assert resumed_temp_dir == temp_dir == h.session_dir / 'claude' / 'tmp'
      assert working_file.read_text() == '{}'

  def test_pinned_claude_is_run_against_the_sessions_transcripts(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as harness:
      assert ride_runner.run_session(_spec()) == 0
      assert harness.run_claude.call_args.args[0] == tmp_path / 'pinned-claude'
      assert harness.run_claude.call_args.args[3] == harness.projects_dir

  def test_recorder_runs_for_the_session_and_stops_after(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert ride_runner.run_session(_spec()) == 0
      assert h.start_recorder.call_args.args[0] == tmp_path
      # the launch recipe lands on the trail header as native.llm
      assert h.start_recorder.call_args.kwargs['llm'] == claude_code.LLMSpec().dump()
      assert h.start_recorder.return_value.stop.call_count == 1

  def test_statusline_projector_runs_for_the_session_and_stops_after(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as harness:
      assert ride_runner.run_session(_spec()) == 0
      assert harness.start_statusline_projector.call_count == 1
      assert harness.start_statusline_projector.return_value.stop.call_count == 1

  def test_statusline_projector_start_failure_leaves_the_session_running(
    self, monkeypatch, tmp_path, caplog
  ):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as harness:
      harness.start_statusline_projector.side_effect = RuntimeError('projector failed')
      assert ride_runner.run_session(_spec()) == 0
    assert 'projector failed' in caplog.text

  def test_recorder_carries_the_launch_recipe(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert ride_runner.run_session(_spec(llm=':fable5:high')) == 0
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
      assert ride_runner.run_session(_spec()) == 1
      assert h.run_claude.call_count == 0

  def test_no_recorder_when_trails_are_disabled(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.env['TRAILS_DISABLED'] = '1'
      assert ride_runner.run_session(_spec()) == 0
      assert h.start_recorder.call_count == 0
      assert h.run_claude.call_count == 1

  def test_ride_session_serves_the_persona_and_health_gates(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert ride_runner.run_session(_spec(bro='dev')) == 0
      assert h.start_server.call_args[0][0] == 'persona:dev'
      assert h.server.wait_healthy.call_count == 1
      assert h.server.stop.call_count == 1
      assert h.start_recorder.call_count == 1
      assert h.build.call_args.kwargs['endpoint'] == h.server.endpoint

  def test_ride_session_uses_the_project_default_bro(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert ride_runner.run_session(_spec()) == 0
      assert h.start_server.call_args[0][0] == 'persona:bro-dev'

  def test_a_finite_command_list_runs_under_the_sessions_brash_policy(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('bro.registry.create_bro', lambda name: _ListedBro())
    with _Harness(tmp_path) as h:
      assert ride_runner.run_session(_spec()) == 0
      policy = h.build.call_args.kwargs['brash_policy']
      assert policy.parent == h.session_dir / 'claude'
      assert Policy.read(policy) == Policy(entries=('git status',), writable=False)
      assert h.start_server.call_args.args[2][brash_policy.POLICY_ENV] == str(policy)

  def test_an_unrestricted_shell_publishes_no_brash_policy(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.env[brash_policy.POLICY_ENV] = str(tmp_path / 'inherited.json')
      assert ride_runner.run_session(_spec(bro='dev')) == 0
      assert h.build.call_args.kwargs['brash_policy'] is None
      assert brash_policy.POLICY_ENV not in h.start_server.call_args.args[2]

  def test_a_competing_hook_refuses_the_session_before_anything_starts(
    self, monkeypatch, tmp_path, caplog
  ):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('bro.registry.create_bro', lambda name: _ListedBro())
    settings = tmp_path / '.claude' / 'settings.json'
    settings.parent.mkdir()
    settings.write_text(json.dumps({'hooks': {'PreToolUse': [{'hooks': [{'type': 'command'}]}]}}))
    with _Harness(tmp_path) as h:
      assert ride_runner.run_session(_spec()) == 1
      assert h.start_server.call_count == 0
      assert h.run_claude.call_count == 0
    assert f'{settings}: a PreToolUse hook matching every tool' in caplog.text

  def test_server_start_failure_returns_1(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.start_server.side_effect = RuntimeError('did not bind')
      assert ride_runner.run_session(_spec()) == 1
      assert h.run_claude.call_count == 0

  def test_health_gate_failure_stops_server_and_returns_1(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.server.wait_healthy.side_effect = RuntimeError('not healthy')
      assert ride_runner.run_session(_spec(bro='dev')) == 1
      assert h.run_claude.call_count == 0
      assert h.server.stop.call_count == 1

  def test_claude_exit_code_propagates(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.run_claude.return_value = ride_runner.Run(42, stopped=False)
      assert ride_runner.run_session(_spec()) == 42

  def test_ride_session_applies_auth_with_warning(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert ride_runner.run_session(_spec()) == 0
      assert h.apply_auth.call_args.kwargs == {'warn_when_missing': True}
      # the transformed env is the one claude is spawned with
      assert h.apply_auth.call_args.args[0] is h.run_claude.call_args.args[2]

  def test_claudes_mcp_limits_are_24_hour_backstops(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as harness:
      assert ride_runner.run_session(_spec()) == 0
      environment = harness.run_claude.call_args.args[2]
      assert environment['CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT'] == '86400000'
      assert environment['MCP_TOOL_TIMEOUT'] == '86400000'

  def test_claudes_bash_commands_run_through_the_session_path_prefix(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert ride_runner.run_session(_spec()) == 0
      env = h.run_claude.call_args.args[2]
      prefix = Path(env[SHELL_PREFIX_ENV])
      assert prefix.parent == h.session_dir / 'claude'
      assert os.access(prefix, os.X_OK)
      assert env['PATH'] in prefix.read_text()

  def test_the_session_skips_claudes_fast_mode_org_check(self, monkeypatch, tmp_path):
    # pins the name claude itself reads
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert ride_runner.run_session(_spec()) == 0
      assert h.run_claude.call_args.args[2]['CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK'] == '1'


@pytest.fixture
def session_state(monkeypatch, tmp_path) -> Generator[Path]:
  """a session state dir holding the session's watch store and waiter state."""
  session = tmp_path / 'session'
  monkeypatch.setenv('RIDE_SESSION_DIR', str(session))
  with watches.Owner.for_session():
    WaiterState.for_session().reset()
    yield session


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
    self, monkeypatch, session_state
  ):
    events = []

    class FakeChannel:
      @classmethod
      def from_env(cls):
        return _RecordingChannel(events)

    trail_pointer.write(session_state / trail_pointer.FILENAME, 't-root')
    monkeypatch.setattr(ride_runner, 'RunLifecycle', FakeChannel)
    run_claude = MagicMock(return_value=StreamedRun(0, stopped=False, results=('hi',)))
    monkeypatch.setattr(ride_runner, 'run_streaming', run_claude)

    assert ride_runner._run_claude_root_solo(_PINNED_CLAUDE, ['built'], {'ENV': 'yes'}, 'go') == 0
    assert run_claude.call_args.args == (
      [str(_PINNED_CLAUDE), 'built'],
      {'ENV': 'yes'},
      'go',
    )
    assert events == [
      ('trail', 't-root'),
      ('close',),
      ('completed', None, 'ok', 't-root'),
      ('close',),
    ]

  def test_zero_exit_stop_emits_no_terminal(self, monkeypatch, session_state):
    events = []

    class FakeChannel:
      @classmethod
      def from_env(cls):
        events.append('opened')
        return _RecordingChannel(events)

    monkeypatch.setattr(ride_runner, 'RunLifecycle', FakeChannel)
    monkeypatch.setattr(
      ride_runner,
      'run_streaming',
      MagicMock(return_value=StreamedRun(0, stopped=True, results=())),
    )

    assert ride_runner._run_claude_root_solo(_PINNED_CLAUDE, [], {}, 'go') == 0
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

  def test_clean_exit_relays_the_reply_and_lifecycle(
    self, tmp_path, session_state, channel_events, capfd
  ):
    trail_pointer.write(session_state / trail_pointer.FILENAME, 't-child')
    env = fake_claude_env(tmp_path, 'result("THE REPLY")\nsys.stdin.read()\n')
    assert ride_runner._run_claude_summoned(_fake_claude(env), [], env, 'go') == 0
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
    env = fake_claude_env(tmp_path, 'time.sleep(0.4)\nresult("LATE")\nsys.stdin.read()\n')
    assert ride_runner._run_claude_summoned(_fake_claude(env), [], env, 'go') == 0
    assert channel_events == [
      ('trail', 't-child'),
      ('close',),
      ('completed', 'LATE', 'ok', 't-child'),
      ('close',),
    ]

  def test_an_unpublished_trail_still_delivers_the_terminal(
    self, tmp_path, session_state, channel_events
  ):
    env = fake_claude_env(tmp_path, 'result("DONE")\nsys.stdin.read()\n')
    assert ride_runner._run_claude_summoned(_fake_claude(env), [], env, 'go') == 0
    assert channel_events == [('completed', 'DONE', 'ok', None), ('close',)]

  def test_failed_exit_emits_no_terminal_but_echoes(
    self, tmp_path, session_state, channel_events, capfd
  ):
    env = fake_claude_env(tmp_path, 'result("PARTIAL")\nsys.exit(3)\n')
    assert ride_runner._run_claude_summoned(_fake_claude(env), [], env, 'go') == 3
    assert channel_events == []
    assert capfd.readouterr().out == 'PARTIAL\n'

  def test_a_stopped_run_suppresses_the_terminal(
    self, tmp_path, session_state, channel_events, monkeypatch
  ):
    trail_pointer.write(session_state / trail_pointer.FILENAME, 't-child')
    received_signals = []
    original_send_signal = subprocess.Popen.send_signal

    def record_signal(process, signal_number):
      received_signals.append(signal_number)
      original_send_signal(process, signal_number)

    monkeypatch.setattr(subprocess.Popen, 'send_signal', record_signal)
    env = fake_claude_env(
      tmp_path,
      'signal.signal(signal.SIGINT, lambda *_: sys.exit(0))\ntime.sleep(0.2)\n'
      'os.kill(os.getppid(), signal.SIGTERM)\ntime.sleep(10)\n',
    )
    ride_runner._run_claude_summoned(_fake_claude(env), [], env, 'go')
    assert received_signals[:1] == [signal.SIGINT]
    assert not [event for event in channel_events if event[0] == 'completed']

  def test_without_a_channel_the_run_still_completes(
    self, tmp_path, session_state, monkeypatch, capfd
  ):
    monkeypatch.delenv('BROKER_CHANNEL', raising=False)
    trail_pointer.write(session_state / trail_pointer.FILENAME, 't-child')
    env = fake_claude_env(tmp_path, 'result("OK")\nsys.stdin.read()\n')
    assert ride_runner._run_claude_summoned(_fake_claude(env), [], env, 'go') == 0
    assert capfd.readouterr().out == 'OK\n'


class TestRunClaudeSummonedInteractive:
  def test_started_is_announced_and_the_exit_leaves_no_terminal(
    self, tmp_path, session_state, channel_events, monkeypatch
  ):
    monkeypatch.setattr(ride_runner, '_TRAIL_POLL_SECONDS', 0.05)
    trail_pointer.write(session_state / trail_pointer.FILENAME, 't-manual')

    def _linger(*_arguments, **_options) -> ride_runner.Run:
      deadline = time.monotonic() + 5
      while ('close',) not in channel_events:
        assert time.monotonic() < deadline, 'the trail watch never announced'
        time.sleep(0.01)
      return ride_runner.Run(0, stopped=False)

    monkeypatch.setattr(ride_runner, 'run_interactive', _linger)
    transcripts = tmp_path / 'projects'
    assert ride_runner._run_claude_summoned_interactive(_PINNED_CLAUDE, [], {}, transcripts) == 0
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


class TestSoloSession:
  def test_a_root_routes_to_the_lifecycle_runner(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as harness:
      with patch('ride.claude.runner._run_claude_root_solo', return_value=5) as solo:
        assert ride_runner.run_session(_spec(solo=True)) == 5
      solo.assert_called_once()
      harness.run_claude.assert_not_called()

  def test_a_summoned_child_routes_to_the_lifecycle_runner(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as harness:
      harness.env['RIDE_SUMMONED'] = '1'
      with patch('ride.claude.runner._run_claude_summoned', return_value=5) as solo:
        assert ride_runner.run_session(_spec(solo=True)) == 5
      solo.assert_called_once()
      harness.run_claude.assert_not_called()

  def test_a_failed_exit_is_logged_and_a_clean_one_is_not(self, monkeypatch, tmp_path, caplog):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path):
      with patch('ride.claude.runner._run_claude_root_solo', return_value=0):
        assert ride_runner.run_session(_spec(solo=True)) == 0
      assert 'claude exited with status' not in caplog.text
      with patch('ride.claude.runner._run_claude_root_solo', return_value=5):
        assert ride_runner.run_session(_spec(solo=True)) == 5
    assert 'claude exited with status 5' in caplog.text


class TestSummonedSession:
  def test_a_summoned_along_session_routes_to_the_interactive_runner(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.env['RIDE_SUMMONED'] = '1'
      with patch(
        'ride.claude.runner._run_claude_summoned_interactive', return_value=7
      ) as interactive:
        assert ride_runner.run_session(_spec()) == 7
      assert interactive.call_args.args[3] == h.projects_dir
      h.run_claude.assert_not_called()

  def test_summoner_attribution_is_dropped_from_claudes_env(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.env['RIDE_SUMMONER'] = '{"trail_id":"t-parent"}'
      assert ride_runner.run_session(_spec()) == 0
      # the recorder daemon starts before the drop, so its snapshot carries it
      assert h.start_recorder.called
      assert 'RIDE_SUMMONER' not in os.environ
      assert 'RIDE_SUMMONER' not in h.run_claude.call_args.args[2]
