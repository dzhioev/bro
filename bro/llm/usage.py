#!/usr/bin/env python
"""shared LLM-usage accounting: one usage-reading surface for every environment.

Usage is kept as per-model counts in four disjoint billed token classes:
`input` (uncached prompt), `cache_write`, `cache_read`, and `output`.
`from_wire_counts` delegates raw vocabulary normalization to the named wire.

The cumulative usage of the agent whose work a process carries out is the
usage file the environment points at (`BRO_USAGE_FILE`), read by
`current_usage()`: a JSON snapshot of one publisher's per-model totals, written
atomically (temp + rename) and self-describing
(`{"agent": ..., "models": {slug: counts}}`), since the reader cannot trust the
environment for the agent — an in-process bro run inherits the launcher's
`RIDE_BRO`, not its own. A publisher writes its file before it names it to the
processes it starts, so a pointer naming no file raises. A publisher running
apart from the processes that read it names a lock it holds while it keeps the
file current, and first writes a pending record (`{"pending": true, ...}`) that
reads as no usage until it has billed some; a snapshot naming a lock nobody
holds raises. A publisher that can no longer keep its file current replaces it
with the reason (`{"failed": reason}`), which the reader raises rather than
crediting a stale snapshot. A native LLM loop publishes after every API call
into a file of its own process (`publish`).

This module also owns the commit-footer line format (one `>`-quoted line; `'`
thousands separator so it never collides with the `, ` joining model entries):

  > created with <agents> | <model>: ↑(<input> <cache_write> <cache_read>) ↓<output>[, …]

`<agents>` is a `, `-joined list of surface identities — `Claude Code <version>`
for a Claude Code session, the usage file's agent unversioned (a bro run
publishes `bro//<name>`, e.g. `bro//dev`); an aggregated footer unions the
agents of the commits it covers.
`↑(…)` groups the upload classes (input, cache_write, cache_read, in that
order); `↓` marks output. `parse_footer` also accepts the historic shape that
compressed same-agent versions (`Claude Code 2.1.114, 2.1.120`), normalizing
bare version tokens back to full `Claude Code <version>` agents.

The `usage` CLI prints `current_usage()` — the agent line, then one per-model
entry per line. The per-commit delta/baseline machinery on top of these
cumulatives lives in `bro.workflow.commit_footer`.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import sys
import tempfile
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from bro.base.args import Parser

__cli_name__ = 'usage'

USAGE_FILE_VARIABLE = 'BRO_USAGE_FILE'

_THOUSANDS = "'"
_UP = '↑'
_DOWN = '↓'

CLASSES = ('input', 'cache_write', 'cache_read', 'output')

# a per-model usage record is a plain dict keyed by CLASSES (JSON-friendly for
# state files and the usage file; arithmetic via the helpers below).
Counts = dict[str, int]


def zero() -> Counts:
  return dict.fromkeys(CLASSES, 0)


def add(a: Counts, b: Counts) -> Counts:
  return {c: a.get(c, 0) + b.get(c, 0) for c in CLASSES}


def subtract(a: Counts, b: Counts) -> Counts:
  return {c: a.get(c, 0) - b.get(c, 0) for c in CLASSES}


def from_wire_counts(wire: str, raw: dict) -> Counts:
  from bro.llm.wires import counts

  return counts(wire, raw)


@dataclass
class Usage:
  """a surface's cumulative spend: who spent it and how much per model."""

  agent: str  # surface identity, e.g. 'Claude Code 2.1.201' or 'bro//dev'
  per_model: dict[str, Counts]  # keyed by model slug


# --- the env-pointed usage file ----------------------------------------------


class UsageUnavailable(RuntimeError):
  """the usage file holds no usage a reader can credit: its publisher failed,
  stopped, or never wrote it."""


def _replace(path: Path, payload: dict) -> None:
  tmp = path.with_name(path.name + '.tmp')
  tmp.write_text(json.dumps(payload, indent=2) + '\n')
  tmp.replace(path)


