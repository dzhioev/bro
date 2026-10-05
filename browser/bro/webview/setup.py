"""Host-side interactive capture and storage of webview browser profiles."""

from __future__ import annotations

import json
import os
import sys
from typing import Any, BinaryIO, NoReturn, cast

from bro.base import credentials
from bro.webview.capture_protocol import CAPTURE_OPTIONS_ENV, EXCHANGE_MAX_BYTES
from bro.webview.profile import (
  ProfileError,
  decode_profile,
  encode_profile,
  has_indexed_db,
  has_storage,
  normalized_storage_state,
  validate_storage_state,
)
from bro.webview.worker import VNC_PORT, WEBVIEW, _dockerfile, vnc_url
from bro.worker_types import WorkerContainer


class SetupError(Exception):
  """The host cannot safely produce or store the requested profile."""


def _reject_json_constant(_constant: str) -> NoReturn:
  raise ValueError('not strict JSON')


def _write_line(stream: BinaryIO, value: dict[str, Any]) -> None:
  encoded = json.dumps(
    value,
    ensure_ascii=True,
    separators=(',', ':'),
    allow_nan=False,
  ).encode()
  if len(encoded) + 1 > EXCHANGE_MAX_BYTES:
    raise SetupError(f'capture exchange line exceeds the {EXCHANGE_MAX_BYTES}-byte limit')
  stream.write(encoded + b'\n')
  stream.flush()


def _read_line(stream: BinaryIO, subject: str) -> dict[str, Any]:
  line = stream.readline(EXCHANGE_MAX_BYTES + 1)
  if line == b'':
    raise SetupError(f'capture ended before {subject}')
  if len(line) > EXCHANGE_MAX_BYTES or not line.endswith(b'\n'):
    raise SetupError(f'capture {subject} exceeds the {EXCHANGE_MAX_BYTES}-byte line limit')
  try:
    value = json.loads(line, parse_constant=_reject_json_constant)
  except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
    raise SetupError(f'capture {subject} is not valid JSON') from error
  if not isinstance(value, dict):
    raise SetupError(f'capture {subject} must be an object')
  return value


def _message_shape(message: dict[str, Any]) -> str:
  event = message.get('event')
  event_name = event if event in {'ready', 'state', 'error'} else 'unknown'
  return f'event {event_name!r} with {len(message)} fields'


def _capture_spec(url: str | None, indexed_db: bool, port: int | None) -> WorkerContainer:
  options = json.dumps(
    {'url': url, 'indexed_db': indexed_db},
    ensure_ascii=True,
    separators=(',', ':'),
  )
  return WorkerContainer(
    files={'Dockerfile': _dockerfile()},
    command=('webview', 'capture'),
    env={CAPTURE_OPTIONS_ENV: options},
    published_ports={VNC_PORT: port},
  )


