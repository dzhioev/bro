"""a Claude session's cumulative usage, kept current in its usage file.

The runner names the file to claude through `BRO_USAGE_FILE` (`bro.llm.usage`).
The publisher follows the newest segment the session wrote since it started,
plus the transcripts of the subagents that segment spawned, reading on every
transcript write, and replaces the file whenever the totals grow. Claude writes
one record per content block of a response, each repeating the response's
usage, so a message is billed once, by the Claude trail format's own
classification.
"""

import contextlib
import threading
import time
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from bro.base import log
from bro.llm.usage import (
  Counts,
  Usage,
  add,
  from_vendor_counts,
  holding_publisher_lock,
  write_usage_failure,
  write_usage_file,
  write_usage_pending,
  zero,
)
from bro.trails.claude_format import CLAUDE_FORMAT
from ride.claude.claude_config import latest_jsonl
from ride.claude.transcripts import read_lines_after, subagent_transcripts, watching

# a read without a write to wake it, for one the watch missed
_IDLE_READ_SECONDS = 3.0


class SessionUsage:
  """the billed usage of a session's transcripts, read as they grow."""

  def __init__(self, projects_dir: Path, *, started_after: float) -> None:
    self._projects_dir = projects_dir
    self._started_after = started_after
    self._segment: Optional[Path] = None
    self._offsets: dict[Path, int] = {}
    self._billed: set[str] = set()
    self._per_model: dict[str, Counts] = {}
    self._version: Optional[str] = None

  def refresh(self) -> Optional[Usage]:
    """the session's cumulative usage once what its transcripts grew by is read;
    None before the session has billed any."""
    segment = latest_jsonl(self._projects_dir)
    if segment is None or segment.stat().st_mtime < self._started_after:
      return None
    if segment != self._segment:
      self._start_segment(segment)
    for path in [segment, *subagent_transcripts(segment)]:
      lines, self._offsets[path] = read_lines_after(path, self._offsets.get(path, 0))
      for line in lines:
        self._count(line)
    if len(self._per_model) == 0:
      return None
    if self._version is None:
      raise ValueError(f'{segment} bills usage but names no Claude Code version')
    return Usage(
      agent=f'Claude Code {self._version}',
      per_model={model: dict(counts) for model, counts in self._per_model.items()},
    )

  def _start_segment(self, segment: Path) -> None:
    self._segment = segment
    self._offsets = {}
    self._billed = set()
    self._per_model = {}
    self._version = None

  def _count(self, line: str) -> None:
    classification = CLAUDE_FORMAT.classify(CLAUDE_FORMAT.parse(line))
    version = (classification.native_updates or {}).get('harness_version')
    if version is not None:
      self._version = version
    if classification.usage_model is None or classification.usage is None:
      return
    billing_key = classification.billing_key
    if billing_key is not None:
      if billing_key in self._billed:
        return
      self._billed.add(billing_key)
    model = classification.usage_model
    self._per_model[model] = add(
      self._per_model.get(model, zero()), from_vendor_counts(classification.usage)
    )


@contextlib.contextmanager
def publishing_usage(projects_dir: Path, target: Path) -> Generator[None]:
  """keep `target` holding the cumulative usage of the session claude records in
  `projects_dir` while the block runs, reading the transcripts once more as it
  ends; it holds a pending record from before the block starts. A failure to
  read or publish releases the publisher lock the records name and replaces
  `target` with the failure, and one that cannot be recorded there raises as the
  block ends."""
  target.parent.mkdir(parents=True, exist_ok=True)
  session = SessionUsage(projects_dir, started_after=time.time())
  stop = threading.Event()
  with (
    watching(projects_dir) as changes,
    ThreadPoolExecutor(max_workers=1, thread_name_prefix='usage-publisher') as executor,
  ):

    def _publish(lock: Path) -> None:
      published: Optional[Usage] = None
      while True:
        stopping = stop.is_set()
        current = session.refresh()
        if current is not None and current != published:
          write_usage_file(target, current, lock=lock)
          published = current
        if stopping:
          return
        changes.wait(_IDLE_READ_SECONDS)

    with contextlib.ExitStack() as setup:
      lock = setup.enter_context(holding_publisher_lock(target))
      write_usage_pending(target, lock)
      # the worker releases the lock when it stops publishing
      held = setup.pop_all()

    def _publish_or_record_failure() -> None:
      try:
        with held:
          _publish(lock)
      except Exception as error:
        log.exception('publishing the session usage failed')
        write_usage_failure(target, f'{type(error).__name__}: {error}')

    publishing = executor.submit(_publish_or_record_failure)
    try:
      yield
    finally:
      stop.set()
      changes.notify()
    publishing.result()
