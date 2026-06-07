"""trail recording for bro / LLM runs.

a *trail* is one complete recorded run of a bro; a *step* is a single event
within a trail (system prompt, user input, reasoning summary, assistant text,
tool call / result, raw llm_call payload, error, end). `Tracker` is the
sibling of `llm.observer.Observer` — `Observer` renders events to stderr,
`Tracker` ships them to a durable sink.

implementations:
- `NullTracker` — no-op, the default when no sink is configured.
- `LocalFileTracker` — appends JSONL to a local file; one header line + N
  step lines per trail. dev helper for offline inspection.
"""

import json
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TextIO

import configs

StepKind = Literal[
  'system_prompt',
  'user_input',
  'reasoning',
  'assistant',
  'tool_call',
  'tool_result',
  'llm_call',
  'error',
  'end',
]

Relationship = Literal['fork', 'subagent']

EndReason = Literal['terminal', 'raised', 'error']


@dataclass(frozen=True)
class Parent:
  """pointer to a parent trail's fork point.

  `relationship='subagent'` is reserved for when sub-bros come back; v1 never
  emits it.
  """

  trail_id: str
  step_id: str
  relationship: Relationship


@dataclass(frozen=True)
class Trail:
  """trail header — the metadata written once when the trail opens.

  `bro_version` is sourced from `configs.VERSION` inside `start_trail`.
  `llm_spec` is the dict returned by `LLMSpec.dump()`. `system_prompt` is
  emitted as the first step rather than carried on the header.
  """

  trail_id: str
  bro: str
  bro_version: int
  llm_spec: dict
  started_at: str
  interactive: bool
  entry_point: str
  parent: Parent | None


@dataclass(frozen=True)
class Step:
  """one recorded event in a trail. `body` shape depends on `kind`; extras
  carry the per-kind metadata documented in the design doc (e.g. `turn_index`,
  `tool_name`, `arguments`, `call_id`, `tokens_in`).
  """

  trail_id: str
  step_id: str
  ts: str
  kind: StepKind
  body: Any
  extras: dict[str, Any]


@dataclass(frozen=True)
class RecordedTrail:
  """a trail rehydrated from a sink — header plus the ordered steps. consumed
  by `bro.fork` to replay or fork a recorded run. richer query / reader APIs
  land in the stage 6 reader library; this is the minimum the forker needs.
  """

  header: Trail
  steps: list[Step]


class Tracker(ABC):
  """capture a trail of bro / LLM run events and ship it to a sink.

  sibling of `llm.observer.Observer`. `Observer` renders the live stream for a
  human watching stderr; `Tracker` records the same stream durably for later
  analysis, A/B comparison, and forking.
  """

  @abstractmethod
  def start_trail(
    self,
    bro: str,
    llm_spec: dict,
    system_prompt: str,
    parent: Parent | None,
    interactive: bool,
    entry_point: str,
  ) -> str:
    """open a new trail. returns the assigned `trail_id`.

    the prompt is recorded as the first step (kind=`system_prompt`, turn 0);
    everything else goes on the trail header.
    """
    ...

  @abstractmethod
  def step(self, kind: StepKind, body: Any, **extras: Any) -> None:
    """append one step to the current trail. extras are passed through to the
    sink as additional fields on the step record.
    """
    ...

  @abstractmethod
  def end_trail(self, reason: EndReason) -> None:
    """close the current trail. emits a final `kind='end'` step carrying the
    reason. safe to call more than once — extra calls are ignored.
    """
    ...


class NullTracker(Tracker):
  """no-op tracker — the default when no sink is configured and the standard
  choice in tests.
  """

  def start_trail(
    self,
    bro: str,
    llm_spec: dict,
    system_prompt: str,
    parent: Parent | None,
    interactive: bool,
    entry_point: str,
  ) -> str:
    return ''

  def step(self, kind: StepKind, body: Any, **extras: Any) -> None:
    pass

  def end_trail(self, reason: EndReason) -> None:
    pass


def _now_iso() -> str:
  return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _new_id() -> str:
  # LocalFileTracker readers walk in file order, so step_id only needs to be
  # unique — not sortable. hex uuid satisfies that without a new dep.
  return uuid.uuid4().hex


