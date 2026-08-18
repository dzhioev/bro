import os
import signal
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import ride.claude.runner as ride_runner
from bro.base import credentials
from bro.launch.broxy import START_SESSION_BROXY_ENV
from bro.llm.llms import claude_code
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
      patch('ride.claude.runner._start_session_mcp_server', return_value=self.server),
      patch(
        'ride.claude.runner.build_claude_launch',
        return_value=ClaudeLaunch(argv=['built'], system_prompt='sp'),
      ),
      patch('ride.claude.runner._run_claude', return_value=0),
      patch('ride.claude.runner._start_session_recorder'),
      patch('ride.claude.runner._apply_claude_auth'),
      patch('bro.launch.broxy._start_session_broxy', return_value=self.broxy),
      patch('ride.claude.runner.in_container', return_value=False),
      patch('ride.claude.runner._provision_host_claude_dir', return_value=self.claude_config_dir),
      patch('ride.claude.runner.project_root', return_value=Path('/main-repo')),
      # an empty credential store pins the derived git identity to the legacy
      # address regardless of the developer host's real `github` secret
      patch('bro.base.credentials.default_store', return_value=credentials.Store({})),
      # the workspace-provisioning probe; default: no feature, no hook install
      patch('bro.registry.create_bro'),
    ]
    entered = [p.__enter__() for p in self._patches]
    self.env = entered[0]
    self.env.pop('RIDE_BRO', None)
    self.env.pop('BRO_HOLD', None)
    self.env.pop('RIDE_RUNNER_PID', None)
    self.env.pop('BROKER_CHANNEL', None)
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
    self.create_bro = entered[12]
    self.create_bro.return_value.has_feature.return_value = False
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
      assert h.env['RIDE_BRO'] == 'dev'
      assert h.build.call_args.kwargs['endpoint'] == h.server.endpoint

  def test_ride_session_serves_the_persona_and_health_gates(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert ride_runner.run_in_place(_spec(bro='dev')) == 0
      assert h.start_server.call_args[0][0] == 'persona:dev'
      assert h.server.wait_healthy.call_count == 1
      assert h.server.stop.call_count == 1
      assert h.start_recorder.call_count == 1
      assert h.env['RIDE_BRO'] == 'dev'
      assert h.build.call_args.kwargs['endpoint'] == h.server.endpoint

  def test_ride_session_uses_the_project_default_bro(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert ride_runner.run_in_place(_spec()) == 0
      assert h.start_server.call_args[0][0] == 'persona:bro-dev'
      assert h.env['RIDE_BRO'] == 'bro-dev'

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

  def test_exports_bro_git_identity_unconditionally(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      # every session commits as bro, hold-independent
      assert ride_runner.run_in_place(_spec()) == 0
      assert h.env['GIT_AUTHOR_NAME'] == 'bro-dev'
      assert h.env['GIT_COMMITTER_EMAIL'] == 'bro-dev@bro'

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

  def test_accounting_persona_gets_the_footer_hooks(self, monkeypatch, tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    subprocess.run(['git', 'init', '-q', str(workspace)], check=True)
    monkeypatch.chdir(workspace)
    with _Harness(tmp_path) as h:
      h.create_bro.return_value.has_feature.return_value = True
      assert ride_runner.run_in_place(_spec()) == 0
      h.create_bro.assert_called_once_with('bro-dev')
      h.create_bro.return_value.has_feature.assert_called_once_with('commit-accounting')
    for hook_name in ('commit-msg', 'post-commit'):
      assert (workspace / '.git' / 'hooks' / hook_name).exists()

  def test_no_footer_hooks_without_the_feature(self, monkeypatch, tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    subprocess.run(['git', 'init', '-q', str(workspace)], check=True)
    monkeypatch.chdir(workspace)
    with _Harness(tmp_path):
      assert ride_runner.run_in_place(_spec()) == 0
    assert not (workspace / '.git' / 'hooks' / 'commit-msg').exists()

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


class TestSessionBroxy:
  def test_rewrites_the_channel_and_stops_the_broxy(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.env['BROKER_CHANNEL'] = 'unix:/up.sock'
      h.env[START_SESSION_BROXY_ENV] = '1'
      assert ride_runner.run_in_place(_spec()) == 0
      assert h.start_broxy.call_args.args[0] == 'unix:/up.sock'
      env = h.run_claude.call_args.args[1]
      assert env['BROKER_CHANNEL'] == h.broxy.address
      h.broxy.stop.assert_called_once()

  def test_unsets_the_channel_when_the_broxy_cannot_start(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.env['BROKER_CHANNEL'] = 'unix:/up.sock'
      h.env[START_SESSION_BROXY_ENV] = '1'
      h.start_broxy.return_value = None
      assert ride_runner.run_in_place(_spec()) == 0
      env = h.run_claude.call_args.args[1]
      assert 'BROKER_CHANNEL' not in env

  def test_container_mode_keeps_the_entrypoint_owned_channel(self, monkeypatch, tmp_path):
    # a container carries no BRO_START_SESSION_BROXY signal — only a host
    # launch sets it — so the entrypoint's channel passes through untouched
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.in_container.return_value = True
      h.env['BROKER_CHANNEL'] = 'unix:/tmp/broxy.sock'
      assert ride_runner.run_in_place(_spec()) == 0
      h.start_broxy.assert_not_called()
      env = h.run_claude.call_args.args[1]
      assert env['BROKER_CHANNEL'] == 'unix:/tmp/broxy.sock'

  def test_no_channel_starts_no_broxy(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.env[START_SESSION_BROXY_ENV] = '1'
      assert ride_runner.run_in_place(_spec()) == 0
      h.start_broxy.assert_not_called()


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