def _stored_profile(
  raw: str | None, *, fresh: bool
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
  if raw is None:
    return None, None
  try:
    state = decode_profile(raw)
  except ProfileError:
    if fresh:
      return None, None
    raise
  return (None if fresh else state), state


def _capture_profile(
  *,
  seed: dict[str, Any] | None,
  url: str | None,
  indexed_db: bool,
  port: int | None,
) -> dict[str, Any]:
  from ride.worker_container import foreground_worker_run

  spec = _capture_spec(url, indexed_db, port)
  with foreground_worker_run(WEBVIEW, spec) as run:
    process = run.process
    if process.stdin is None or process.stdout is None:
      raise RuntimeError('foreground webview capture has no exchange pipes')
    process_stdin = cast(BinaryIO, process.stdin)
    process_stdout = cast(BinaryIO, process.stdout)
    _write_line(process_stdin, {'seed': seed})
    ready = _read_line(process_stdout, 'readiness')
    if ready != {'event': 'ready'}:
      raise SetupError(f'capture returned malformed readiness: {_message_shape(ready)}')
    ((host_port, container_port),) = run.published_ports
    if container_port != VNC_PORT:
      raise RuntimeError(f'foreground webview published unexpected container port {container_port}')
    print(f'Open {vnc_url(host_port)}')
    input('Log in, keep the browser window open, then press Enter to capture: ')
    _write_line(process_stdin, {'request': 'state'})
    answer = _read_line(process_stdout, 'state answer')
    if answer.get('event') == 'error' and set(answer) == {'event', 'error'}:
      reason = answer['error']
      if not isinstance(reason, str) or reason == '':
        raise SetupError('capture returned an invalid error answer')
      raise SetupError(f'capture failed: {reason}')
    if set(answer) != {'event', 'state'} or answer.get('event') != 'state':
      raise SetupError(f'capture returned malformed state answer: {_message_shape(answer)}')
    try:
      state = validate_storage_state(answer['state'])
    except ProfileError as error:
      raise SetupError(f'capture returned an invalid profile: {error}') from error
    code = process.wait()
    if code != 0:
      raise SetupError(f'capture exited with status {code}')
    return state


def _summary(storage_state: dict[str, Any], size: int) -> str:
  cookies = storage_state['cookies']
  sites = {
    cookie.get('domain')
    for cookie in cookies
    if isinstance(cookie, dict) and isinstance(cookie.get('domain'), str)
  }
  origins = [
    origin
    for origin in storage_state['origins']
    if origin['localStorage'] or origin.get('indexedDB')
  ]
  indexed_db_origins = sum(bool(origin.get('indexedDB')) for origin in storage_state['origins'])
  return (
    f'captured {len(cookies)} cookies across {len(sites)} sites, '
    f'{len(origins)} origins with storage, {indexed_db_origins} IndexedDB origins, {size} bytes'
  )


def setup_profile(
  instance: str,
  *,
  url: str | None = None,
  fresh: bool = False,
  indexed_db: bool = False,
  port: int | None = None,
) -> str:
  if 'RIDE_ISOLATION' in os.environ:
    raise SetupError('webview setup must run on the host, outside a managed session')
  if not sys.stdin.isatty():
    raise SetupError('webview setup requires a terminal on stdin')
  if instance == '':
    raise SetupError('webview setup instance must not be empty')
  name = f'cookies+{instance}'
  store = credentials.Store(
    credentials.default_registry(),
    credentials.STORE_DIR,
    {'cookies': instance},
  )
  if store.source_type(name) != credentials.LocalSource.TYPE:
    raise SetupError(f'profile {name!r} has a non-local credential source')

  try:
    lock = store.stored_name_lock(name, blocking=False)
    with lock:
      expected = store.read_stored_material(name)
      raw = None
      if expected is not None and not fresh:
        try:
          raw = store.get('cookies')
        except (credentials.SecretNotFound, ValueError) as error:
          raise SetupError(f'stored profile {name!r} is invalid: {error}') from error
      elif expected is not None:
        try:
          raw = expected.decode()
        except UnicodeDecodeError:
          raw = None
      try:
        seed, comparable = _stored_profile(raw, fresh=fresh)
      except ProfileError as error:
        raise SetupError(f'stored profile {name!r} is invalid: {error}') from error
      capture_indexed_db = indexed_db or (seed is not None and has_indexed_db(seed))
      state = _capture_profile(
        seed=seed,
        url=url,
        indexed_db=capture_indexed_db,
        port=port,
      )
      if not has_storage(state):
        raise SetupError('captured profile is empty; log in before capturing')
      encoded = encode_profile(state)
      if comparable is not None and normalized_storage_state(state) == normalized_storage_state(
        comparable
      ):
        return 'profile unchanged; wrote nothing'
      store.write_stored_material(name, encoded, expected=expected)
      return _summary(state, len(encoded))
  except credentials.StoredNameLocked as error:
    raise SetupError(str(error)) from error
  except credentials.StoredMaterialChanged as error:
    raise SetupError(str(error)) from error
