import asyncio
import os
import tempfile
from pathlib import Path
from typing import BinaryIO

import pytest

from bro.base.text_window import DEFAULT_LIMIT
from bro.files import access
from bro.mcp import mount
from bro.native.dev_mcp import (
  edit_file,
  glob,
  grep,
  read_file,
  read_reference,
  toolset,
  write_file,
)


def test_read_file_returns_numbered_lines():
  with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, 'f.txt')
    write_file(path, 'aaa\nbbb\nccc\n')
    out = read_file(path)
    assert '    1\taaa' in out
    assert '    2\tbbb' in out
    assert '    3\tccc' in out
    # under cap → no markers
    assert 'skipped' not in out


def test_read_file_offset_emits_before_marker():
  with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, 'f.txt')
    write_file(path, '1\n2\n3\n4\n5\n')
    out = read_file(path, offset=2, limit=2)
    assert '    1\t' not in out
    assert '    3\t3' in out
    assert '    4\t4' in out
    assert '    5\t' not in out
    assert 'skipped before: 2 lines' in out
    assert 'skipped after: 1 lines' in out


def test_read_file_default_limit_caps_long_file():
  # produce DEFAULT_LIMIT + 50 lines; default should keep DEFAULT_LIMIT and
  # surface a `[...skipped after...]` marker noting 50 dropped lines.
  with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, 'f.txt')
    write_file(path, ''.join(f'line {i}\n' for i in range(DEFAULT_LIMIT + 50)))
    out = read_file(path)
    assert 'skipped before' not in out
    assert 'skipped after: 50 lines' in out
    assert '    1\tline 0' in out
    assert f'{DEFAULT_LIMIT:>5}\tline {DEFAULT_LIMIT - 1}' in out
    assert f'{DEFAULT_LIMIT + 1:>5}\t' not in out


def test_read_file_explicit_limit_returns_more():
  with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, 'f.txt')
    write_file(path, ''.join(f'line {i}\n' for i in range(DEFAULT_LIMIT + 50)))
    out = read_file(path, limit=DEFAULT_LIMIT + 50)
    assert 'skipped' not in out
    assert f'{DEFAULT_LIMIT + 50:>5}\tline {DEFAULT_LIMIT + 49}' in out


def test_write_file_overwrites_and_creates_parents():
  with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, 'a', 'b', 'c.txt')
    write_file(path, 'hi')
    assert open(path).read() == 'hi'
    write_file(path, 'bye')
    assert open(path).read() == 'bye'


def test_edit_file_unique_match():
  with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, 'f.txt')
    write_file(path, 'aaa\nbbb\nccc\n')
    edit_file(path, 'bbb', 'BBB')
    assert open(path).read() == 'aaa\nBBB\nccc\n'


def test_edit_file_multi_match_without_replace_all_raises():
  with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, 'f.txt')
    write_file(path, 'aaa\naaa\n')
    with pytest.raises(ValueError, match='occurs 2 times'):
      edit_file(path, 'aaa', 'X')
    assert open(path).read() == 'aaa\naaa\n'


def test_edit_file_replace_all_replaces_every_occurrence():
  with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, 'f.txt')
    write_file(path, 'aaa\naaa\n')
    edit_file(path, 'aaa', 'X', replace_all=True)
    assert open(path).read() == 'X\nX\n'


def test_edit_file_not_found_raises():
  with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, 'f.txt')
    write_file(path, 'aaa\n')
    with pytest.raises(ValueError, match='old_string not found'):
      edit_file(path, 'zzz', 'X')


def test_file_ops_reject_non_regular_file():
  # a FIFO would block open()/read_text forever; the file tools refuse it up front.
  with tempfile.TemporaryDirectory() as d:
    fifo = os.path.join(d, 'pipe')
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match='not a regular file'):
      read_file(fifo)
    with pytest.raises(ValueError, match='not a regular file'):
      write_file(fifo, 'x')
    with pytest.raises(ValueError, match='not a regular file'):
      edit_file(fifo, 'a', 'b')


