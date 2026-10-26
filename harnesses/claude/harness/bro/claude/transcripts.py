"""following a Claude Code session's transcript files as they grow.

Claude writes a session's main thread to one jsonl *segment* in the projects dir
and the turns of each subagent it spawns to a transcript of its own under the
segment's companion dir. Claude appends whole lines, so a reader takes the
complete ones and leaves a line still being written for its next read.
"""

import contextlib
import threading
from collections.abc import Generator
from pathlib import Path

import watchfiles


def subagent_transcripts(segment: Path) -> list[Path]:
  """the transcripts of the subagents `segment`'s session spawned."""
  return sorted(segment.with_suffix('').rglob('*.jsonl'))


def read_lines_after(path: Path, byte_offset: int) -> tuple[list[str], int]:
  """the complete lines `path` holds past `byte_offset`, and the offset after them."""
  with path.open('rb') as stream:
    stream.seek(byte_offset)
    payload = stream.read()
  complete_size = payload.rfind(b'\n') + 1
  if complete_size == 0:
    return [], byte_offset
  lines = payload[:complete_size].decode('utf-8').split('\n')[:-1]
  return lines, byte_offset + complete_size


class Changes:
  """what a transcript reader waits on between its reads: a write to a transcript
  under the watched dir, or a `notify` from its own process."""

  def __init__(self) -> None:
    self._changed = threading.Event()

  def notify(self) -> None:
    self._changed.set()

  def wait(self, timeout: float) -> bool:
    """block until a change since the last wait, or for `timeout` seconds; True
    when a change ended it. A write the notice reports has already landed, so a
    read after the wait returns sees it."""
    changed = self._changed.wait(timeout)
    self._changed.clear()
    return changed


def _is_transcript(change: watchfiles.Change, path: str) -> bool:
  del change
  return path.endswith('.jsonl')


@contextlib.contextmanager
def watching(projects_dir: Path) -> Generator[Changes]:
  """notify the yielded `Changes` of every write to a transcript under
  `projects_dir` while the block runs, creating the dir for claude when it has
  not yet."""
  projects_dir.mkdir(parents=True, exist_ok=True)
  changes = Changes()
  stop = threading.Event()

  def _watch() -> None:
    for _ in watchfiles.watch(projects_dir, watch_filter=_is_transcript, stop_event=stop):
      changes.notify()

  thread = threading.Thread(target=_watch, name='transcript-watch', daemon=True)
  thread.start()
  try:
    yield changes
  finally:
    stop.set()
    thread.join()
