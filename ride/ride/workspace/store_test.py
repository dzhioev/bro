import pytest

import ride.workspace.store as workspace_store


class TestFinalizeScopedSecrets:
  def test_grants_join_required_and_revokes_optional(self):
    scoped = workspace_store.ScopedSecrets({'github'}, {'openai'}, False)
    result = workspace_store.finalize_scoped_secrets(
      scoped, grant=['gmail_creds'], revoke=['openai']
    )
    assert result == workspace_store.ScopedSecrets({'github', 'gmail_creds'}, set(), False)

  def test_grant_replaces_the_selected_credential_of_the_same_kind(self):
    scoped = workspace_store.ScopedSecrets({'brog', 'github'}, {'openai'}, False)
    result = workspace_store.finalize_scoped_secrets(scoped, grant=['brog+github'], revoke=[])
    assert result == workspace_store.ScopedSecrets({'brog+github', 'github'}, {'openai'}, False)

  def test_grant_can_replace_an_instance_with_the_bare_kind(self):
    scoped = workspace_store.ScopedSecrets({'brog+github'}, set(), False)
    result = workspace_store.finalize_scoped_secrets(scoped, grant=['brog'], revoke=[])
    assert result.required == {'brog'}

  def test_replacing_an_optional_credential_promotes_the_grant_to_required(self):
    scoped = workspace_store.ScopedSecrets(set(), {'openai'}, False)
    result = workspace_store.finalize_scoped_secrets(scoped, grant=['openai+work'], revoke=[])
    assert result == workspace_store.ScopedSecrets({'openai+work'}, set(), False)

  def test_granting_two_instances_of_one_kind_errors(self):
    scoped = workspace_store.ScopedSecrets(set(), set(), False)
    with pytest.raises(ValueError, match='credential kind.*granted more than once'):
      workspace_store.finalize_scoped_secrets(
        scoped, grant=['brog+github', 'brog+linear'], revoke=[]
      )

  def test_explicit_revoke_of_the_replaced_name_is_redundant(self):
    scoped = workspace_store.ScopedSecrets({'brog'}, set(), False)
    with pytest.raises(ValueError, match="cannot revoke 'brog'"):
      workspace_store.finalize_scoped_secrets(scoped, grant=['brog+github'], revoke=['brog'])

  def test_revoke_removes_required(self):
    scoped = workspace_store.ScopedSecrets({'github'}, {'openai'}, True)
    result = workspace_store.finalize_scoped_secrets(scoped, grant=[], revoke=['github'])
    assert result == workspace_store.ScopedSecrets(set(), {'openai'}, True)

  def test_grant_of_optional_secret_is_redundant(self):
    scoped = workspace_store.ScopedSecrets(set(), {'openai'}, True)
    with pytest.raises(ValueError, match='already in the scoped credential set'):
      workspace_store.finalize_scoped_secrets(scoped, grant=['openai'], revoke=[])

  def test_revoke_absent_from_both_tiers_errors(self):
    scoped = workspace_store.ScopedSecrets({'github'}, {'openai'}, True)
    with pytest.raises(ValueError, match='not in the scoped credential set'):
      workspace_store.finalize_scoped_secrets(scoped, grant=[], revoke=['aws'])


class TestLogScopedSecrets:
  def test_logs_sorted_required_and_the_optional_remainder(self, caplog):
    with caplog.at_level('INFO'):
      workspace_store.log_scoped_secrets('ws', {'github', 'aws'}, {'openai', 'github'})
    assert 'scoped secrets for ws: aws, github' in caplog.text
    # the optional line reports only names not already required
    assert 'optional (best-effort) secrets for ws: openai' in caplog.text

  def test_empty_scope_logs_none_and_skips_the_optional_line(self, caplog):
    with caplog.at_level('INFO'):
      workspace_store.log_scoped_secrets('ws', set(), set())
    assert 'scoped secrets for ws: (none)' in caplog.text
    assert 'optional' not in caplog.text


class TestMaterializeScopedStore:
  def test_writes_store_with_restrictive_modes_and_returns_registry(self, tmp_path):
    store = {'credentials.json': b'{}', 'github.cred': b'tok'}
    registry = workspace_store.materialize_scoped_store(store, tmp_path / '.bro')
    assert registry == tmp_path / '.bro' / 'credentials.json'
    assert (tmp_path / '.bro' / 'github.cred').read_bytes() == b'tok'
    assert (tmp_path / '.bro').stat().st_mode & 0o777 == 0o700
    assert (tmp_path / '.bro' / 'github.cred').stat().st_mode & 0o777 == 0o600

  def test_recreates_the_directory_so_a_dropped_secret_does_not_linger(self, tmp_path):
    directory = tmp_path / '.bro'
    workspace_store.materialize_scoped_store(
      {'credentials.json': b'{}', 'aws.cred': b'v'}, directory
    )
    workspace_store.materialize_scoped_store({'credentials.json': b'{}'}, directory)
    assert not (directory / 'aws.cred').exists()


class TestBroTarball:
  def _entries(self, blob: bytes) -> dict:
    import io
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(blob), mode='r') as tar:
      return {m.name: m for m in tar.getmembers()}

  def test_prefixes_bro_and_round_trips_content(self):
    blob = workspace_store._bro_tarball(
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
    members = self._entries(workspace_store._bro_tarball({'notion.json': b'x'}))
    assert members['.bro'].isdir()
    assert members['.bro'].mode == 0o700
    assert members['.bro/notion.json'].mode == 0o600
    # owned by the host uid/gid — the same uid the entrypoint remaps ride to on Linux
    assert members['.bro/notion.json'].uid == workspace_store.os.getuid()
    assert members['.bro/notion.json'].gid == workspace_store.os.getgid()
