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
    self.server = MagicMock()
    self.server.endpoint = MCPEndpoint(port=1234, token='tok')

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
      patch('cw.runner._claude_code_token_env', return_value={}),
    ]
    entered = [p.__enter__() for p in self._patches]
    self.env = entered[0]
    self.env.pop('CW_BRO', None)
    self.start_server = entered[2]
    self.build = entered[3]
    self.run_claude = entered[4]
    self.sync = entered[5]
    self.populate = entered[6]
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

  def test_auto_exports_bro_git_identity(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _Harness(tmp_path) as h:
      assert cw.runner.run_in_place(_spec(auto=True)) == 0
      assert h.env['GIT_AUTHOR_NAME'] == 'Bro'
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