def write_usage_file(path: Path, current: Usage, *, lock: Optional[Path] = None) -> None:
  """replace the snapshot at `path` with `current`, atomically. A snapshot naming
  `lock` is current only while its publisher holds that lock
  (`holding_publisher_lock`)."""
  payload: dict = {'agent': current.agent, 'models': current.per_model}
  if lock is not None:
    payload['lock'] = str(lock)
  _replace(path, payload)


def write_usage_pending(path: Path, lock: Path) -> None:
  """replace the file at `path` with a record that its publisher, holding `lock`,
  has billed no usage yet, atomically."""
  _replace(path, {'pending': True, 'lock': str(lock)})


def write_usage_failure(path: Path, reason: str) -> None:
  """replace the snapshot at `path` with the reason its publisher stopped keeping
  it current, atomically."""
  _replace(path, {'failed': reason})


@contextlib.contextmanager
def holding_publisher_lock(path: Path) -> Generator[Path]:
  """hold, for the block, the lock the snapshots a publisher writes at `path`
  name, and yield it: a reader trusts such a snapshot only while it is held, so
  a publisher that stops — failed, or killed with its process — releases what it
  published without writing to it."""
  lock = path.with_name(path.name + '.lock')
  with lock.open('a') as stream:
    fcntl.flock(stream, fcntl.LOCK_EX)
    yield lock


def _held(lock: Path) -> bool:
  try:
    stream = lock.open('rb')
  except FileNotFoundError:
    return False
  with stream:
    try:
      fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
      return True
    return False


def publish(agent: str, per_model: dict[str, Counts]) -> None:
  """write the full cumulative snapshot to this process's own usage file and
  point the environment at it, so the subprocesses it starts afterwards read it.

  The file is the process's own rather than an inherited pointer's: a publisher
  started under another agent's session would otherwise overwrite that agent's
  snapshot. Each publish replaces the whole file — the file is a snapshot of
  one writer's totals, and a process runs one publishing LLM loop in practice.
  """
  path = Path(tempfile.gettempdir()) / f'bro-usage-{os.getpid()}.json'
  write_usage_file(path, Usage(agent=agent, per_model=per_model))
  os.environ[USAGE_FILE_VARIABLE] = str(path)


def read_usage_file(path: Path) -> Optional[Usage]:
  """the usage the file at `path` holds; None while its publisher has billed none."""
  data = json.loads(path.read_text())
  if 'failed' in data:
    raise UsageUnavailable(f'{path}: its usage publisher failed: {data["failed"]}')
  lock = data.get('lock')
  if lock is not None and not _held(Path(lock)):
    raise UsageUnavailable(f'{path}: its usage publisher no longer keeps it current')
  if data.get('pending') is True:
    return None
  per_model = {
    model: {c: int(counts.get(c, 0)) for c in CLASSES} for model, counts in data['models'].items()
  }
  return Usage(agent=data['agent'], per_model=per_model)


def current_usage() -> Optional[Usage]:
  """the cumulative usage the environment's usage file holds; None without a
  pointer, and while its publisher has billed none. Raises `UsageUnavailable`
  for a file no publisher wrote or one it no longer keeps current."""
  pointer = os.environ.get(USAGE_FILE_VARIABLE)
  if pointer is None:
    return None
  path = Path(pointer)
  if not path.exists():
    raise UsageUnavailable(f'{path}: no usage publisher has written it')
  return read_usage_file(path)


def agent_session() -> bool:
  """whether an agent produced this process's work, by the presence of a usage
  file pointer. Deliberately not `current_usage()`, which answers no while the
  publisher has billed no usage."""
  return os.environ.get(USAGE_FILE_VARIABLE) is not None


# --- footer formatting + parsing -----------------------------------------------


def format_int(n: int) -> str:
  return f'{n:,}'.replace(',', _THOUSANDS)


