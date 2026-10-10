import importlib.metadata
import json
import subprocess
from pathlib import Path

import pytest

from bro.base.source_commit import RECORD_NAME, SourceCommit, checkout_commit, installed_commit


def _git(*args: str, cwd: Path) -> str:
  return subprocess.run(
    ['git', '-c', 'user.email=t@t', '-c', 'user.name=t', *args],
    cwd=cwd,
    check=True,
    capture_output=True,
    text=True,
  ).stdout.strip()


@pytest.fixture
def checkout(tmp_path) -> Path:
  """a committed checkout holding `package/module.py` and `other.txt`."""
  root = tmp_path / 'checkout'
  (root / 'package').mkdir(parents=True)
  (root / 'package' / 'module.py').write_text('value = 1\n')
  (root / 'other.txt').write_text('other\n')
  _git('init', '-q', cwd=root)
  _git('add', '--all', cwd=root)
  _git('commit', '-q', '-m', 'initial', cwd=root)
  return root


def _head(root: Path) -> str:
  return _git('rev-parse', 'HEAD', cwd=root)


def _installed(tmp_path: Path, **files: str) -> importlib.metadata.Distribution:
  dist_info = tmp_path / 'site-packages' / 'demo-1.0.dist-info'
  dist_info.mkdir(parents=True)
  (dist_info / 'METADATA').write_text('Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n')
  for name, content in files.items():
    (dist_info / name).write_text(content)
  return importlib.metadata.Distribution.at(dist_info)


class TestCheckoutCommit:
  def test_a_clean_checkout_names_its_head(self, checkout):
    assert checkout_commit(checkout) == SourceCommit(_head(checkout), modified=False)

  def test_a_changed_tracked_file_marks_it_modified(self, checkout):
    (checkout / 'package' / 'module.py').write_text('value = 2\n')

    assert checkout_commit(checkout) == SourceCommit(_head(checkout), modified=True)

  def test_an_untracked_file_marks_it_modified(self, checkout):
    (checkout / 'package' / 'added.py').touch()

    assert checkout_commit(checkout / 'package') == SourceCommit(_head(checkout), modified=True)

  def test_an_untracked_file_marks_it_modified_whatever_status_shows(self, checkout):
    _git('config', 'status.showUntrackedFiles', 'no', cwd=checkout)
    (checkout / 'package' / 'added.py').touch()

    assert checkout_commit(checkout) == SourceCommit(_head(checkout), modified=True)

  def test_a_change_outside_the_directory_leaves_it_unmodified(self, checkout):
    (checkout / 'other.txt').write_text('changed\n')

    assert checkout_commit(checkout / 'package') == SourceCommit(_head(checkout), modified=False)

  def test_a_directory_outside_any_checkout_has_none(self, tmp_path):
    assert checkout_commit(tmp_path) is None

  def test_a_checkout_without_a_commit_has_none(self, tmp_path):
    _git('init', '-q', cwd=tmp_path)

    assert checkout_commit(tmp_path) is None


class TestInstalledCommit:
  def test_a_recorded_commit_wins_over_the_installation_source(self, tmp_path):
    recorded = SourceCommit('c0ffee', modified=True)
    archive = {'url': 'file:///bundle/wheels/demo-1.0-py3-none-any.whl', 'archive_info': {}}
    distribution = _installed(
      tmp_path, **{RECORD_NAME: recorded.to_json(), 'direct_url.json': json.dumps(archive)}
    )

    assert installed_commit(distribution) == recorded

  def test_a_version_control_installation_names_its_resolved_commit(self, tmp_path):
    direct_url = {
      'url': 'https://example.invalid/demo.git',
      'vcs_info': {'vcs': 'git', 'requested_revision': 'master', 'commit_id': 'c0ffee'},
    }
    distribution = _installed(tmp_path, **{'direct_url.json': json.dumps(direct_url)})

    assert installed_commit(distribution) == SourceCommit('c0ffee', modified=False)

  def test_an_editable_installation_reads_its_live_checkout(self, checkout, tmp_path):
    direct_url = {
      'url': checkout.as_uri(),
      'subdirectory': 'package',
      'dir_info': {'editable': True},
    }
    distribution = _installed(tmp_path, **{'direct_url.json': json.dumps(direct_url)})
    (checkout / 'package' / 'module.py').write_text('value = 2\n')

    assert installed_commit(distribution) == SourceCommit(_head(checkout), modified=True)

  def test_an_editable_installation_from_a_percent_named_checkout_reads_it(
    self, checkout, tmp_path
  ):
    named = checkout.rename(tmp_path / 'checkout%20name')
    direct_url = {'url': named.as_uri(), 'dir_info': {'editable': True}}
    distribution = _installed(tmp_path, **{'direct_url.json': json.dumps(direct_url)})

    assert installed_commit(distribution) == SourceCommit(_head(named), modified=False)

  @pytest.mark.parametrize(
    'direct_url',
    [
      None,
      {'url': 'file:///wheels/demo-1.0-py3-none-any.whl', 'archive_info': {}},
      {'url': 'file:///sources/demo', 'dir_info': {}},
    ],
    ids=['index', 'archive', 'directory'],
  )
  def test_an_installation_that_records_no_commit_has_none(self, tmp_path, direct_url):
    files = {} if direct_url is None else {'direct_url.json': json.dumps(direct_url)}

    assert installed_commit(_installed(tmp_path, **files)) is None

  def test_a_malformed_record_fails(self, tmp_path):
    distribution = _installed(tmp_path, **{RECORD_NAME: json.dumps({'commit': 'c0ffee'})})

    with pytest.raises(ValueError, match=RECORD_NAME):
      installed_commit(distribution)
