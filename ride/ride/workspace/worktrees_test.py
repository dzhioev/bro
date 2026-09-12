import ride.workspace.worktrees as workspace_worktrees


class TestProvisionHostWorktree:
  def test_runs_the_worktree_setup_script(self, monkeypatch, tmp_path):
    from types import SimpleNamespace

    (tmp_path / 'setup.sh').write_text('#!/bin/sh\n')
    captured: dict = {}

    def fake_run(args, **kwargs):
      captured['args'] = args
      captured.update(kwargs)
      return SimpleNamespace(returncode=0)

    monkeypatch.setattr(workspace_worktrees.subprocess, 'run', fake_run)
    assert workspace_worktrees.provision_workspace(tmp_path) is True
    assert captured['args'] == [str(tmp_path / 'setup.sh')]
    assert captured['cwd'] == str(tmp_path)

  def test_missing_setup_script_skips_provisioning(self, monkeypatch, tmp_path, caplog):
    caplog.set_level('INFO')

    def fail_run(args, **kwargs):
      raise AssertionError('nothing should run for a script-less worktree')

    monkeypatch.setattr(workspace_worktrees.subprocess, 'run', fail_run)
    assert workspace_worktrees.provision_workspace(tmp_path) is True
    assert 'skipping project provisioning' in caplog.text

  def test_strips_ride_venv_manifest_from_the_provision_env(self, monkeypatch, tmp_path):
    from types import SimpleNamespace

    (tmp_path / 'setup.sh').write_text('#!/bin/sh\n')
    monkeypatch.setenv('RIDE_VENV_MANIFEST', '/opt/project-venv-manifest')
    captured: dict = {}

    def fake_run(args, **kwargs):
      captured.update(kwargs)
      return SimpleNamespace(returncode=0)

    monkeypatch.setattr(workspace_worktrees.subprocess, 'run', fake_run)
    assert workspace_worktrees.provision_workspace(tmp_path) is True
    assert 'RIDE_VENV_MANIFEST' not in captured['env']
