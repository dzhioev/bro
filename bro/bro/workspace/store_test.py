import pytest

import workspace.store


class TestFinalizeScopedSecrets:
  def test_grants_join_required_and_revokes_optional(self):
    scoped = workspace.store.ScopedSecrets({'github'}, {'openai'}, False)
    result = workspace.store.finalize_scoped_secrets(
      scoped, grant=['gmail_creds'], revoke=['openai']
    )
    assert result == workspace.store.ScopedSecrets({'github', 'gmail_creds'}, set(), False)

  def test_revoke_removes_required(self):
    scoped = workspace.store.ScopedSecrets({'github'}, {'openai'}, True)
    result = workspace.store.finalize_scoped_secrets(scoped, grant=[], revoke=['github'])
    assert result == workspace.store.ScopedSecrets(set(), {'openai'}, True)

  def test_grant_of_optional_secret_is_redundant(self):
    scoped = workspace.store.ScopedSecrets(set(), {'openai'}, True)
    with pytest.raises(ValueError, match='already in the scoped credential set'):
      workspace.store.finalize_scoped_secrets(scoped, grant=['openai'], revoke=[])

  def test_revoke_absent_from_both_tiers_errors(self):
    scoped = workspace.store.ScopedSecrets({'github'}, {'openai'}, True)
    with pytest.raises(ValueError, match='not in the scoped credential set'):
      workspace.store.finalize_scoped_secrets(scoped, grant=[], revoke=['aws'])


class TestLogScopedSecrets:
  def test_logs_sorted_required_and_the_optional_remainder(self, caplog):
    with caplog.at_level('INFO'):
      workspace.store.log_scoped_secrets('ws', {'github', 'aws'}, {'openai', 'github'})
    assert 'scoped secrets for ws: aws, github' in caplog.text
    # the optional line reports only names not already required
    assert 'optional (best-effort) secrets for ws: openai' in caplog.text

  def test_empty_scope_logs_none_and_skips_the_optional_line(self, caplog):
    with caplog.at_level('INFO'):
      workspace.store.log_scoped_secrets('ws', set(), set())
    assert 'scoped secrets for ws: (none)' in caplog.text
    assert 'optional' not in caplog.text


class TestMaterializeScopedStore:
  def test_writes_store_with_restrictive_modes_and_returns_registry(self, tmp_path):
    store = {'credentials.json': b'{}', 'github.cred': b'tok'}
    registry = workspace.store.materialize_scoped_store(store, tmp_path / '.bro')
    assert registry == tmp_path / '.bro' / 'credentials.json'
    assert (tmp_path / '.bro' / 'github.cred').read_bytes() == b'tok'
    assert (tmp_path / '.bro').stat().st_mode & 0o777 == 0o700
    assert (tmp_path / '.bro' / 'github.cred').stat().st_mode & 0o777 == 0o600

  def test_recreates_the_directory_so_a_dropped_secret_does_not_linger(self, tmp_path):
    directory = tmp_path / '.bro'
    workspace.store.materialize_scoped_store(
      {'credentials.json': b'{}', 'aws.cred': b'v'}, directory
    )
    workspace.store.materialize_scoped_store({'credentials.json': b'{}'}, directory)
    assert not (directory / 'aws.cred').exists()


class TestBroTarball:
  def _entries(self, blob: bytes) -> dict:
    import io
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(blob), mode='r') as tar:
      return {m.name: m for m in tar.getmembers()}

  def test_prefixes_bro_and_round_trips_content(self):
    blob = workspace.store._bro_tarball(
      {'notion.json': b'{"token": "t"}', 'credentials.json': b'{}'}
    )
    members = self._entries(blob)
    assert set(members) == {'.bro', '.bro/notion.json', '.bro/credentials.json'}

    import io
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(blob), mode='r') as tar:
      extracted = tar.extractfile('.bro/notion.json')
      assert extracted is not None
      assert extracted.read() == b'{"token": "t"}'

  def test_modes_and_owner(self):
    members = self._entries(workspace.store._bro_tarball({'notion.json': b'x'}))
    assert members['.bro'].isdir()
    assert members['.bro'].mode == 0o700
    assert members['.bro/notion.json'].mode == 0o600
    # owned by the host uid/gid — the same uid the entrypoint remaps cw to on Linux
    assert members['.bro/notion.json'].uid == workspace.store.os.getuid()
    assert members['.bro/notion.json'].gid == workspace.store.os.getgid()
