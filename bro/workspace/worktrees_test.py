import bro.workspace.worktrees as workspace_worktrees


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
    assert workspace_worktrees.provision_host_worktree(tmp_path) is True
    assert captured['args'] == [str(tmp_path / 'setup.sh')]
    assert captured['cwd'] == str(tmp_path)

  def test_missing_setup_script_skips_provisioning(self, monkeypatch, tmp_path):
    def fail_run(args, **kwargs):
      raise AssertionError('nothing should run for a script-less worktree')

    monkeypatch.setattr(workspace_worktrees.subprocess, 'run', fail_run)
    assert workspace_worktrees.provision_host_worktree(tmp_path) is True

  def test_strips_cw_venv_baked_from_the_provision_env(self, monkeypatch, tmp_path):
    from types import SimpleNamespace

    (tmp_path / 'setup.sh').write_text('#!/bin/sh\n')
    monkeypatch.setenv('CW_VENV_BAKED', '1')
    captured: dict = {}

    def fake_run(args, **kwargs):
      captured.update(kwargs)
      return SimpleNamespace(returncode=0)

    monkeypatch.setattr(workspace_worktrees.subprocess, 'run', fake_run)
    assert workspace_worktrees.provision_host_worktree(tmp_path) is True
    assert 'CW_VENV_BAKED' not in captured['env']


class TestEnsureHostWorktree:
  def _recorder(self, monkeypatch, *, branch_exists=False, submodule_returncode=0):
    from types import SimpleNamespace

    calls: list = []

    def fake_run(args, **kwargs):
      calls.append(args)
      if len(args) > 1 and args[1] == 'show-ref':
        return SimpleNamespace(returncode=0 if branch_exists else 1, stdout='')
      if 'submodule' in args:
        return SimpleNamespace(returncode=submodule_returncode, stdout='')
      return SimpleNamespace(returncode=0, stdout='')

    monkeypatch.setattr(workspace_worktrees.subprocess, 'run', fake_run)
    return calls

  def _add_command(self, calls):
    return next(c for c in calls if c[:3] == ['git', 'worktree', 'add'])

  def test_new_branch_uses_base_ref(self, monkeypatch, tmp_path):
    calls = self._recorder(monkeypatch)
    worktree = tmp_path / 'worktree'
    assert workspace_worktrees.ensure_host_worktree(worktree, 'worktree-x', 'sha123') is True
    assert self._add_command(calls) == [
      'git',
      'worktree',
      'add',
      '-q',
      str(worktree),
      '-b',
      'worktree-x',
      'sha123',
    ]

  def test_new_branch_defaults_to_head(self, monkeypatch, tmp_path):
    # the launcher's-HEAD rule: a new worktree bases on the checkout as it stands
    calls = self._recorder(monkeypatch)
    worktree = tmp_path / 'worktree'
    assert workspace_worktrees.ensure_host_worktree(worktree, 'worktree-x') is True
    assert self._add_command(calls) == [
      'git',
      'worktree',
      'add',
      '-q',
      str(worktree),
      '-b',
      'worktree-x',
      'HEAD',
    ]

  def test_existing_branch_ignores_base_ref(self, monkeypatch, tmp_path):
    calls = self._recorder(monkeypatch, branch_exists=True)
    worktree = tmp_path / 'worktree'
    assert workspace_worktrees.ensure_host_worktree(worktree, 'worktree-x', 'sha123') is True
    assert self._add_command(calls) == [
      'git',
      'worktree',
      'add',
      '-q',
      str(worktree),
      'worktree-x',
    ]

  def test_initializes_submodules_after_add(self, monkeypatch, tmp_path):
    # a superproject worktree needs its submodules before its setup.sh can run
    calls = self._recorder(monkeypatch)
    worktree = tmp_path / 'worktree'
    assert workspace_worktrees.ensure_host_worktree(worktree, 'worktree-x') is True
    update = next(c for c in calls if 'submodule' in c)
    assert update == ['git', '-C', str(worktree), 'submodule', 'update', '--init', '-q']
    assert calls.index(update) > calls.index(self._add_command(calls))

  def test_failed_submodule_init_fails(self, monkeypatch, tmp_path):
    self._recorder(monkeypatch, submodule_returncode=1)
    worktree = tmp_path / 'worktree'
    assert workspace_worktrees.ensure_host_worktree(worktree, 'worktree-x') is False

  def test_existing_dir_is_noop(self, monkeypatch, tmp_path):
    calls = self._recorder(monkeypatch)
    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    assert workspace_worktrees.ensure_host_worktree(worktree, 'worktree-x', 'sha123') is True
    assert calls == []