@pytest.mark.asyncio
async def test_grep_skips_fifo_without_blocking():
  # -D skip means grep never reads a FIFO/device — even one named directly — so it
  # returns immediately instead of blocking on the open. timeout_seconds is a safety
  # net: a regression that drops -D skip fails as TIMED OUT here rather than hanging.
  with tempfile.TemporaryDirectory() as d:
    fifo = os.path.join(d, 'pipe')
    os.mkfifo(fifo)
    assert await grep('anything', path=fifo, timeout_seconds=5) == 'no matches'


@pytest.mark.asyncio
async def test_grep_finds_match():
  with tempfile.TemporaryDirectory() as d:
    write_file(os.path.join(d, 'f.txt'), 'hello world\ngoodbye world\n')
    result = await grep('hello', path=d)
    assert 'hello world' in result
    assert 'skipped' not in result


@pytest.mark.asyncio
async def test_grep_no_match():
  with tempfile.TemporaryDirectory() as d:
    write_file(os.path.join(d, 'f.txt'), 'nothing here\n')
    assert await grep('xyzzy', path=d) == 'no matches'


@pytest.mark.asyncio
async def test_grep_case_insensitive():
  with tempfile.TemporaryDirectory() as d:
    write_file(os.path.join(d, 'f.txt'), 'HELLO\n')
    assert 'HELLO' in await grep('hello', path=d, case_insensitive=True)
    assert await grep('hello', path=d) == 'no matches'


@pytest.mark.asyncio
async def test_grep_glob_filter():
  with tempfile.TemporaryDirectory() as d:
    write_file(os.path.join(d, 'a.py'), 'target\n')
    write_file(os.path.join(d, 'a.txt'), 'target\n')
    result = await grep('target', path=d, glob='*.py')
    assert 'a.py' in result
    assert 'a.txt' not in result


@pytest.mark.asyncio
async def test_grep_explicit_limit_truncates_and_emits_after_marker():
  with tempfile.TemporaryDirectory() as d:
    write_file(os.path.join(d, 'f.txt'), 'x\n' * 10)
    result = await grep('x', path=d, limit=3)
    assert result.count('\n') == 3  # 3 kept lines + after marker (no trailing \n)
    assert 'skipped after: 7 lines' in result


@pytest.mark.asyncio
async def test_grep_default_limit_caps_pathological_output():
  # without an explicit limit the old grep returned everything — a runaway result
  # crashed do.do. now we get DEFAULT_LIMIT lines + a marker reporting the rest.
  with tempfile.TemporaryDirectory() as d:
    write_file(os.path.join(d, 'f.txt'), 'match\n' * (DEFAULT_LIMIT * 3))
    result = await grep('match', path=d)
    assert 'skipped after:' in result
    assert f'{DEFAULT_LIMIT * 2:,} lines' in result


def test_glob_returns_matches_sorted_by_mtime():
  with tempfile.TemporaryDirectory() as d:
    for name, modified in (('old.py', 1_000), ('new.py', 2_000)):
      write_file(os.path.join(d, name), '')
      os.utime(os.path.join(d, name), (modified, modified))
    result = glob('*.py', path=d).splitlines()
    assert result[0].endswith('new.py')
    assert result[1].endswith('old.py')


def test_glob_excludes_non_matches():
  with tempfile.TemporaryDirectory() as d:
    write_file(os.path.join(d, 'a.py'), '')
    write_file(os.path.join(d, 'b.txt'), '')
    result = glob('*.py', path=d)
    assert 'a.py' in result
    assert 'b.txt' not in result


def test_glob_no_matches():
  with tempfile.TemporaryDirectory() as d:
    assert glob('*.nonexistent', path=d) == 'no matches'


