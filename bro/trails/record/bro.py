"""Bro harness recorder over the shared trails write spine."""

import logging
import threading
from dataclasses import asdict
from typing import Any, Optional

from bro.base import configs
from bro.llm.tracker import EndReason, StepKind, Tracker
from bro.trails.model import ForkedFrom, tools_sha256
from bro.trails.record import spine
from bro.trails.record.spine import Recording
from bro.trails.store import TrailsStore


class Recorder(Tracker):
  """adapt bro tracker events into universal ordinal records."""

  def __init__(self, store: TrailsStore):
    self._store = store
    self._recording: Optional[Recording] = None
    self._lock = threading.RLock()
    self._keepalive_stop: Optional[threading.Event] = None
    self._keepalive_thread: Optional[threading.Thread] = None

  def start_trail(
    self,
    bro: str,
    llm_spec: dict,
    system_prompt: str,
    forked_from: Optional[Any],
    interactive: bool,
    surface: str,
    hold: str = 'unattended',
    summoned_by: Optional[dict[str, Any]] = None,
  ) -> str:
    if forked_from is not None and not isinstance(forked_from, ForkedFrom):
      raise TypeError('forked_from must be a ForkedFrom')
    payload = {
      'harness': 'bro',
      'bro': bro,
      'version': configs.VERSION,
      'native': {'llm': llm_spec},
      'body': {'records': [{'kind': 'system_prompt', 'body': system_prompt, 'turn_index': 0}]},
      'forked_from': (
        {key: value for key, value in asdict(forked_from).items() if value is not None}
        if forked_from is not None
        else None
      ),
      'interactive': interactive,
      'surface': surface,
      'hold': hold,
    }
    if summoned_by is not None:
      payload['summoned_by'] = summoned_by
    recording = Recording.create(self._store, payload)
    with self._lock:
      self._recording = recording
    self._start_keepalive()
    return recording.trail_id

  def step(self, kind: StepKind, body: Any, **extras: Any) -> Optional[int]:
    record = {'kind': kind, 'body': body, **extras}
    tool_blobs: Optional[dict[str, Any]] = None
    if kind == 'llm_call':
      if not isinstance(body, dict):
        raise ValueError('llm_call body must be an object')
      request = body.get('request')
      if not isinstance(request, dict) or not isinstance(request.get('tools'), list):
        raise ValueError('llm_call body.request.tools must be a list')
      tools = request['tools']
      sha256 = tools_sha256(tools)
      request_without_tools = {key: value for key, value in request.items() if key != 'tools'}
      record['body'] = {**body, 'request': request_without_tools}
      record['tools_sha256'] = sha256
      tool_blobs = {sha256: tools}

    with self._lock:
      recording = self._recording
      if recording is None:
        raise RuntimeError('step() called before start_trail()')
      return recording.append([record], tools=tool_blobs)

  def end_trail(self, reason: EndReason, detail: Optional[str] = None) -> None:
    with self._lock:
      recording = self._recording
    if recording is None:
      return
    self._stop_keepalive()
    try:
      recording.end(reason, detail, retry_delays=spine.WRITE_RETRY_DELAYS_SECONDS)
    except Exception as exception:
      logging.warning('trails end_trail failed for trail %s: %s', recording.trail_id, exception)
    with self._lock:
      self._recording = None
    self._store.close()

  def close(self) -> None:
    self._stop_keepalive()
    self._store.close()

  def _start_keepalive(self) -> None:
    stop = threading.Event()
    self._keepalive_stop = stop
    self._keepalive_thread = threading.Thread(
      target=self._keepalive_loop,
      args=(stop,),
      name=f'trails-keepalive-{self._recording.trail_id if self._recording is not None else ""}',
      daemon=True,
    )
    self._keepalive_thread.start()

  def _stop_keepalive(self) -> None:
    if self._keepalive_stop is not None:
      self._keepalive_stop.set()
      self._keepalive_stop = None

  def _keepalive_loop(self, stop: threading.Event) -> None:
    while not stop.wait(spine.KEEPALIVE_INTERVAL_SECONDS):
      with self._lock:
        recording = self._recording
      if recording is None:
        return
      try:
        recording.keepalive_if_idle(retry_delays=(0.5,))
      except Exception as exception:
        logging.warning('trails keepalive failed for trail %s: %s', recording.trail_id, exception)
