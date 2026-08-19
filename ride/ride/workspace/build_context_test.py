import io
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

import ride.workspace.build_context as build_context


def _project(tmp_path: Path, members: list[str]) -> Path:
  project = tmp_path / 'project'
  project.mkdir()
  member_list = ', '.join(f'"{name}"' for name in members)
  (project / 'pyproject.toml').write_text(
    f'[project]\nname = "root"\n\n[tool.uv.workspace]\nmembers = [{member_list}]\n'
  )
  (project / 'uv.lock').write_text('lock')
  for name in members:
    (project / name).mkdir(parents=True)
    (project / name / 'pyproject.toml').write_text(f'[project]\nname = "{name}"\n')
  subprocess.run(['git', 'init', '-q'], cwd=project, check=True)
  subprocess.run(['git', 'add', '-A'], cwd=project, check=True)
  return project


def _config(monkeypatch, build_context_command=None):
  monkeypatch.setattr(
    build_context,
    'project_config',
    lambda: type('Config', (), {'build_context_command': build_context_command})(),
  )


def _names(archive_bytes: bytes) -> list[str]:
  with tarfile.open(fileobj=io.BytesIO(archive_bytes)) as archive:
    return [member.name for member in archive.getmembers() if member.isfile()]


class TestManifestPaths:
  def test_root_manifests_plus_every_member(self, tmp_path):
    project = _project(tmp_path, ['bro', 'bro-dev'])
    assert build_context.manifest_paths(project) == [
      'pyproject.toml',
      'uv.lock',
      'bro/pyproject.toml',
      'bro-dev/pyproject.toml',
    ]

  def test_no_workspace_table_yields_the_root_manifests(self, tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'pyproject.toml').write_text('[project]\nname = "solo"\n')
    (project / 'uv.lock').write_text('lock')
    assert build_context.manifest_paths(project) == ['pyproject.toml', 'uv.lock']

  def test_glob_members_expand(self, tmp_path):
    project = _project(tmp_path, ['packages/one', 'packages/two'])
    (project / 'pyproject.toml').write_text(
      '[project]\nname = "root"\n\n[tool.uv.workspace]\nmembers = ["packages/*"]\n'
    )
    assert build_context.manifest_paths(project) == [
      'pyproject.toml',
      'uv.lock',
      'packages/one/pyproject.toml',
      'packages/two/pyproject.toml',
    ]

  def test_excluded_member_is_dropped(self, tmp_path):
    project = _project(tmp_path, ['packages/one', 'packages/two'])
    (project / 'pyproject.toml').write_text(
      '[project]\nname = "root"\n\n[tool.uv.workspace]\n'
      'members = ["packages/*"]\nexclude = ["packages/two"]\n'
    )
    assert build_context.manifest_paths(project) == [
      'pyproject.toml',
      'uv.lock',
      'packages/one/pyproject.toml',
    ]

  def test_missing_lock_raises(self, tmp_path):
    project = _project(tmp_path, [])
    (project / 'uv.lock').unlink()
    with pytest.raises(FileNotFoundError, match='uv.lock'):
      build_context.manifest_paths(project)


class TestProjectFiles:
  def test_defaults_to_tracked_files(self, tmp_path, monkeypatch):
    _config(monkeypatch)
    project = _project(tmp_path, ['bro'])
    (project / 'untracked.txt').write_text('x')
    assert 'untracked.txt' not in build_context.project_files(project)
    assert 'bro/pyproject.toml' in build_context.project_files(project)

  def test_working_tree_content_wins_over_the_index(self, tmp_path, monkeypatch):
    _config(monkeypatch)
    project = _project(tmp_path, [])
    (project / 'uv.lock').write_text('edited locally')
    with tarfile.open(fileobj=io.BytesIO(build_context.assemble(project))) as archive:
      extracted = archive.extractfile('uv.lock')
      assert extracted is not None
      assert extracted.read() == b'edited locally'

  def test_configured_command_replaces_the_producer(self, tmp_path, monkeypatch):
    _config(monkeypatch, build_context_command='printf "uv.lock\\npyproject.toml\\n"')
    project = _project(tmp_path, [])
    assert build_context.project_files(project) == ['uv.lock', 'pyproject.toml']


class TestAssemble:
  def test_injects_the_framework_assets_at_fixed_paths(self, tmp_path, monkeypatch):
    _config(monkeypatch)
    project = _project(tmp_path, ['bro'])
    names = _names(build_context.assemble(project))
    assert build_context.DOCKERFILE_PATH in names
    for injected in build_context.FRAMEWORK_FILES:
      assert injected in names
    for relative in build_context.manifest_paths(project):
      assert f'{build_context.MANIFEST_PREFIX}/{relative}' in names

  def test_members_are_sorted_and_parents_precede_children(self, tmp_path, monkeypatch):
    _config(monkeypatch)
    project = _project(tmp_path, ['bro', 'bro-dev'])
    with tarfile.open(fileobj=io.BytesIO(build_context.assemble(project))) as archive:
      names = [member.name for member in archive.getmembers()]
    assert names == sorted(names)
    for name in names:
      parent = str(Path(name).parent)
      if parent != '.':
        assert names.index(parent) < names.index(name)

  def test_metadata_is_normalized(self, tmp_path, monkeypatch):
    _config(monkeypatch)
    project = _project(tmp_path, ['bro'])
    (project / 'run.sh').write_text('#!/bin/sh\n')
    (project / 'run.sh').chmod(0o700)
    subprocess.run(['git', 'add', '-A'], cwd=project, check=True)
    with tarfile.open(fileobj=io.BytesIO(build_context.assemble(project))) as archive:
      members = {member.name: member for member in archive.getmembers()}
    for member in members.values():
      assert member.mtime == 0
      assert member.uid == 0
      assert member.gid == 0
      assert member.uname == ''
      assert member.gname == ''
    assert members['run.sh'].mode == 0o755
    assert members['uv.lock'].mode == 0o644
    assert members[build_context.DOCKERFILE_PATH].mode == build_context._INJECTED_MODE
    assert (
      members[f'{build_context.INJECTED_PREFIX}/entrypoint.sh'].mode == build_context._INJECTED_MODE
    )

  def test_touching_a_file_leaves_the_archive_identical(self, tmp_path, monkeypatch):
    _config(monkeypatch)
    project = _project(tmp_path, ['bro'])
    before = build_context.assemble(project)
    os.utime(project / 'uv.lock', (10**9, 10**9))
    assert build_context.assemble(project) == before

  def test_a_project_file_under_the_reserved_prefix_raises(self, tmp_path, monkeypatch):
    _config(monkeypatch)
    project = _project(tmp_path, [])
    (project / build_context.INJECTED_PREFIX).mkdir()
    (project / build_context.INJECTED_PREFIX / 'Dockerfile').write_text('FROM scratch')
    subprocess.run(['git', 'add', '-A'], cwd=project, check=True)
    with pytest.raises(ValueError, match='reserved'):
      build_context.assemble(project)
