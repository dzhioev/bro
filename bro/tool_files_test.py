import hashlib
import os

import pytest

import bro.artifact as artifact
import bro.tool_files as tool_files
from bro.artifact import ArtifactError, compose_ref
from bro.base.lulid import is_lulid
from bro.tool_files import file_name, final_extension, return_bytes, return_ref, staged, take
from bro.workspace.paths import MINT_DIR_ENV


@pytest.mark.parametrize(
  ('raw', 'expected'),
  [
    ('Invoice\u202efdp.exe', 'Invoicefdp.exe'),
    ('Q3 report (final).pdf', 'Q3_report_final_.pdf'),
    ('../../.ssh/authorized_keys', 'authorized_keys'),
    ('..\\..\\boot.ini', 'boot.ini'),
    ('$(reboot).pdf', '_reboot_.pdf'),
    ('-rf', 'rf'),
    ('отчёт.pdf', 'отчёт.pdf'),
    ('re\u0301sume\u0301.pdf', 'résumé.pdf'),
    ('हिंदी.pdf', 'हिंदी.pdf'),
    ('สวัสดี.pdf', 'สวัสดี.pdf'),
    ('\u0301abc.txt', 'abc.txt'),
    ('...', 'attachment.bin'),
    ('', 'attachment.bin'),
    ('a\u200bb\U000e0063\u200dc', 'ab_c'),
    ('a\ud800b', 'a_b'),
    ('a !? \t\nb.txt', 'a_b.txt'),
    ('-\u0301.\u093eabc', 'abc'),
    ('a_+-.9', 'a_+-.9'),
  ],
)
def test_external_name_normalization(raw, expected):
  assert file_name(raw, 'attachment.bin') == expected


