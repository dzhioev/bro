import os
import signal
from pathlib import Path
from unittest.mock import MagicMock, patch

import cw.runner
from cw.claude_argv import ClaudeLaunch
from cw.mcp import MCPEndpoint
from cw.session_test import _spec


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
      patch('cw.runner._claude_projects_dir', return_value=self.projects_dir),
      patch('cw.runner._start_session_mcp_server', return_value=self.server),
      patch(
        'cw.runner.build_claude_launch',
        return_value=ClaudeLaunch(argv=['built'], system_prompt='sp'),
      ),
      patch('cw.runner._run_claude', return_value=0),
      patch('cw.runner._sync_bare_session_log'),
      patch('cw.runner._populate_bro_skills'),
      patch('cw.runner._apply_claude_auth'),
      patch('cw.runner._start_session_broxy', return_value=self.broxy),
      patch('cw.runner._in_container', return_value=False),
      patch('cw.runner._provision_host_claude_dir', return_value=self.claude_config_dir),
      patch('cw.runner._project_root', return_value=Path('/main-repo')),
    ]
    entered = [p.__enter__() for p in self._patches]
    self.env = entered[0]
    self.env.pop('CW_BRO', None)
    self.env.pop('BROKER_CHANNEL', None)
    self.env.pop('CLAUDE_CONFIG_DIR', None)
    self.start_server = entered[2]
    self.build = entered[3]
    self.run_claude = entered[4]
    self.sync = entered[5]
    self.populate = entered[6]
    self.apply_auth = entered[7]
    self.start_broxy = entered[8]
    self.in_container = entered[9]
    self.provision_claude_dir = entered[10]
    return self

  def __exit__(self, *exception):
    for p in reversed(self._patches):
      p.__exit__(*exception)
    return False


