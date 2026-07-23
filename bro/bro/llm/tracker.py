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
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Optional, TextIO
from urllib.parse import urlparse

from base import configs
from base.lulid import lulid

# delays before each retry attempt for transient blips on per-step POSTs and
# end-trail POSTs (an empty tuple means "fail-fast, no retries", used by
# start_trail). format: schedule[i] is the sleep before retry-attempt i; the
# initial attempt has no preceding sleep.
_STEP_RETRY_DELAYS_SECONDS = (0.1, 0.5, 2.0)

# cadence of HTTPTracker's liveness heartbeat: while a trail is open, a
# background thread wakes on this interval and POSTs a keepalive whenever no
# other write landed within it. sized so the wire never idles longer than two
# intervals — well under the shared ALB's 300s idle timeout
# (infra/cdk/shared_stack.py), so the persistent connection stays warm through
# long blocking tools.
KEEPALIVE_INTERVAL_SECONDS = 60.0

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

EndReason = Literal['ok', 'raised', 'error']


@dataclass(frozen=True)
class ForkedFrom:
  """pointer to a source trail's fork point."""

  trail_id: str
  step_id: str


@dataclass(frozen=True)
class Trail:
  """trail header — the metadata written once when the trail opens.

  `version` is sourced from `configs.VERSION` inside `start_trail`.
  `llm_spec` is the dict returned by `LLMSpec.dump()`. `system_prompt` is
  emitted as the first step rather than carried on the header.
  """

  id: str
  harness: str
  bro: Optional[str]
  version: str
  native: dict
  started_at: str
  interactive: bool
  surface: str
  forked_from: Optional[ForkedFrom]
  summoned_by: Optional[dict[str, Any]] = None

  @property
  def llm_spec(self) -> dict:
    return self.native['llm']


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
    forked_from: Optional[ForkedFrom],
    interactive: bool,
    surface: str,
    hold: str = 'unattended',
    summoned_by: Optional[dict[str, Any]] = None,
  ) -> str:
    """open a new trail. returns the assigned `trail_id`.

    the prompt is recorded as the first step (kind=`system_prompt`, turn 0);
    everything else goes on the trail header. `summoned_by` is optional provenance
    for a summoned run and is unrelated to the fork-lineage `forked_from` pointer.
    """
    ...

  @abstractmethod
  def step(self, kind: StepKind, body: Any, **extras: Any) -> None:
    """append one step to the current trail. extras are passed through to the
    sink as additional fields on the step record.
    """
    ...

  @abstractmethod
  def end_trail(self, reason: EndReason, detail: Optional[str] = None) -> None:
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
    forked_from: Optional[ForkedFrom],
    interactive: bool,
    surface: str,
    hold: str = 'unattended',
    summoned_by: Optional[dict[str, Any]] = None,
  ) -> str:
    return ''

  def step(self, kind: StepKind, body: Any, **extras: Any) -> None:
    pass

  def end_trail(self, reason: EndReason, detail: Optional[str] = None) -> None:
    pass


def _now_iso() -> str:
  return datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _new_step_id() -> str:
  # HTTPTracker mints the step_id client-side (a lulid, matching the server's
  # own id format) and reuses it across retries of the same POST: the server
  # Puts it with attribute_not_exists, so a retried write is an idempotent
  # no-op rather than a duplicate row + double-counted aggregate. a lulid (not
  # a plain uuid) because the steps-table sort key relies on the ordering.
  return lulid()


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
    self._trail_id: Optional[str] = None

  def start_trail(
    self,
    bro: str,
    llm_spec: dict,
    system_prompt: str,
    forked_from: Optional[ForkedFrom],
    interactive: bool,
    surface: str,
    hold: str = 'unattended',
    summoned_by: Optional[dict[str, Any]] = None,
  ) -> str:
    self._trail_id = lulid()
    header = {
      'record_type': 'trail',
      'id': self._trail_id,
      'harness': 'bro',
      'bro': bro,
      'version': configs.VERSION,
      'native': {'llm': llm_spec},
      'started_at': _now_iso(),
      'interactive': interactive,
      'surface': surface,
      'hold': hold,
      'forked_from': asdict(forked_from) if forked_from is not None else None,
    }
    if summoned_by is not None:
      header['summoned_by'] = summoned_by
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
      'step_id': lulid(),
      'ts': _now_iso(),
      'kind': kind,
      'body': body,
      **extras,
    }
    self._write(record)

  def end_trail(self, reason: EndReason, detail: Optional[str] = None) -> None:
    if self._trail_id is None:
      return
    # a final `end` step is the JSONL equivalent of the server updating
    # `ended_at` / `end_reason` on the trail row.
    self.step('end', {'reason': reason, **({'detail': detail} if detail is not None else {})})
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


