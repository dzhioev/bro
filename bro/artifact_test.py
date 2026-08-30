import hashlib
import os

import pytest

import bro.artifact as artifact
from bro.broker import brotocol


def _ref_of(content: bytes) -> str:
  return f'sha256:{hashlib.sha256(content).hexdigest()}'


class TestRefGrammar:
  @pytest.mark.parametrize('value', [f'sha256:{"0" * 64}', f'sha256:{"9af" * 21}c'])
  def test_well_formed_refs(self, value):
    assert artifact.is_ref(value)

  @pytest.mark.parametrize(
    'value',
    [
      None,
      42,
      '',
      'sha256:',
      f'sha256:{"0" * 63}',
      f'sha256:{"0" * 65}',
      f'sha256:{"G" * 64}',
      f'sha256:{"A" * 64}',  # hex must be lowercase, matching hexdigest output
      f'md5:{"0" * 64}',
      f' sha256:{"0" * 64}',
    ],
  )
  def test_malformed_refs(self, value):
    assert not artifact.is_ref(value)


class TestDigest:
  def test_file_digest_is_the_plain_content_digest(self, tmp_path):
    path = tmp_path / 'a.bin'
    path.write_bytes(b'payload')
    assert artifact.digest_path(path) == _ref_of(b'payload')

  def test_directory_digest_is_stable_across_identical_copies(self, tmp_path):
    for name in ('one', 'two'):
      root = tmp_path / name
      (root / 'sub').mkdir(parents=True)
      (root / 'a.txt').write_bytes(b'alpha')
      (root / 'sub' / 'b.txt').write_bytes(b'beta')
      os.symlink('a.txt', root / 'link')
    assert artifact.digest_path(tmp_path / 'one') == artifact.digest_path(tmp_path / 'two')

  def test_content_change_changes_the_directory_digest(self, tmp_path):
    (tmp_path / 'a.txt').write_bytes(b'alpha')
    before = artifact.digest_path(tmp_path)
    (tmp_path / 'a.txt').write_bytes(b'beta')
    assert artifact.digest_path(tmp_path) != before

  def test_executable_bit_changes_the_digest(self, tmp_path):
    path = tmp_path / 'tool'
    path.write_bytes(b'#!/bin/sh\n')
    before = artifact.digest_path(tmp_path)
    path.chmod(0o755)
    assert artifact.digest_path(tmp_path) != before

  def test_symlink_target_is_recorded_not_followed(self, tmp_path):
    (tmp_path / 'a.txt').write_bytes(b'alpha')
    os.symlink('a.txt', tmp_path / 'link')
    with_target_a = artifact.digest_path(tmp_path)
    os.remove(tmp_path / 'link')
    os.symlink('dangling-but-internal', tmp_path / 'link')
    assert artifact.digest_path(tmp_path) != with_target_a

  def test_absolute_symlink_target_is_refused(self, tmp_path):
    os.symlink('/etc/passwd', tmp_path / 'link')
    with pytest.raises(ValueError, match='escapes the directory'):
      artifact.digest_path(tmp_path)

  def test_escaping_relative_symlink_target_is_refused(self, tmp_path):
    (tmp_path / 'sub').mkdir()
    os.symlink('../../outside', tmp_path / 'sub' / 'link')
    with pytest.raises(ValueError, match='sub/link escapes the directory'):
      artifact.digest_path(tmp_path)

  def test_unsupported_entry_type_is_refused(self, tmp_path):
    os.mkfifo(tmp_path / 'pipe')
    with pytest.raises(ValueError, match='unsupported entry type at pipe'):
      artifact.digest_path(tmp_path)

  def test_missing_path_is_refused(self, tmp_path):
    with pytest.raises(ValueError, match='no file or directory'):
      artifact.digest_path(tmp_path / 'absent')


class _FakeClient:
  def __init__(self, result: brotocol.Message):
    self._result = result
    self.calls: list = []

  def __enter__(self):
    return self

  def __exit__(self, *exc_info):
    return None

  def call(self, kind, args, timeout, **kwargs):
    self.calls.append((kind, args, timeout))
    return self._result


def _serve(monkeypatch, payload: dict) -> _FakeClient:
  client = _FakeClient(brotocol.Message(type=brotocol.Tag.RESULT, payload=payload, quest='R'))
  monkeypatch.setattr(artifact, '_open_client', lambda: client)
  return client