class TestRunInPlace:
  def test_resume_without_session_errors(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert cw.runner.run_in_place(_spec(resume=True)) == 1
      assert h.run_claude.call_count == 0

  def test_resume_prepends_latest_session_id(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.projects_dir.mkdir()
      old = h.projects_dir / 'old.jsonl'
      old.write_text('{}')
      os.utime(old, (1, 1))
      (h.projects_dir / 'newer.jsonl').write_text('{}')
      assert cw.runner.run_in_place(_spec(resume=True, claude_args=['--foo'])) == 0
      assert h.build.call_args.kwargs['claude_args'] == ['--resume', 'newer', '--foo']

  def test_bro_session_serves_health_gates_and_syncs(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert cw.runner.run_in_place(_spec(bro='pm')) == 0
      assert h.start_server.call_args[0][0] == 'bro:pm'
      assert h.server.wait_healthy.call_count == 1
      assert h.server.stop.call_count == 1
      assert h.sync.call_count == 1
      assert h.env['CW_BRO'] == 'pm'
      assert h.build.call_args.kwargs['endpoint'] == h.server.endpoint
      # a --bro session reaches skills via the bro::skill tool, not --add-dir
      assert h.populate.call_count == 0
      assert h.build.call_args.kwargs['skills_dir'] is None

  def test_mcp_local_serves_flow_without_health_gate(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert cw.runner.run_in_place(_spec(mcp='local')) == 0
      assert h.start_server.call_args[0][0] == 'flow'
      assert h.server.wait_healthy.call_count == 0
      assert h.server.stop.call_count == 1
      assert h.sync.call_count == 0

  def test_native_session_starts_no_server(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert cw.runner.run_in_place(_spec(mcp='http')) == 0
      assert h.start_server.call_count == 0
      assert h.build.call_args.kwargs['endpoint'] is None

  def test_server_start_failure_returns_1(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.start_server.side_effect = RuntimeError('did not bind')
      assert cw.runner.run_in_place(_spec(mcp='local')) == 1
      assert h.run_claude.call_count == 0

  def test_health_gate_failure_stops_server_and_returns_1(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.server.wait_healthy.side_effect = RuntimeError('not healthy')
      assert cw.runner.run_in_place(_spec(bro='pm')) == 1
      assert h.run_claude.call_count == 0
      assert h.server.stop.call_count == 1

  def test_themed_native_session_populates_skills(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.env['CW_BRO'] = 'ppp-dev'
      assert cw.runner.run_in_place(_spec()) == 0
      skills_dir, bro_name = h.populate.call_args[0]
      assert bro_name == 'ppp-dev'
      assert h.build.call_args.kwargs['skills_dir'] == skills_dir

  def test_exports_bro_git_identity_unconditionally(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      # no --auto: every session commits as bro, autonomy-independent
      assert cw.runner.run_in_place(_spec()) == 0
      assert h.env['GIT_AUTHOR_NAME'] == 'bro'
      assert h.env['GIT_COMMITTER_EMAIL'] == 'dzhioev+bro@gmail.com'

  def test_session_context_set_next_to_claude(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert cw.runner.run_in_place(_spec()) == 0
      assert 'CW_SESSION_CONTEXT' in h.env

  def test_claude_exit_code_propagates(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.run_claude.return_value = 42
      assert cw.runner.run_in_place(_spec()) == 42

  def test_native_session_applies_auth_with_warning(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert cw.runner.run_in_place(_spec(mcp='http')) == 0
      assert h.apply_auth.call_args.kwargs == {'warn_when_missing': True}
      # the transformed env is the one claude is spawned with
      assert h.apply_auth.call_args.args[0] is h.run_claude.call_args.args[1]

  def test_bro_session_applies_auth_without_warning(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert cw.runner.run_in_place(_spec(bro='pm')) == 0
      assert h.apply_auth.call_args.kwargs == {'warn_when_missing': False}

  def test_host_session_provisions_and_exports_the_claude_config_dir(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert cw.runner.run_in_place(_spec()) == 0
      h.provision_claude_dir.assert_called_once_with('w', tmp_path, Path('/main-repo'))
      env = h.run_claude.call_args.args[1]
      assert env['CLAUDE_CONFIG_DIR'] == str(h.claude_config_dir)

  def test_container_session_keeps_the_default_claude_config(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.in_container.return_value = True
      assert cw.runner.run_in_place(_spec()) == 0
      h.provision_claude_dir.assert_not_called()
      assert 'CLAUDE_CONFIG_DIR' not in h.run_claude.call_args.args[1]


class TestSessionBroxy:
  def test_rewrites_the_channel_and_stops_the_broxy(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.env['BROKER_CHANNEL'] = 'unix:/up.sock'
      assert cw.runner.run_in_place(_spec()) == 0
      assert h.start_broxy.call_args.args[0] == 'unix:/up.sock'
      env = h.run_claude.call_args.args[1]
      assert env['BROKER_CHANNEL'] == h.broxy.address
      h.broxy.stop.assert_called_once()

  def test_unsets_the_channel_when_the_broxy_cannot_start(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.env['BROKER_CHANNEL'] = 'unix:/up.sock'
      h.start_broxy.return_value = None
      assert cw.runner.run_in_place(_spec()) == 0
      env = h.run_claude.call_args.args[1]
      assert 'BROKER_CHANNEL' not in env

  def test_container_mode_keeps_the_entrypoint_owned_channel(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      h.in_container.return_value = True
      h.env['BROKER_CHANNEL'] = 'unix:/tmp/broxy.sock'
      assert cw.runner.run_in_place(_spec()) == 0
      h.start_broxy.assert_not_called()
      env = h.run_claude.call_args.args[1]
      assert env['BROKER_CHANNEL'] == 'unix:/tmp/broxy.sock'

  def test_no_channel_starts_no_broxy(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert cw.runner.run_in_place(_spec()) == 0
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
    assert cw.runner._run_claude([], env) == 7
    assert signal.getsignal(signal.SIGTERM) == previous
