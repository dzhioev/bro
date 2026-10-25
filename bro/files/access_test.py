import os
import socket
from pathlib import Path

import pytest

from bro.files import access


@pytest.fixture(params=['pinned', 'fallback'])
def opener(request, monkeypatch):
  if request.param == 'pinned' and not hasattr(os, 'O_PATH'):
    pytest.skip('O_PATH unavailable')
  if request.param == 'fallback':
    monkeypatch.delattr(os, 'O_PATH', raising=False)
  return access.AllowAll()


def test_binary_read_write_and_truncation(opener, tmp_path):
  path = tmp_path / 'file'
  path.write_bytes(b'long original\r\n')
  with opener.open_read(path) as stream:
    assert stream.read() == b'long original\r\n'
  with opener.open_write(path) as stream:
    stream.write(b'new\x00')
  assert path.read_bytes() == b'new\x00'


def test_missing_read_creates_nothing(opener, tmp_path):
  path = tmp_path / 'missing' / 'file'
  with pytest.raises(FileNotFoundError):
    opener.open_read(path)
  assert list(tmp_path.iterdir()) == []


def test_write_creates_parents_only_when_requested(opener, tmp_path):
  path = tmp_path / 'missing' / 'file'
  with pytest.raises(FileNotFoundError):
    opener.open_write(path)
  assert not path.parent.exists()
  with opener.open_write(path, create_parents=True) as stream:
    stream.write(b'created')
  assert path.read_bytes() == b'created'


@pytest.mark.parametrize('replacement', ['file', 'symlink'])
def test_missing_write_creation_is_exclusive(opener, tmp_path, monkeypatch, replacement):
  path = tmp_path / 'new'
  other = tmp_path / 'other'
  other.write_bytes(b'untouched')
  original_open = os.open

  def race_open(target, flags, *args, **kwargs):
    if target == path and flags & os.O_CREAT:
      if replacement == 'file':
        path.write_bytes(b'untouched')
      else:
        path.symlink_to(other)
    return original_open(target, flags, *args, **kwargs)

  monkeypatch.setattr(os, 'open', race_open)
  with pytest.raises(FileExistsError):
    opener.open_write(path)
  assert path.read_bytes() == b'untouched'
  assert other.read_bytes() == b'untouched'


@pytest.mark.parametrize('kind', ['fifo', 'device', 'directory', 'socket'])
@pytest.mark.parametrize('write', [False, True])
def test_non_regular_files_are_refused_unopened(opener, tmp_path, monkeypatch, kind, write):
  path = tmp_path / 'special'
  with socket.socket(socket.AF_UNIX) as listener:
    if kind == 'fifo':
      os.mkfifo(path)
    elif kind == 'device':
      path = Path('/dev/null')
    elif kind == 'directory':
      path.mkdir()
    else:
      listener.bind(str(path))
    original_open = os.open
    attempts = []

    def record_open(target, flags, *args, **kwargs):
      attempts.append(flags)
      return original_open(target, flags, *args, **kwargs)

    monkeypatch.setattr(os, 'open', record_open)
    method = opener.open_write if write else opener.open_read
    with pytest.raises(ValueError, match='not a regular file'):
      method(path)
    assert all(flags & getattr(os, 'O_PATH', 0) for flags in attempts)


@pytest.mark.parametrize('write', [False, True])
def test_regular_symlinks_are_followed(opener, tmp_path, write):
  target = tmp_path / 'target'
  target.write_bytes(b'original')
  link = tmp_path / 'link'
  link.symlink_to(target)
  if write:
    with opener.open_write(link) as stream:
      stream.write(b'changed')
    assert target.read_bytes() == b'changed'
    assert link.is_symlink()
  else:
    with opener.open_read(link) as stream:
      assert stream.read() == b'original'