REF = f'sha256:{"a" * 64}'


class TestClient:
  def test_mint_sends_the_kind_and_returns_ref_and_size(self, monkeypatch):
    client = _serve(monkeypatch, {'outcome': 'ok', 'value': {'ref': REF, 'size': 7}})
    minted = artifact.mint_artifact('out/bundle')
    assert minted == artifact.Minted(ref=REF, size=7)
    assert client.calls == [(artifact.MINT, {'path': 'out/bundle'}, artifact.DEFAULT_TIMEOUT)]

  def test_get_sends_the_kind_and_returns_the_path(self, monkeypatch):
    client = _serve(monkeypatch, {'outcome': 'ok', 'value': {'path': f'/var/ride/artifacts/{REF}'}})  # fmt: skip
    assert artifact.get_artifact(REF, timeout=5) == f'/var/ride/artifacts/{REF}'
    assert client.calls == [(artifact.GET, {'ref': REF}, 5)]

  def test_denied_raises_with_the_host_reason(self, monkeypatch):
    _serve(monkeypatch, {'outcome': 'denied', 'error': f'artifact {REF} is not shared with this peer'})  # fmt: skip
    with pytest.raises(artifact.ArtifactError, match='is not shared with this peer'):
      artifact.get_artifact(REF)

  def test_failed_raises_with_reason_and_diagnostic(self, monkeypatch):
    _serve(monkeypatch, {'outcome': 'failed', 'error': 'disk full', 'detail': {'reason': 'error'}})
    with pytest.raises(artifact.ArtifactError, match=r'failed \(error\); disk full'):
      artifact.mint_artifact('x')

  @pytest.mark.parametrize(
    'value',
    [
      None,
      'ref',
      {'ref': 'nope', 'size': 1},
      {'ref': REF},
      {'ref': REF, 'size': -1},
      {'ref': REF, 'size': True},
    ],  # fmt: skip
  )
  def test_malformed_mint_value_raises(self, monkeypatch, value):
    _serve(monkeypatch, {'outcome': 'ok', 'value': value})
    with pytest.raises(artifact.ArtifactError, match='malformed'):
      artifact.mint_artifact('x')

  def test_malformed_get_value_raises(self, monkeypatch):
    _serve(monkeypatch, {'outcome': 'ok', 'value': {}})
    with pytest.raises(artifact.ArtifactError, match='malformed get result'):
      artifact.get_artifact(REF)

  def test_no_channel_is_an_error(self, monkeypatch):
    monkeypatch.delenv('BROKER_CHANNEL', raising=False)
    with pytest.raises(artifact.ArtifactError, match='no broker channel'):
      artifact.mint_artifact('x')


class TestCli:
  def test_mint_prints_the_ref(self, monkeypatch, capsys):
    _serve(monkeypatch, {'outcome': 'ok', 'value': {'ref': REF, 'size': 7}})
    assert artifact.main(['artifact', 'mint', 'out/bundle']) == 0
    assert capsys.readouterr().out == f'{REF}\n'

  def test_get_prints_the_path(self, monkeypatch, capsys):
    _serve(monkeypatch, {'outcome': 'ok', 'value': {'path': '/var/ride/artifacts/x'}})
    assert artifact.main(['artifact', 'get', REF]) == 0
    assert capsys.readouterr().out == '/var/ride/artifacts/x\n'

  def test_denied_get_relays_the_reason(self, monkeypatch, capsys, caplog):
    _serve(monkeypatch, {'outcome': 'denied', 'error': 'nope'})
    assert artifact.main(['artifact', 'get', REF]) == 1
    assert capsys.readouterr().out == ''
    assert 'nope' in caplog.text

  def test_digest_runs_locally(self, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv('BROKER_CHANNEL', raising=False)
    path = tmp_path / 'a.bin'
    path.write_bytes(b'payload')
    assert artifact.main(['artifact', 'digest', str(path)]) == 0
    assert capsys.readouterr().out == f'{_ref_of(b"payload")}\n'

  def test_digest_relays_a_refusal(self, tmp_path, capsys, caplog):
    assert artifact.main(['artifact', 'digest', str(tmp_path / 'absent')]) == 1
    assert 'no file or directory' in caplog.text

  def test_usage_error_without_a_verb(self, caplog):
    assert artifact.main(['artifact']) == 2
    assert 'usage: artifact' in caplog.text
