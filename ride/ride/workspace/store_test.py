from pathlib import PurePosixPath

import pytest

import ride.workspace.store as workspace_store


class TestFinalizeScopedSecrets:
  def test_scope_entries_must_be_kinds(self):
    with pytest.raises(ValueError, match="use kind 'github'"):
      workspace_store.ScopedSecrets({'github+reviewer'}, set())


class TestLogScopedSecrets:
  def test_logs_sorted_required_and_the_optional_remainder(self, caplog):
    with caplog.at_level('INFO'):
      workspace_store.log_scoped_secrets('ws', {'github', 'aws'}, {'openai', 'github'})
    assert 'scoped secrets for ws: aws, github' in caplog.text
    # the optional line reports only names not already required
    assert 'optional secrets for ws: openai' in caplog.text

  def test_empty_scope_logs_none_and_skips_the_optional_line(self, caplog):
    with caplog.at_level('INFO'):
      workspace_store.log_scoped_secrets('ws', set(), set())
    assert 'scoped secrets for ws: (none)' in caplog.text
    assert 'optional' not in caplog.text


class TestMaterializeScopedStore:
  def test_writes_store_with_restrictive_modes_and_returns_directory(self, tmp_path):
    store = {'creds.json': b'{}', 'creds/github.cred': b'tok'}
    directory = workspace_store.materialize_scoped_store(store, tmp_path / '.bro')
    assert directory == tmp_path / '.bro'
    assert (directory / 'creds/github.cred').read_bytes() == b'tok'
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / 'creds/github.cred').stat().st_mode & 0o777 == 0o600

  def test_recreates_the_directory_so_a_dropped_secret_does_not_linger(self, tmp_path):
    directory = tmp_path / '.bro'
    workspace_store.materialize_scoped_store(
      {'creds.json': b'{}', 'creds/aws.cred': b'v'}, directory
    )
    workspace_store.materialize_scoped_store({'creds.json': b'{}'}, directory)
    assert not (directory / 'creds/aws.cred').exists()


class TestStoreTarball:
  def _entries(self, blob: bytes) -> dict:
    import io
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(blob), mode='r') as tar:
      return {m.name: m for m in tar.getmembers()}

  def test_prefixes_root_and_round_trips_content(self):
    blob = workspace_store.store_tarball(
      {'creds/notion.cred': b'{"token": "t"}', 'creds.json': b'{}'}, PurePosixPath('.bro')
    )
    members = self._entries(blob)
    assert set(members) == {'.bro', '.bro/creds', '.bro/creds/notion.cred', '.bro/creds.json'}

    import io
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(blob), mode='r') as tar:
      extracted = tar.extractfile('.bro/creds/notion.cred')
      assert extracted is not None
      assert extracted.read() == b'{"token": "t"}'

  def test_nested_root_carries_its_ancestor_directories(self):
    members = self._entries(
      workspace_store.store_tarball({'creds/x.cred': b'v'}, PurePosixPath('.bro-party/m1/store'))
    )
    directories = {name for name, member in members.items() if member.isdir()}
    assert directories == {
      '.bro-party',
      '.bro-party/m1',
      '.bro-party/m1/store',
      '.bro-party/m1/store/creds',
    }
    assert set(members) - directories == {'.bro-party/m1/store/creds/x.cred'}
    assert all(members[name].mode == 0o700 for name in directories)

  def test_absolute_root_is_refused(self):
    with pytest.raises(ValueError, match='relative'):
      workspace_store.store_tarball({}, PurePosixPath('/home/ride/.bro'))

  def test_modes_and_owner(self):
    members = self._entries(
      workspace_store.store_tarball({'creds/notion.cred': b'x'}, PurePosixPath('.bro'))
    )
    assert members['.bro'].isdir()
    assert members['.bro'].mode == 0o700
    assert members['.bro/creds'].isdir()
    assert members['.bro/creds/notion.cred'].mode == 0o600
    # owned by the host uid/gid — the same uid the entrypoint remaps ride to on Linux
    assert members['.bro/creds/notion.cred'].uid == workspace_store.os.getuid()
    assert members['.bro/creds/notion.cred'].gid == workspace_store.os.getgid()