@pytest.mark.parametrize('extension', ['', '.pdf', '.' + 'é' * 15 + 'a'])
def test_long_name_truncation_preserves_the_final_extension(extension):
  result = file_name('界' * 200 + extension, 'file.bin')

  assert result == '界' * ((255 - len(extension.encode())) // 3) + extension
  assert len(result.encode()) <= 255


def test_long_tail_without_final_extension_is_cut_from_the_end():
  assert file_name('a.' + 'x' * 300, 'file.bin') == 'a.' + 'x' * 253


@pytest.mark.parametrize(
  ('name', 'extension'),
  [
    ('file', ''),
    ('file.tar.gz', '.gz'),
    ('file.', '.'),
    ('file.' + 'x' * 31, '.' + 'x' * 31),
    ('file.' + 'x' * 32, ''),
    ('file.' + '界' * 10, '.' + '界' * 10),
    ('file.' + '界' * 11, ''),
  ],
)
def test_final_extension_uses_the_last_dot_and_a_byte_bound(name, extension):
  assert final_extension(name) == extension


INVALID_NAMES = [
  '',
  '.',
  '..',
  '-file',
  '.hidden',
  '\u0301file',
  '\u093efile',
  'a/b',
  'a\\b',
  'a b',
  'a\x00b',
  'a\nb',
  'a\ud800b',
  'a\u200bb',
  'a\u200db',
  'a\u20ddb',
  'x' * 256,
  'é' * 128,
]


@pytest.mark.parametrize('name', INVALID_NAMES)
def test_invalid_literal_name_is_refused_before_staging(name, tmp_path, monkeypatch):
  monkeypatch.setenv(MINT_DIR_ENV, str(tmp_path))

  with pytest.raises(ValueError, match='file name must be'):
    with staged(name):
      pytest.fail('an invalid name must not be staged')
  assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('name', INVALID_NAMES)
def test_fallback_must_obey_the_name_rule_even_when_unused(name):
  with pytest.raises(ValueError, match='file name must be'):
    file_name('valid.pdf', name)


@pytest.mark.parametrize(
  'name', ['a', '_file', '+file', 'हिंदी.pdf', 'e\u0301.txt', 'é' * 127 + 'a']
)
def test_valid_literal_names_stage_unique_entries(name, tmp_path, monkeypatch):
  monkeypatch.setenv(MINT_DIR_ENV, str(tmp_path))

  with staged(name) as first, staged(name) as second:
    assert first.path != second.path
    assert first.path.parent == tmp_path
    extension = final_extension(name)
    identifier = first.path.name[: -len(extension)] if extension else first.path.name
    assert is_lulid(identifier)
    assert first.path.name == identifier + extension
    assert first.path.read_bytes() == b''
    first.path.write_bytes(b'output')
  assert not first.path.exists()
  assert not second.path.exists()


def test_staged_cleans_up_a_producer_exception(tmp_path, monkeypatch):
  monkeypatch.setenv(MINT_DIR_ENV, str(tmp_path))

  with pytest.raises(RuntimeError, match='producer failed'):
    with staged('file.bin') as entry:
      entry.path.write_bytes(b'partial')
      raise RuntimeError('producer failed')
  assert list(tmp_path.iterdir()) == []


def test_staged_requires_a_launch_built_mint_directory(monkeypatch):
  monkeypatch.delenv(MINT_DIR_ENV, raising=False)

  with pytest.raises(ArtifactError, match=r'no mint directory \(RIDE_MINT_DIR unset\)'):
    with staged('file.bin'):
      pytest.fail('an unmanaged run must not stage')


def test_staged_uses_exclusive_creation(tmp_path, monkeypatch):
  monkeypatch.setenv(MINT_DIR_ENV, str(tmp_path))
  monkeypatch.setattr(tool_files, 'lulid', lambda: 'same-id')
  existing = tmp_path / 'same-id.png'
  existing.write_bytes(b'existing')

  with pytest.raises(FileExistsError):
    with staged('file.png'):
      pytest.fail('a collision must not overwrite an entry')
  assert existing.read_bytes() == b'existing'


@pytest.fixture
def materialization(tmp_path, monkeypatch):
  path = tmp_path / 'materialized'
  path.write_bytes(b'file content')
  calls = []

  def request(kind, arguments, timeout):
    calls.append((kind, arguments))
    assert kind == artifact.FETCH
    return {'path': str(path)}

  monkeypatch.setattr(artifact, '_call', request)
  return path, calls


def test_take_fetches_with_checked_and_keeps_the_original_name(materialization):
  path, calls = materialization
  ref = compose_ref('sha256:' + 'a' * 64, 'unsafe name.pdf')

  assert take(ref) == {'ref': ref, 'path': str(path), 'size': len(b'file content')}
  assert calls == [(artifact.FETCH, {'ref': ref, 'checked': True})]


@pytest.mark.parametrize(
  'ref', ['/etc/passwd', 'sha256:abc/file', 'sha256:' + 'a' * 64 + '/../file']
)
def test_take_refuses_a_malformed_ref_without_requesting_it(ref, materialization):
  _, calls = materialization
  with pytest.raises(ValueError, match='malformed artifact ref'):
    take(ref)
  assert calls == []


def test_take_refuses_a_directory(materialization):
  path, _ = materialization
  path.unlink()
  path.mkdir()
  with pytest.raises(ValueError, match='is a directory, not a file'):
    take(compose_ref('sha256:' + 'a' * 64, 'directory'))


def test_take_requires_a_regular_file(materialization):
  path, _ = materialization
  path.unlink()
  os.mkfifo(path)
  with pytest.raises(ValueError, match='is not a regular file'):
    take(compose_ref('sha256:' + 'a' * 64, 'pipe'))


def test_take_propagates_the_hosts_refusal(monkeypatch):
  def refuse(*arguments, **keywords):
    raise ArtifactError('artifact is not shared with this peer')

  monkeypatch.setattr(artifact, '_call', refuse)
  with pytest.raises(ArtifactError, match='is not shared with this peer'):
    take(compose_ref('sha256:' + 'a' * 64, 'file'))


def test_return_ref_normalizes_only_the_name(materialization):
  path, calls = materialization
  digest = 'sha256:' + 'a' * 64
  normalized = compose_ref(digest, 'statement_1_.pdf')

  assert return_ref(compose_ref(digest, 'statement (1).pdf'), 'download.bin') == {
    'ref': normalized,
    'path': str(path),
    'size': path.stat().st_size,
  }
  assert calls == [(artifact.FETCH, {'ref': normalized, 'checked': True})]


@pytest.mark.parametrize('failure', [None, 'mint', 'fetch'])
def test_return_bytes_mints_the_entry_before_fetching_and_always_removes_it(
  tmp_path, monkeypatch, failure
):
  mint_directory = tmp_path / 'mint'
  mint_directory.mkdir()
  monkeypatch.setenv(MINT_DIR_ENV, str(mint_directory))
  payload = b'rendered bytes'
  ref = compose_ref('sha256:' + hashlib.sha256(payload).hexdigest(), 'render.png')
  materialized = tmp_path / 'materialized'
  materialized.write_bytes(payload)
  requests = []

  def request(kind, arguments, timeout):
    requests.append(kind)
    if kind == artifact.MINT:
      assert arguments['name'] == 'render.png'
      entry = mint_directory / arguments['entry']
      assert is_lulid(entry.stem)
      assert entry.suffix == '.png'
      assert entry.read_bytes() == payload
      if failure == 'mint':
        raise ArtifactError('mint failed')
      return {'ref': ref, 'size': len(payload)}
    assert kind == artifact.FETCH
    assert arguments == {'ref': ref, 'checked': True}
    if failure == 'fetch':
      raise ArtifactError('fetch failed')
    return {'path': str(materialized)}

  monkeypatch.setattr(artifact, '_call', request)
  if failure is not None:
    with pytest.raises(ArtifactError, match=f'{failure} failed'):
      return_bytes(payload, 'render.png')
  else:
    assert return_bytes(payload, 'render.png') == {
      'ref': ref,
      'path': str(materialized),
      'size': len(payload),
    }
  assert requests == ([artifact.MINT] if failure == 'mint' else [artifact.MINT, artifact.FETCH])
  assert list(mint_directory.iterdir()) == []