@pytest.mark.parametrize('write', [False, True])
@pytest.mark.parametrize('replacement', ['file', 'fifo', 'device', 'directory'])
def test_pinned_open_never_opens_replacement(tmp_path, monkeypatch, write, replacement):
  if not hasattr(os, 'O_PATH'):
    pytest.skip('O_PATH unavailable')
  path = tmp_path / 'file'
  saved = tmp_path / 'saved'
  path.write_bytes(b'original')
  original_fstat = os.fstat
  original_open = os.open
  opens = []

  def swap_after_check(descriptor):
    status = original_fstat(descriptor)
    if not saved.exists():
      path.rename(saved)
      if replacement == 'file':
        path.write_bytes(b'replacement')
      elif replacement == 'fifo':
        os.mkfifo(path)
      elif replacement == 'device':
        path.symlink_to('/dev/null')
      else:
        path.mkdir()
    return status

  def record_open(target, flags, *args, **kwargs):
    opens.append((target, flags))
    return original_open(target, flags, *args, **kwargs)

  monkeypatch.setattr(os, 'fstat', swap_after_check)
  monkeypatch.setattr(os, 'open', record_open)
  opener = access.AllowAll()
  if write:
    with opener.open_write(path) as stream:
      stream.write(b'changed')
    assert saved.read_bytes() == b'changed'
  else:
    with opener.open_read(path) as stream:
      assert stream.read() == b'original'
  assert opens[0][0] == path and opens[0][1] & os.O_PATH
  assert len(opens) == 2
  assert str(opens[1][0]).startswith('/proc/self/fd/')
  assert opens[1][1] & os.O_NOCTTY and opens[1][1] & os.O_NONBLOCK
  if replacement == 'file':
    assert path.read_bytes() == b'replacement'


@pytest.mark.parametrize('write', [False, True])
@pytest.mark.parametrize('replacement', ['file', 'fifo', 'device'])
def test_fallback_rejects_swapped_file_before_truncation(tmp_path, monkeypatch, write, replacement):
  monkeypatch.delattr(os, 'O_PATH', raising=False)
  path = tmp_path / 'file'
  saved = tmp_path / 'saved'
  path.write_bytes(b'original')
  original_open = os.open
  opened = []

  def swap_before_open(target, flags, *args, **kwargs):
    if target == path:
      path.rename(saved)
      if replacement == 'file':
        path.write_bytes(b'replacement')
      elif replacement == 'fifo':
        os.mkfifo(path)
      else:
        path.symlink_to('/dev/null')
    try:
      descriptor = original_open(target, flags, *args, **kwargs)
    except OSError:
      assert flags & os.O_NONBLOCK
      raise
    opened.append(descriptor)
    return descriptor

  monkeypatch.setattr(os, 'open', swap_before_open)
  method = access.AllowAll().open_write if write else access.AllowAll().open_read
  # A nonblocking write-only FIFO open fails with ENXIO before fstat.
  expected_error = OSError if write and replacement == 'fifo' else ValueError
  with pytest.raises(expected_error):
    method(path)
  assert saved.read_bytes() == b'original'
  if replacement == 'file':
    assert path.read_bytes() == b'replacement'
  for descriptor in opened:
    with pytest.raises(OSError):
      os.fstat(descriptor)


def test_installed_restores_nested_contexts_after_failure():
  initial = access.current()
  first, second = access.AllowAll(), access.AllowAll()
  with access.installed(first):
    assert access.current() is first
    with pytest.raises(RuntimeError), access.installed(second):
      assert access.current() is second
      raise RuntimeError('stop')
    assert access.current() is first
  assert access.current() is initial


def test_denial_names_path_and_rule(tmp_path):
  error = access.FileAccessDenied(tmp_path, 'private')
  assert isinstance(error, PermissionError)
  assert str(tmp_path) in str(error) and 'private' in str(error)
  assert error.path == tmp_path and error.rule == 'private'


def test_allow_all_search_preserves_path():
  path = Path('relative/directory')
  assert access.AllowAll().search_directory(path) == path
