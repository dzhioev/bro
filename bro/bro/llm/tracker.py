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
- `HTTPTracker` — sync per-step HTTPS POSTs to the deployed `trails-server`.
  the production sink, configured via the `trails` secret.
"""

import http.client
import json
import logging
import ssl
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TextIO
from urllib.parse import urlparse

from ulid import ULID

import configs

# delays before each retry attempt for transient blips on per-step POSTs and
# end-trail POSTs (an empty tuple means "fail-fast, no retries", used by
# start_trail). format: schedule[i] is the sleep before retry-attempt i; the
# initial attempt has no preceding sleep.
_STEP_RETRY_DELAYS_SECONDS = (0.1, 0.5, 2.0)

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


def _new_step_id() -> str:
  # HTTPTracker mints the step_id client-side (ULID, matching the server's own
  # id format) and reuses it across retries of the same POST: the server Puts it
  # with attribute_not_exists, so a retried write is an idempotent no-op rather
  # than a duplicate row + double-counted aggregate. ULID (not _new_id's uuid)
  # because the steps-table sort key relies on the ordering.
  return str(ULID())


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


class HTTPStatusError(Exception):
  """a non-2xx response from trails-server, carrying the numeric status so the
  retry loop can branch on it (a generic HTTPException buries it in the message).
  """

  def __init__(self, status: int, message: str):
    super().__init__(message)
    self.status = status


def _is_retryable_status(status: int) -> bool:
  # transient: 5xx (server-side) and 429 (rate limit) are worth retrying.
  # deterministic 4xx (400 malformed, 404 missing trail, 413 too large) won't
  # change on a retry — fail fast instead of sleeping through the schedule.
  return status >= 500 or status == 429


class HTTPTracker(Tracker):
  """ship trail events to the deployed `trails-server` over HTTPS.

  one persistent connection per tracker; each call is a synchronous POST and
  every step either commits or propagates the error out of the bro. sync writes
  are what makes crash-on-failure meaningful — async fire-and-forget would lose
  steps when one-shot bros exit before a flush.

  policy by endpoint:
  - `start_trail` (`POST /v1/trails`): fail-fast. one attempt; raising aborts
    the run before any work happens, because a bro that can't record shouldn't
    proceed.
  - `step` (`POST /v1/trails/{id}/steps`): 100ms / 500ms / 2s in-process
    retries on transient blips, then propagate.
  - `end_trail` (`POST /v1/trails/{id}/end`): same retry schedule, but a
    persistent failure is logged loudly and not re-raised — the trail is
    already finished, and the server can reap headers missing `ended_at`.

  the server auto-emits the `system_prompt` step inside `POST /v1/trails`, so
  `HTTPTracker.start_trail` does not mirror `LocalFileTracker`'s extra
  `step('system_prompt', ...)` call.
  """

  def __init__(self, base_url: str, token: str, *, timeout: float = 5.0):
    self._base_url = base_url.rstrip('/')
    self._token = token
    self._timeout = timeout
    parsed = urlparse(self._base_url)
    if parsed.scheme != 'https':
      raise ValueError(f'HTTPTracker requires an https URL, got {base_url!r}')
    hostname = parsed.hostname
    self._host: str = hostname if hostname is not None else 'localhost'
    self._port = parsed.port
    self._conn: http.client.HTTPSConnection | None = None
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
    payload = {
      'bro': bro,
      'bro_version': configs.VERSION,
      'llm_spec': llm_spec,
      'system_prompt': system_prompt,
      'parent': asdict(parent) if parent is not None else None,
      'interactive': interactive,
      'entry_point': entry_point,
    }
    response = self._post('/v1/trails', payload, retry_delays=())
    trail_id: str = response['trail_id']
    self._trail_id = trail_id
    return trail_id

  def step(self, kind: StepKind, body: Any, **extras: Any) -> None:
    if self._trail_id is None:
      raise RuntimeError('step() called before start_trail()')
    # mint the step_id here, outside _post, so every retry of this POST carries
    # the same id — that is what makes the server-side write idempotent.
    payload = {'step_id': _new_step_id(), 'kind': kind, 'body': body, **extras}
    self._post(
      f'/v1/trails/{self._trail_id}/steps',
      payload,
      retry_delays=_STEP_RETRY_DELAYS_SECONDS,
    )

  def end_trail(self, reason: EndReason) -> None:
    if self._trail_id is None:
      return
    payload = {'step_id': _new_step_id(), 'reason': reason}
    try:
      self._post(
        f'/v1/trails/{self._trail_id}/end',
        payload,
        retry_delays=_STEP_RETRY_DELAYS_SECONDS,
      )
    except Exception as exc:
      # work is already done; the server reaps trails missing ended_at, so we
      # log loudly and let the run return normally rather than masking the
      # outcome with a tracker failure.
      logging.warning('trails end_trail failed for trail %s: %s', self._trail_id, exc)
    finally:
      self._trail_id = None
      self._drop_conn()

  def close(self) -> None:
    self._drop_conn()

  def _post(self, path: str, payload: dict, *, retry_delays: tuple[float, ...]) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    headers = {
      'Authorization': f'Bearer {self._token}',
      'Content-Type': 'application/json',
    }
    last_exc: Exception | None = None
    # initial attempt has no preceding sleep; each retry sleeps its delay
    # before attempting. the loop falls through after the last failure and
    # raises the captured exception.
    schedule: tuple[float, ...] = (0.0,) + retry_delays
    for delay in schedule:
      if delay > 0:
        time.sleep(delay)
      try:
        return self._request('POST', path, headers, body)
      except HTTPStatusError as exc:
        # drop the persistent connection so the next attempt opens a fresh one.
        self._drop_conn()
        # deterministic 4xx won't change on a retry — propagate immediately
        # rather than sleeping through the rest of the schedule.
        if not _is_retryable_status(exc.status):
          raise
        last_exc = exc
      except Exception as exc:
        last_exc = exc
        # transient blips often leave the socket half-open; reopen next attempt.
        self._drop_conn()
    assert last_exc is not None
    raise last_exc

  def _request(self, method: str, path: str, headers: dict, body: bytes) -> dict:
    conn = self._get_conn()
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    if resp.status >= 400:
      raise HTTPStatusError(
        resp.status, f'{method} {path} -> HTTP {resp.status}: {raw.decode(errors="replace")}'
      )
    if resp.status == 204 or len(raw) == 0:
      return {}
    return json.loads(raw)

  def _get_conn(self) -> http.client.HTTPSConnection:
    if self._conn is not None:
      return self._conn
    ctx = ssl.create_default_context()
    self._conn = http.client.HTTPSConnection(
      self._host, self._port, timeout=self._timeout, context=ctx
    )
    return self._conn

  def _drop_conn(self) -> None:
    if self._conn is None:
      return
    try:
      self._conn.close()
    except Exception:
      pass
    self._conn = None


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
