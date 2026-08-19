from pathlib import Path

import pytest

from bro import monitor


class TestClaudeConfigDir:
  def test_names_the_dir_the_session_declares(self, tmp_path, monkeypatch):
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'w' / 'claude'))
    assert monitor.claude_config_dir() == tmp_path / 'w' / 'claude'
    assert monitor.in_claude_session()

  def test_outside_a_claude_session_there_is_no_config_dir(self, monkeypatch):
    monkeypatch.delenv('CLAUDE_CONFIG_DIR', raising=False)
    assert not monitor.in_claude_session()
    with pytest.raises(RuntimeError, match='CLAUDE_CONFIG_DIR'):
      monitor.claude_config_dir()


class TestSessionDir:
  def test_names_the_dir_the_session_declares(self, tmp_path, monkeypatch):
    monkeypatch.setenv('RIDE_SESSION_DIR', str(tmp_path / 'session'))
    assert monitor.session_dir() == tmp_path / 'session'
    assert monitor.harness_session_dir('claude') == tmp_path / 'session' / 'claude'

  def test_outside_a_managed_session_there_is_none(self, monkeypatch):
    monkeypatch.delenv('RIDE_SESSION_DIR', raising=False)
    assert monitor.session_dir() is None
    assert monitor.harness_session_dir('claude') is None

  def test_the_workspace_placement_is_a_record_beside_the_tree(self, tmp_path):
    assert monitor.workspace_session_dir(tmp_path / 'ws') == tmp_path / 'ws' / 'session'


class TestProjectsDir:
  def test_encodes_slashes_and_dots(self):
    assert (
      monitor.encode_project_path(Path('/home/u/project/.claude/wt'))
      == '-home-u-project--claude-wt'
    )

  def test_container_clone_encodes_to_the_mount_point(self):
    assert monitor.encode_project_path(Path('/workspace')) == '-workspace'

  def test_projects_dir_sits_under_the_active_config_root(self, tmp_path, monkeypatch):
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'session'))
    assert monitor.claude_projects_dir(Path('/workspace')) == (
      tmp_path / 'session' / 'projects' / '-workspace'
    )


class TestWorkingProjectsDir:
  def test_prefers_the_nearest_ancestor_with_a_transcript_dir(self, tmp_path, monkeypatch):
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'config'))
    workspace = tmp_path / 'ws'
    nested = workspace / 'sub' / 'deeper'
    nested.mkdir(parents=True)
    projects = monitor.claude_projects_dir(workspace)
    projects.mkdir(parents=True)
    monkeypatch.chdir(nested)
    monkeypatch.setenv('PWD', str(nested))
    assert monitor.working_projects_dir() == projects

  def test_falls_back_to_the_working_directory_itself(self, tmp_path, monkeypatch):
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'config'))
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setenv('PWD', str(workspace))
    assert monitor.working_projects_dir() == monitor.claude_projects_dir(workspace)