def model_family(slug: str) -> str:
  """Label a model slug through its provider, otherwise strip a snapshot date."""
  from bro.llm import providers

  for _, module in providers.declarations():
    shape = getattr(module, 'MODEL_SLUG', None)
    if shape is not None and shape.match(slug) is not None:
      return module.model_family(slug)
  return re.sub(r'-\d{4}-\d{2}-\d{2}$', '', slug)


def to_labels(slug_counts: dict[str, Counts]) -> dict[str, Counts]:
  """collapse model-slug-keyed counts to the families the footer labels."""
  labels: dict[str, Counts] = {}
  for slug, c in slug_counts.items():
    label = model_family(slug)
    labels[label] = add(labels.get(label, zero()), c)
  return labels


def _format_entry(label: str, c: Counts) -> str:
  return (
    f'{label}: {_UP}({format_int(c.get("input", 0))} {format_int(c.get("cache_write", 0))} '
    f'{format_int(c.get("cache_read", 0))}) {_DOWN}{format_int(c.get("output", 0))}'
  )


def format_footer(agents: list[str], label_counts: dict[str, Counts]) -> str:
  token_parts = ', '.join(_format_entry(m, label_counts[m]) for m in label_counts)
  return f'> created with {", ".join(agents)} | {token_parts}'


_FOOTER_RE = re.compile(
  r'^>\s*created with\s+(?P<agents>.+?)\s*\|\s*(?P<tokens>.+?)\s*$',
  re.MULTILINE,
)
_PART_RE = re.compile(
  r'^(?P<model>.*?):\s*'
  r'↑\s*\(\s*(?P<input>[\d\']+)\s+(?P<cache_write>[\d\']+)\s+(?P<cache_read>[\d\']+)\s*\)\s*'
  r'↓\s*(?P<output>[\d\']+)$'
)
_BARE_VERSION_RE = re.compile(r'^\d+(?:\.\d+)*$')


@dataclass
class Footer:
  agents: list[str]
  delta: dict[str, Counts]  # label-keyed (the footer renders labels, not slugs)


def _unformat_int(s: str) -> int:
  return int(s.replace(_THOUSANDS, ''))


def parse_footer(commit_msg: str) -> Optional[Footer]:
  m = _FOOTER_RE.search(commit_msg)
  if m is None:
    return None
  delta: dict[str, Counts] = {}
  for chunk in m.group('tokens').split(', '):
    pm = _PART_RE.match(chunk.strip())
    if pm is None:
      continue
    label = pm.group('model').strip()
    counts = {
      'input': _unformat_int(pm.group('input')),
      'cache_write': _unformat_int(pm.group('cache_write')),
      'cache_read': _unformat_int(pm.group('cache_read')),
      'output': _unformat_int(pm.group('output')),
    }
    delta[label] = add(delta.get(label, zero()), counts)
  if len(delta) == 0:
    return None
  agents: list[str] = []
  for token in m.group('agents').split(', '):
    token = token.strip()
    # historic footers compressed same-agent versions ("Claude Code 2.1.114,
    # 2.1.120"); a bare version token is such a compressed Claude Code agent.
    if _BARE_VERSION_RE.match(token) is not None:
      token = f'Claude Code {token}'
    agents.append(token)
  return Footer(agents=agents, delta=delta)


def strip_footer(commit_msg: str) -> str:
  """the message without its footer line."""
  return _FOOTER_RE.sub('', commit_msg).rstrip()


# --- CLI -----------------------------------------------------------------------


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(
    description="print the session's cumulative LLM usage as per-model counts "
    'in the four billed token classes'
  )
  parser.parse(argv)
  current = current_usage()
  if current is None:
    print(
      f'error: no usage published (no {USAGE_FILE_VARIABLE} pointer, or none billed yet)',
      file=sys.stderr,
    )
    return 1
  print(f'agent: {current.agent}')
  for label, counts in to_labels(current.per_model).items():
    print(_format_entry(label, counts))
  return 0