def test_toolset_build_lists_only_file_and_search_tools():
  server = toolset.build()
  tools = asyncio.run(server.list_tools())
  names = {tool.name for tool in tools}
  assert names == {
    'read_reference',
    'read_file',
    'write_file',
    'edit_file',
    'grep',
    'glob',
  }
  assert names.isdisjoint({'bash', 'job', 'watch', 'kill'})


def test_read_reference_returns_file_contents():
  assert read_reference().startswith('# dev tools reference')


def test_toolset_subset_filters_tools():
  server = toolset.build('read_file', 'grep')
  tools = asyncio.run(server.list_tools())
  names = {t.name for t in tools}
  assert names == {'read_file', 'grep'}


def test_mount_unknown_tool_raises():
  with pytest.raises(ValueError, match='unknown dev tools'):
    mount(toolset, 'nope')


_WIRED_TOOLS = {
  'read_file': ('open_read',),
  'write_file': ('open_write',),
  'edit_file': ('open_read', 'open_write'),
  'grep': ('search_directory',),
  'glob': ('search_directory',),
}
_PATH_FREE_TOOLS = {'read_reference'}


class RecordingAccess(access.AllowAll):
  def __init__(self):
    self.calls: list[tuple[str, Path, bool]] = []
    self.streams: list[BinaryIO] = []

  def open_read(self, path: Path) -> BinaryIO:
    self.calls.append(('open_read', path, False))
    stream = super().open_read(path)
    self.streams.append(stream)
    return stream

  def open_write(self, path: Path, *, create_parents: bool = False) -> BinaryIO:
    self.calls.append(('open_write', path, create_parents))
    stream = super().open_write(path, create_parents=create_parents)
    self.streams.append(stream)
    return stream

  def search_directory(self, path: Path) -> Path:
    self.calls.append(('search_directory', path, False))
    return super().search_directory(path)


@pytest.mark.asyncio
@pytest.mark.parametrize('name', _WIRED_TOOLS)
async def test_every_path_taking_tool_consults_access(name, tmp_path):
  path = tmp_path / 'file'
  path.write_text('before')
  arguments = {
    'read_file': {'file_path': str(path)},
    'write_file': {'file_path': str(path), 'content': 'after'},
    'edit_file': {'file_path': str(path), 'old_string': 'before', 'new_string': 'after'},
    'grep': {'pattern': 'before', 'path': str(tmp_path)},
    'glob': {'pattern': '*', 'path': str(tmp_path)},
  }
  tools = {tool.name: tool for tool in await toolset.build().list_tools()}
  recording = RecordingAccess()
  with access.installed(recording):
    await tools[name].call(arguments[name])
  expected_path = tmp_path if name in {'grep', 'glob'} else path
  assert recording.calls == [
    (operation, expected_path, name == 'write_file') for operation in _WIRED_TOOLS[name]
  ]
  assert all(stream.closed for stream in recording.streams)


@pytest.mark.asyncio
async def test_access_wiring_covers_registered_dev_tools():
  names = {tool.name for tool in await toolset.build().list_tools()}
  assert names == _WIRED_TOOLS.keys() | _PATH_FREE_TOOLS
  assert _PATH_FREE_TOOLS.isdisjoint(_WIRED_TOOLS)
  recording = RecordingAccess()
  with access.installed(recording):
    read_reference()
  assert not recording.calls


class DenyingAccess(access.AllowAll):
  def open_read(self, path: Path) -> BinaryIO:
    raise access.FileAccessDenied(path, 'test read denial')

  def open_write(self, path: Path, *, create_parents: bool = False) -> BinaryIO:
    raise access.FileAccessDenied(path, 'test write denial')

  def search_directory(self, path: Path) -> Path:
    raise access.FileAccessDenied(path, 'test search denial')


