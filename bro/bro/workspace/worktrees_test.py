import cw.worktrees
from cw.worktrees import HostWorktree


class TestFinishHostWorktree:
  def _ws(self, monkeypatch, tmp_path, *, clean):
    ws = HostWorktree('feat', tmp_path)
    reasons = [] if clean else ['1 commit(s) not on origin/master']
    monkeypatch.setattr(ws, 'is_clean', lambda refresh_origin=True: (clean, reasons))
    removed: list = []
    monkeypatch.setattr(ws, 'remove', lambda: removed.append(ws.name))
    return ws, removed

  def test_interactive_drops_on_yes(self, monkeypatch, tmp_path):
    ws, removed = self._ws(monkeypatch, tmp_path, clean=True)
    monkeypatch.setattr(cw.worktrees, 'yesno', lambda q: True)
    cw.worktrees._finish_host_worktree(ws, interactive=True)
    assert removed == ['feat']

  def test_interactive_keeps_on_no(self, monkeypatch, tmp_path):
    ws, removed = self._ws(monkeypatch, tmp_path, clean=True)
    monkeypatch.setattr(cw.worktrees, 'yesno', lambda q: False)
    cw.worktrees._finish_host_worktree(ws, interactive=True)
    assert removed == []

  def test_non_interactive_keeps_and_never_prompts(self, monkeypatch, tmp_path):
    ws, removed = self._ws(monkeypatch, tmp_path, clean=False)

    def boom(q):
      raise AssertionError('must not prompt in a non-interactive session')

    monkeypatch.setattr(cw.worktrees, 'yesno', boom)
    cw.worktrees._finish_host_worktree(ws, interactive=False)
    assert removed == []


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
    wt = tmp_path / 'wt'
    assert cw.worktrees._ensure_host_worktree(wt, 'worktree-x', 'sha123') is True
    assert self._add_command(calls) == [
      'git',
      'worktree',
      'add',
      str(wt),
      '-b',
      'worktree-x',
      'sha123',
    ]

  def test_new_branch_defaults_to_head(self, monkeypatch, tmp_path):
    calls = self._recorder(monkeypatch)
    wt = tmp_path / 'wt'
    assert cw.worktrees._ensure_host_worktree(wt, 'worktree-x') is True
    assert self._add_command(calls) == ['git', 'worktree', 'add', str(wt), '-b', 'worktree-x']

  def test_existing_branch_ignores_base_ref(self, monkeypatch, tmp_path):
    calls = self._recorder(monkeypatch, branch_exists=True)
    wt = tmp_path / 'wt'
    assert cw.worktrees._ensure_host_worktree(wt, 'worktree-x', 'sha123') is True
    assert self._add_command(calls) == ['git', 'worktree', 'add', str(wt), 'worktree-x']

  def test_existing_dir_is_noop(self, monkeypatch, tmp_path):
    calls = self._recorder(monkeypatch)
    wt = tmp_path / 'wt'
    wt.mkdir()
    assert cw.worktrees._ensure_host_worktree(wt, 'worktree-x', 'sha123') is True
    assert calls == []
