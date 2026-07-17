import cw.worktrees


class TestProvisionHostWorktree:
  def test_strips_cw_venv_baked_from_the_provision_env(self, monkeypatch, tmp_path):
    from types import SimpleNamespace

    (tmp_path / 'setup').mkdir()
    (tmp_path / 'setup' / 'provision_repo.sh').write_text('#!/bin/sh\n')
    monkeypatch.setenv('CW_VENV_BAKED', '1')
    captured: dict = {}

    def fake_run(args, **kwargs):
      captured.update(kwargs)
      return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cw.worktrees.subprocess, 'run', fake_run)
    assert cw.worktrees._provision_host_worktree(tmp_path) is True
    assert 'CW_VENV_BAKED' not in captured['env']


class TestEnsureHostWorktree:
  def _recorder(self, monkeypatch, *, branch_exists=False):
    from types import SimpleNamespace

    calls: list = []

    def fake_run(args, **kwargs):
      calls.append(args)
      is_show_ref = len(args) > 1 and args[1] == 'show-ref'
      rc = 0 if (branch_exists or not is_show_ref) else 1
      return SimpleNamespace(returncode=rc, stdout='')

    monkeypatch.setattr(cw.worktrees.subprocess, 'run', fake_run)
    return calls

  def _add_command(self, calls):
    return next(c for c in calls if c[:3] == ['git', 'worktree', 'add'])

  def test_new_branch_uses_base_ref(self, monkeypatch, tmp_path):
    calls = self._recorder(monkeypatch)
    worktree = tmp_path / 'worktree'
    assert cw.worktrees._ensure_host_worktree(worktree, 'worktree-x', 'sha123') is True
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
    assert cw.worktrees._ensure_host_worktree(worktree, 'worktree-x') is True
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
    assert cw.worktrees._ensure_host_worktree(worktree, 'worktree-x', 'sha123') is True
    assert self._add_command(calls) == [
      'git',
      'worktree',
      'add',
      '-q',
      str(worktree),
      'worktree-x',
    ]

  def test_existing_dir_is_noop(self, monkeypatch, tmp_path):
    calls = self._recorder(monkeypatch)
    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    assert cw.worktrees._ensure_host_worktree(worktree, 'worktree-x', 'sha123') is True
    assert calls == []
