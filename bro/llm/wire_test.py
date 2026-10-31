import base64

import pytest

from bro.files import access
from bro.llm.wire import EXTENSION_TYPES, MIME_TYPE, File, Text, UserMessage


@pytest.mark.parametrize('value', [None, 1, b'text'])
def test_text_requires_a_string(value):
  with pytest.raises(TypeError):
    Text(value)


@pytest.mark.parametrize(
  'arguments,error',
  [
    ((bytearray(b'data'), 'image/png'), TypeError),
    ((b'', 'image/png'), ValueError),
    ((b'data', None), TypeError),
    ((b'data', 'Image/PNG'), ValueError),
    ((b'data', 'text/plain;charset=utf-8'), ValueError),
    ((b'data', 'image/*'), ValueError),
    ((b'data', 'pdf'), ValueError),
    ((b'data', 'image/png', 1), TypeError),
    ((b'data', 'image/png', ''), ValueError),
  ],
)
def test_file_rejects_invalid_content(arguments, error):
  with pytest.raises(error):
    File(*arguments)


@pytest.mark.parametrize(
  'content,error',
  [('text', TypeError), ([], TypeError), ((), ValueError), ((object(),), TypeError)],
)
def test_user_message_requires_nonempty_typed_tuple(content, error):
  with pytest.raises(error):
    UserMessage(content)


def test_content_preserves_empty_text_names_and_binary_bytes():
  file = File(b'\x00\xff\xfe', 'application/vnd.example+json', 'scans/march.pdf')
  assert file.data_url() == 'data:application/vnd.example+json;base64,' + base64.b64encode(
    file.data
  ).decode('ascii')
  assert UserMessage((Text(''), file)).content == (Text(''), file)
  assert file.filename == 'scans/march.pdf'


def test_extension_table_has_canonical_suffixes_and_types():
  for suffix, mime_type in EXTENSION_TYPES.items():
    assert suffix.startswith('.') and suffix == suffix.lower()
    assert MIME_TYPE.fullmatch(mime_type)


@pytest.mark.parametrize('name', ['report.pdf.gz', 'notes', 'scan.tiff'])
def test_unknown_suffix_fails_before_opening(name):
  with pytest.raises(ValueError, match=f'{name}.*mime_type'):
    File.from_path(name)


def test_from_path_reads_through_access_and_keeps_basename(tmp_path):
  backing = tmp_path / 'source'
  backing.write_bytes(b'image')

  class Access(access.AllowAll):
    def open_read(self, path):
      assert path == tmp_path / 'Chart.WEBP'
      return super().open_read(backing)

  with access.installed(Access()):
    assert File.from_path(tmp_path / 'Chart.WEBP') == File(b'image', 'image/webp', 'Chart.WEBP')


def test_from_path_accepts_explicit_type_and_propagates_access_errors(tmp_path):
  path = tmp_path / 'scan.tiff'
  path.write_bytes(b'image')
  assert File.from_path(path, 'image/tiff') == File(b'image', 'image/tiff', 'scan.tiff')
  with pytest.raises(FileNotFoundError):
    File.from_path(tmp_path / 'missing.pdf')
  with pytest.raises(ValueError, match='regular'):
    File.from_path(tmp_path, 'application/pdf')

  class Denied(access.AllowAll):
    def open_read(self, path):
      raise access.FileAccessDenied(path, 'refused')

  with access.installed(Denied()), pytest.raises(access.FileAccessDenied):
    File.from_path(path, 'image/tiff')
