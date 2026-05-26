import os
import tempfile

import pytest

from dev.mcp import MCPServer, bash, edit_file, glob, grep, read_file, write_file


def test_read_file_returns_numbered_lines():
  with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, 'f.txt')
    write_file(path, 'aaa\nbbb\nccc\n')
    out = read_file(path)
    assert '    1\taaa\n' in out
    assert '    2\tbbb\n' in out
    assert '    3\tccc\n' in out


def test_read_file_offset_and_limit():
  with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, 'f.txt')
    write_file(path, '1\n2\n3\n4\n5\n')
    out = read_file(path, offset=2, limit=2)
    assert '    1\t' not in out
    assert '    3\t3\n' in out
    assert '    4\t4\n' in out
    assert '    5\t' not in out


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
    # file unchanged
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


def test_bash_captures_stdout_and_exit_code():
  result = bash('echo hello')
  assert 'exit_code: 0' in result
  assert 'hello' in result


def test_bash_captures_stderr():
  result = bash('echo oops 1>&2 ; false')
  assert 'exit_code: 1' in result
  assert 'oops' in result


def test_bash_timeout_returns_clearly():
  result = bash('sleep 5', timeout_seconds=1)
  assert 'TIMED OUT' in result


def test_grep_finds_match():
  with tempfile.TemporaryDirectory() as d:
    write_file(os.path.join(d, 'f.txt'), 'hello world\ngoodbye world\n')
    result = grep('hello', path=d)
    assert 'hello world' in result


def test_grep_no_match():
  with tempfile.TemporaryDirectory() as d:
    write_file(os.path.join(d, 'f.txt'), 'nothing here\n')
    assert grep('xyzzy', path=d) == 'no matches'


def test_grep_case_insensitive():
  with tempfile.TemporaryDirectory() as d:
    write_file(os.path.join(d, 'f.txt'), 'HELLO\n')
    assert 'HELLO' in grep('hello', path=d, case_insensitive=True)
    assert grep('hello', path=d) == 'no matches'


def test_grep_glob_filter():
  with tempfile.TemporaryDirectory() as d:
    write_file(os.path.join(d, 'a.py'), 'target\n')
    write_file(os.path.join(d, 'a.txt'), 'target\n')
    result = grep('target', path=d, glob='*.py')
    assert 'a.py' in result
    assert 'a.txt' not in result


def test_grep_head_limit():
  with tempfile.TemporaryDirectory() as d:
    write_file(os.path.join(d, 'f.txt'), 'x\n' * 10)
    result = grep('x', path=d, head_limit=3)
    assert result.count('\n') == 3  # 3 lines + trailing truncation marker
    assert 'truncated' in result


def test_glob_returns_matches_sorted_by_mtime():
  with tempfile.TemporaryDirectory() as d:
    write_file(os.path.join(d, 'old.py'), '')
    # bump mtime so 'new.py' is newer
    import time

    time.sleep(0.01)
    write_file(os.path.join(d, 'new.py'), '')
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


def test_mcpserver_no_args_lists_all_tools():
  import asyncio

  server = MCPServer()
  tools = asyncio.run(server.list_tools())
  names = {t.name for t in tools}
  assert names == {'read_file', 'write_file', 'edit_file', 'bash', 'grep', 'glob'}


def test_mcpserver_subset_filters_tools():
  import asyncio

  server = MCPServer('read_file', 'bash')
  tools = asyncio.run(server.list_tools())
  names = {t.name for t in tools}
  assert names == {'read_file', 'bash'}


def test_mcpserver_unknown_tool_raises():
  with pytest.raises(ValueError, match='unknown dev tools'):
    MCPServer('nope')
