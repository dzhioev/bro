import tracemalloc
from contextlib import ExitStack
from pathlib import Path

import pytest

import bro.artifact_mcp as artifact_mcp
from bro.artifact import ArtifactError
from bro.base.text_window import BYTE_LIMIT

REF = f'sha256:{"a" * 64}'


def _serve(monkeypatch, path: Path) -> None:
  monkeypatch.setattr(artifact_mcp, 'get_artifact', lambda ref: str(path))


def test_read_returns_a_numbered_window(tmp_path, monkeypatch):
  artifact = tmp_path / 'artifact.txt'
  artifact.write_text('one\ntwo\nthree\nfour\n')
  _serve(monkeypatch, artifact)

  result = artifact_mcp.read(REF, offset=1, limit=2)

  assert '    2\ttwo' in result
  assert '    3\tthree' in result
  assert 'skipped before: 1 lines' in result
  assert 'skipped after: 1 lines' in result


def test_read_keeps_memory_bounded_for_a_large_line(tmp_path, monkeypatch):
  artifact = tmp_path / 'artifact.txt'
  artifact.write_bytes(b'x' * 8_000_000)
  _serve(monkeypatch, artifact)

  with ExitStack() as stack:
    tracemalloc.start()
    stack.callback(tracemalloc.stop)
    result = artifact_mcp.read(REF, limit=1)
    _, peak = tracemalloc.get_traced_memory()

  assert len(result) < BYTE_LIMIT + 100
  assert 'skipped after' in result
  assert peak < 2_000_000


def test_read_validates_text_after_the_visible_window(tmp_path, monkeypatch):
  artifact = tmp_path / 'artifact.txt'
  artifact.write_bytes(b'visible\n' + b'x' * 100_000 + b'\xff')
  _serve(monkeypatch, artifact)

  with pytest.raises(ValueError, match='not UTF-8 text'):
    artifact_mcp.read(REF, limit=1)


def test_grep_returns_matches_and_context_with_line_numbers(tmp_path, monkeypatch):
  artifact = tmp_path / 'artifact.txt'
  artifact.write_text('before\ntarget one\nmiddle\ntarget two\nafter\n')
  _serve(monkeypatch, artifact)

  result = artifact_mcp.grep(REF, r'target (one|two)', context=1)

  assert '1-before' in result
  assert '2:target one' in result
  assert '4:target two' in result
  assert '5-after' in result


def test_grep_refuses_a_line_that_cannot_be_searched_with_bounded_memory(tmp_path, monkeypatch):
  artifact = tmp_path / 'artifact.txt'
  artifact.write_bytes(b'x' * (BYTE_LIMIT + 1))
  _serve(monkeypatch, artifact)

  with pytest.raises(ValueError, match='grep cannot search it with bounded memory'):
    artifact_mcp.grep(REF, 'x')


def test_directory_ref_reads_the_named_file(tmp_path, monkeypatch):
  artifact = tmp_path / 'artifact'
  (artifact / 'nested').mkdir(parents=True)
  (artifact / 'nested' / 'record.txt').write_text('record\n')
  _serve(monkeypatch, artifact)

  assert '1\trecord' in artifact_mcp.read(REF, path='nested/record.txt')


def test_directory_ref_requires_a_path(tmp_path, monkeypatch):
  artifact = tmp_path / 'artifact'
  artifact.mkdir()
  _serve(monkeypatch, artifact)

  with pytest.raises(ValueError, match='directory; path is required'):
    artifact_mcp.read(REF)


def test_directory_path_cannot_escape_the_artifact(tmp_path, monkeypatch):
  artifact = tmp_path / 'artifact'
  artifact.mkdir()
  (tmp_path / 'outside.txt').write_text('outside')
  _serve(monkeypatch, artifact)

  with pytest.raises(ValueError, match='must stay inside'):
    artifact_mcp.read(REF, path='../outside.txt')


def test_non_text_artifact_is_refused(tmp_path, monkeypatch):
  artifact = tmp_path / 'artifact.bin'
  artifact.write_bytes(b'\xff\xfe')
  _serve(monkeypatch, artifact)

  with pytest.raises(ValueError, match='not UTF-8 text'):
    artifact_mcp.read(REF)


def test_unreachable_ref_relays_the_host_refusal(monkeypatch):
  def refuse(ref: str) -> str:
    raise ArtifactError(f'artifact {ref} is not shared with this peer')

  monkeypatch.setattr(artifact_mcp, 'get_artifact', refuse)

  with pytest.raises(ArtifactError, match='not shared with this peer'):
    artifact_mcp.read(REF)


def test_manual_peer_relays_the_missing_view_refusal(monkeypatch):
  def refuse(ref: str) -> str:
    raise ArtifactError('no artifact view is mounted for a manually launched worker')

  monkeypatch.setattr(artifact_mcp, 'get_artifact', refuse)

  with pytest.raises(ArtifactError, match='manually launched worker'):
    artifact_mcp.grep(REF, 'anything')