@pytest.mark.asyncio
@pytest.mark.parametrize('name', _WIRED_TOOLS)
async def test_access_denial_propagates_without_touching_paths(name, tmp_path):
  path = tmp_path / 'missing' / 'file'
  arguments = {
    'read_file': {'file_path': str(path)},
    'write_file': {'file_path': str(path), 'content': 'after'},
    'edit_file': {'file_path': str(path), 'old_string': 'before', 'new_string': 'after'},
    'grep': {'pattern': 'before', 'path': str(path)},
    'glob': {'pattern': '*', 'path': str(path)},
  }
  tools = {tool.name: tool for tool in await toolset.build().list_tools()}
  with access.installed(DenyingAccess()), pytest.raises(access.FileAccessDenied):
    await tools[name].call(arguments[name])
  assert list(tmp_path.iterdir()) == []


def test_edit_write_is_separately_authorized(tmp_path):
  class ReadOnlyAccess(DenyingAccess):
    def open_read(self, path: Path) -> BinaryIO:
      return access.AllowAll().open_read(path)

  path = tmp_path / 'file'
  path.write_text('before')
  with access.installed(ReadOnlyAccess()), pytest.raises(access.FileAccessDenied):
    edit_file(str(path), 'before', 'after')
  assert path.read_text() == 'before'


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['grep', 'glob'])
async def test_search_uses_authorized_path(name, tmp_path):
  authorized = tmp_path / 'authorized'
  authorized.mkdir()
  (authorized / 'public').write_text('public')
  requested = tmp_path / 'requested'
  requested.mkdir()
  (requested / 'private').write_text('private')

  class RedirectedAccess(access.AllowAll):
    def search_directory(self, path: Path) -> Path:
      assert path == requested
      return authorized

  with access.installed(RedirectedAccess()):
    if name == 'grep':
      result = await grep('.', path=str(requested))
    else:
      result = glob('*', path=str(requested))
  assert 'public' in result
  assert 'private' not in result


@pytest.mark.parametrize('pattern', ['/tmp/*', '../*', 'nested/../../*', '**/../*'])
def test_glob_rejects_escaping_patterns(tmp_path, pattern):
  with pytest.raises(ValueError, match='relative.*no .. components'):
    glob(pattern, path=str(tmp_path))


def test_glob_follows_explicit_symlinked_directory(tmp_path):
  outside = tmp_path / 'outside'
  outside.mkdir()
  (outside / 'file').touch()
  base = tmp_path / 'base'
  base.mkdir()
  (base / 'link').symlink_to(outside, target_is_directory=True)
  assert str(base / 'link' / 'file') in glob('link/*', path=str(base))


def test_file_tools_preserve_text_encoding_and_newlines(tmp_path):
  path = tmp_path / 'file'
  path.write_bytes('Привет\r\nsecond\rline'.encode())
  assert read_file(str(path)).splitlines() == ['    1\tПривет', '    2\tsecond', '    3\tline']
  edit_file(str(path), 'second', 'мир')
  assert path.read_bytes() == 'Привет\nмир\nline'.encode()
  write_file(str(path), 'Привет\n')
  assert path.read_text() == 'Привет\n'


@pytest.mark.parametrize('name', ['read_file', 'write_file', 'edit_file'])
def test_file_tools_use_authorized_streams(name, tmp_path):
  requested = tmp_path / 'requested'
  requested.write_text('private')
  authorized = tmp_path / 'authorized'
  authorized.write_text('before')

  class RedirectedAccess(access.AllowAll):
    def open_read(self, path: Path) -> BinaryIO:
      assert path == requested
      return super().open_read(authorized)

    def open_write(self, path: Path, *, create_parents: bool = False) -> BinaryIO:
      assert path == requested
      return super().open_write(authorized, create_parents=create_parents)

  with access.installed(RedirectedAccess()):
    if name == 'read_file':
      assert 'before' in read_file(str(requested))
    elif name == 'write_file':
      write_file(str(requested), 'after')
    else:
      edit_file(str(requested), 'before', 'after')
  assert authorized.read_text() == ('before' if name == 'read_file' else 'after')
  assert requested.read_text() == 'private'