class LocalFileTracker(Tracker):
  """append trails to a JSONL file (one JSON object per line).

  layout per trail: one trail header line (`record_type='trail'`) followed by
  N step lines (`record_type='step'`). multiple trails in the same file are
  demultiplexed by `trail_id`.

  the file is opened in line-buffered append mode so each line is durable
  immediately — dev usage often `tail -f`s the file during a live run.
  """

  def __init__(self, path: Path | str):
    self._path = Path(path)
    self._file: TextIO = open(self._path, 'a', buffering=1)
    self._trail_id: str | None = None

  def start_trail(
    self,
    bro: str,
    llm_spec: dict,
    system_prompt: str,
    parent: Parent | None,
    interactive: bool,
    entry_point: str,
  ) -> str:
    self._trail_id = _new_id()
    header = {
      'record_type': 'trail',
      'trail_id': self._trail_id,
      'bro': bro,
      'bro_version': configs.VERSION,
      'llm_spec': llm_spec,
      'started_at': _now_iso(),
      'interactive': interactive,
      'entry_point': entry_point,
      'parent': asdict(parent) if parent is not None else None,
    }
    self._write(header)
    # the system prompt is the trail's first step rather than a header field —
    # keeps the header lean and lets a fork swap the prompt without rewriting
    # the header.
    self.step('system_prompt', system_prompt, turn_index=0)
    return self._trail_id

  def step(self, kind: StepKind, body: Any, **extras: Any) -> None:
    if self._trail_id is None:
      raise RuntimeError('step() called before start_trail()')
    record = {
      'record_type': 'step',
      'trail_id': self._trail_id,
      'step_id': _new_id(),
      'ts': _now_iso(),
      'kind': kind,
      'body': body,
      **extras,
    }
    self._write(record)

  def end_trail(self, reason: EndReason) -> None:
    if self._trail_id is None:
      return
    # a final `end` step is the JSONL equivalent of the server updating
    # `ended_at` / `end_reason` on the trail row.
    self.step('end', {'reason': reason})
    self._trail_id = None

  def close(self) -> None:
    if not self._file.closed:
      self._file.close()

  def _write(self, record: dict) -> None:
    self._file.write(json.dumps(record, ensure_ascii=False) + '\n')


# canonical step fields written by LocalFileTracker (everything else on a step
# line is an extras field — `turn_index`, `tool_name`, `arguments`, `call_id`,
# `tokens_in`, ...). kept module-private so the writer stays the only place
# that mints record shapes.
_STEP_CANONICAL_FIELDS = frozenset({'record_type', 'trail_id', 'step_id', 'ts', 'kind', 'body'})


def read_local_file(path: Path | str) -> list[RecordedTrail]:
  """load every trail from a JSONL file produced by `LocalFileTracker`.

  multiple trails in the same file are demultiplexed by `trail_id`; steps come
  back in file order (the writer is append-only and writes header + N steps per
  trail sequentially). returned trails are listed in the order their headers
  appear in the file.
  """
  path = Path(path)
  headers: dict[str, Trail] = {}
  steps: dict[str, list[Step]] = {}
  order: list[str] = []
  for line in path.read_text().splitlines():
    if len(line) == 0:
      continue
    record = json.loads(line)
    record_type = record['record_type']
    trail_id = record['trail_id']
    if record_type == 'trail':
      parent_data = record.get('parent')
      parent = Parent(**parent_data) if parent_data is not None else None
      headers[trail_id] = Trail(
        trail_id=trail_id,
        bro=record['bro'],
        bro_version=record['bro_version'],
        llm_spec=record['llm_spec'],
        started_at=record['started_at'],
        interactive=record['interactive'],
        entry_point=record['entry_point'],
        parent=parent,
      )
      steps[trail_id] = []
      order.append(trail_id)
    elif record_type == 'step':
      extras = {k: v for k, v in record.items() if k not in _STEP_CANONICAL_FIELDS}
      steps[trail_id].append(
        Step(
          trail_id=trail_id,
          step_id=record['step_id'],
          ts=record['ts'],
          kind=record['kind'],
          body=record['body'],
          extras=extras,
        )
      )
  return [RecordedTrail(header=headers[tid], steps=steps[tid]) for tid in order]
