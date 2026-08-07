from pathlib import Path

from bro import monitor


class TestClaudeConfigDir:
  def test_defaults_to_the_user_level_directory(self, monkeypatch):
    monkeypatch.delenv('CLAUDE_CONFIG_DIR', raising=False)
    assert monitor.claude_config_dir() == Path.home() / '.claude'

  def test_override_wins(self, tmp_path, monkeypatch):
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'cw-sessions' / 'w'))
    assert monitor.claude_config_dir() == tmp_path / 'cw-sessions' / 'w'


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