def is_retryable_status(status: int) -> bool:
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
    already finished, and the server's sweep stamps headers missing `ended_at`
    as lost.
  - keepalive (`POST /v1/trails/{id}/keepalive`): best-effort, from a
    background thread during quiet stretches (`KEEPALIVE_INTERVAL_SECONDS`) —
    the server's liveness signal for the lost-trail sweep. failures are logged
    and swallowed; liveness must never take down a healthy run.

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
    self._connection: Optional[http.client.HTTPSConnection] = None
    self._trail_id: Optional[str] = None
    # serializes wire access between the caller and the keepalive thread
    # (http.client connections are not thread-safe); reentrant because _post
    # drops the connection while already holding it.
    self._lock = threading.RLock()
    self._last_write_monotonic = time.monotonic()
    self._keepalive_stop: Optional[threading.Event] = None
    self._keepalive_thread: Optional[threading.Thread] = None

  def start_trail(
    self,
    bro: str,
    llm_spec: dict,
    system_prompt: str,
    forked_from: Optional[ForkedFrom],
    interactive: bool,
    surface: str,
    hold: str = 'unattended',
    summoned_by: Optional[dict[str, Any]] = None,
  ) -> str:
    payload = {
      'harness': 'bro',
      'bro': bro,
      'version': configs.VERSION,
      'native': {'llm': llm_spec},
      'body': {'system_prompt': system_prompt},
      'forked_from': asdict(forked_from) if forked_from is not None else None,
      'interactive': interactive,
      'surface': surface,
      'hold': hold,
    }
    if summoned_by is not None:
      payload['summoned_by'] = summoned_by
    response = self._post('/v1/trails', payload, retry_delays=())
    trail_id: str = response['id']
    self._trail_id = trail_id
    self._start_keepalive()
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

  def end_trail(self, reason: EndReason, detail: Optional[str] = None) -> None:
    if self._trail_id is None:
      return
    self._stop_keepalive()
    payload = {'step_id': _new_step_id(), 'reason': reason}
    if detail is not None:
      payload['detail'] = detail
    try:
      self._post(
        f'/v1/trails/{self._trail_id}/end',
        payload,
        retry_delays=_STEP_RETRY_DELAYS_SECONDS,
      )
    except Exception as exception:
      # work is already done; the server's sweep stamps trails missing
      # ended_at as lost, so we log loudly and let the run return normally
      # rather than masking the outcome with a tracker failure.
      logging.warning('trails end_trail failed for trail %s: %s', self._trail_id, exception)
    finally:
      with self._lock:
        self._trail_id = None
        self._drop_connection()

  def close(self) -> None:
    self._stop_keepalive()
    with self._lock:
      self._drop_connection()

  def _start_keepalive(self) -> None:
    self._keepalive_stop = threading.Event()
    self._keepalive_thread = threading.Thread(
      target=self._keepalive_loop,
      args=(self._keepalive_stop,),
      name=f'trails-keepalive-{self._trail_id}',
      daemon=True,
    )
    self._keepalive_thread.start()

  def _stop_keepalive(self) -> None:
    if self._keepalive_stop is not None:
      self._keepalive_stop.set()
      self._keepalive_stop = None

  def _keepalive_loop(self, stop: threading.Event) -> None:
    while not stop.wait(KEEPALIVE_INTERVAL_SECONDS):
      with self._lock:
        trail_id = self._trail_id
        idle_seconds = time.monotonic() - self._last_write_monotonic
      if trail_id is None:
        return
      if idle_seconds < KEEPALIVE_INTERVAL_SECONDS:
        continue
      try:
        self._post(f'/v1/trails/{trail_id}/keepalive', {}, retry_delays=(0.5,))
      except Exception as exception:
        logging.warning('trails keepalive failed for trail %s: %s', trail_id, exception)

  def _post(self, path: str, payload: dict, *, retry_delays: tuple[float, ...]) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    headers = {
      'Authorization': f'Bearer {self._token}',
      'Content-Type': 'application/json',
    }
    last_exception: Optional[Exception] = None
    # initial attempt has no preceding sleep; each retry sleeps its delay
    # before attempting. the loop falls through after the last failure and
    # raises the captured exception.
    schedule: tuple[float, ...] = (0.0,) + retry_delays
    with self._lock:
      for delay in schedule:
        if delay > 0:
          time.sleep(delay)
        try:
          response = self._request('POST', path, headers, body)
          self._last_write_monotonic = time.monotonic()
          return response
        except HTTPStatusError as exception:
          # drop the persistent connection so the next attempt opens a fresh one.
          self._drop_connection()
          # deterministic 4xx won't change on a retry — propagate immediately
          # rather than sleeping through the rest of the schedule.
          if not is_retryable_status(exception.status):
            raise
          last_exception = exception
        except Exception as exception:
          last_exception = exception
          # transient blips often leave the socket half-open; reopen next attempt.
          self._drop_connection()
      assert last_exception is not None
      raise last_exception

  def _request(self, method: str, path: str, headers: dict, body: bytes) -> dict:
    connection = self._get_connection()
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    raw = response.read()
    if response.status >= 400:
      raise HTTPStatusError(
        response.status,
        f'{method} {path} -> HTTP {response.status}: {raw.decode(errors="replace")}',
      )
    if response.status == 204 or len(raw) == 0:
      return {}
    return json.loads(raw)

  def _get_connection(self) -> http.client.HTTPSConnection:
    if self._connection is not None:
      return self._connection
    context = ssl.create_default_context()
    self._connection = http.client.HTTPSConnection(
      self._host, self._port, timeout=self._timeout, context=context
    )
    return self._connection

  def _drop_connection(self) -> None:
    if self._connection is None:
      return
    try:
      self._connection.close()
    except Exception:
      pass
    self._connection = None


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
    trail_id = record['id'] if record_type == 'trail' else record['trail_id']
    if record_type == 'trail':
      forked_from_data = record.get('forked_from')
      forked_from = ForkedFrom(**forked_from_data) if forked_from_data is not None else None
      headers[trail_id] = Trail(
        id=trail_id,
        harness=record['harness'],
        bro=record.get('bro'),
        version=record['version'],
        native=record['native'],
        started_at=record['started_at'],
        interactive=record['interactive'],
        surface=record['surface'],
        forked_from=forked_from,
        summoned_by=record.get('summoned_by'),
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
  return [RecordedTrail(header=headers[trail_id], steps=steps[trail_id]) for trail_id in order]
