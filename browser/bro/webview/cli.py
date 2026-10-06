"""The webview owner command and container daemon entry point."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
from collections.abc import Callable, Generator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from bro import mission
from bro.base import log
from bro.base.args import Parser
from bro.webview.worker import WEBVIEW

if TYPE_CHECKING:
  from bro.broker.brotocol import Message
  from bro.broker.client import Client

__cli_name__ = 'webview'

OPEN_TIMEOUT = 1200.0


class WebviewError(Exception):
  """An owner operation that produced no usable webview result."""


@dataclass(frozen=True)
class OpenedWebview:
  mission_id: str
  vnc: Optional[str]


def _print_json(value: Any) -> None:
  print(json.dumps(value, ensure_ascii=False, separators=(',', ':')))


def _open_client() -> Client:
  try:
    return mission.open_client()
  except (mission.MissionError, RuntimeError) as error:
    raise WebviewError(str(error)) from error


def _launch_args(
  *,
  vnc: bool,
  vnc_port: int | None,
  cookies: str | None,
  allowed_origins: list[str],
  blocked_origins: list[str],
  share: list[str],
  timeout: Optional[float],
) -> dict[str, Any]:
  args: dict[str, Any] = {
    'type': WEBVIEW,
    'vnc': vnc,
    'allowed_origins': allowed_origins,
    'blocked_origins': blocked_origins,
  }
  if vnc_port is not None:
    args['vnc_port'] = vnc_port
  if cookies is not None:
    args['pass'] = [f'cookies+{cookies}']
  if len(share) > 0:
    args['share'] = share
  if timeout is not None:
    args['timeout'] = timeout
  return args


@contextlib.contextmanager
def _unfinished_launch(client: Client, mission_id: str) -> Generator[Callable[[], None]]:
  active = True

  def completed() -> None:
    nonlocal active
    active = False

  try:
    yield completed
  except BaseException as error:
    if active:
      try:
        mission.request_cancel(mission_id, client=client)
      except (mission.MissionError, ConnectionError) as cancel_error:
        error.add_note(f'cancelling unfinished webview {mission_id} failed: {cancel_error}')
    raise
  if active:
    mission.request_cancel(mission_id, client=client)


def _result_error(message: Message, action: str) -> WebviewError:
  payload = message.payload
  outcome = payload.get('outcome')
  reason = payload.get('error')
  if not isinstance(reason, str) or len(reason.strip()) == 0:
    detail = payload.get('detail')
    if isinstance(detail, dict):
      reason = detail.get('reason')
  if not isinstance(reason, str) or len(reason.strip()) == 0:
    reason = str(payload)
  return WebviewError(f'webview {action} {outcome or "failed"}: {reason}')


def _validate_ready(message: Message, transitions: list[str]) -> Optional[str]:
  if transitions[:2] != ['accepted', 'started']:
    raise WebviewError(
      f'webview became ready before accepted and started marks: {", ".join(transitions) or "none"}'
    )
  if set(message.payload) != {'event', 'vnc'}:
    raise WebviewError(f'webview ready event is malformed: {message.payload}')
  vnc = message.payload['vnc']
  if vnc is not None and not isinstance(vnc, str):
    raise WebviewError(f'webview ready event has invalid VNC URL: {vnc!r}')
  return vnc


def open_webview(
  *,
  vnc: bool = False,
  vnc_port: int | None = None,
  cookies: str | None = None,
  allowed_origins: Optional[list[str]] = None,
  blocked_origins: Optional[list[str]] = None,
  share: Optional[list[str]] = None,
  timeout: Optional[float] = None,
) -> OpenedWebview:
  """Launch a webview and wait through startup until its ready event."""
  from bro.broker.brotocol import Tag

  if vnc_port is not None and not vnc:
    raise ValueError('webview --port requires --vnc')
  if cookies == '':
    raise ValueError('webview --cookies must name a non-empty instance')
  launch_args = _launch_args(
    vnc=vnc,
    vnc_port=vnc_port,
    cookies=cookies,
    allowed_origins=[] if allowed_origins is None else allowed_origins,
    blocked_origins=[] if blocked_origins is None else blocked_origins,
    share=[] if share is None else share,
    timeout=timeout,
  )
  with _open_client() as client:
    request = client.send(mission.LAUNCH, launch_args)
    transitions: list[str] = []

    def on_interim(message: Message) -> None:
      if message.type != Tag.MARK:
        raise WebviewError(f'unexpected webview launch message before ready: {message.payload}')
      transition = message.payload.get('transition')
      if transition == 'accepted':
        if len(transitions) != 0:
          raise WebviewError(f'unexpected webview accepted mark after {transitions[-1]}')
      elif transition == 'started':
        if transitions != ['accepted']:
          raise WebviewError('webview started before its accepted mark')
      elif transition == 'listening':
        if transitions[:2] != ['accepted', 'started']:
          raise WebviewError('webview listened before its started mark')
      else:
        raise WebviewError(f'unexpected webview launch mark: {transition!r}')
      transitions.append(str(transition))

    with _unfinished_launch(client, request.request_id) as completed:
      try:
        reply = client.await_reply(
          request,
          OPEN_TIMEOUT,
          on_interim=on_interim,
          timeout_after_interim=OPEN_TIMEOUT,
          rearm_on_interim=lambda message: message.type == Tag.MARK,
          until=lambda message: (
            message.type == Tag.MESSAGE and message.payload.get('event') == 'ready'
          ),
        )
      except TimeoutError:
        raise WebviewError(
          f'webview open timed out after {OPEN_TIMEOUT:.0f}s; its launch was cancelled'
        ) from None
      except ConnectionError as error:
        raise WebviewError(f'broker channel closed while opening the webview: {error}') from None
      if reply.type == Tag.RESULT:
        completed()
        raise _result_error(reply, 'open')
      vnc_url = _validate_ready(reply, transitions)
      completed()
      return OpenedWebview(request.request_id, vnc_url)


def _worker_has_replied(record: dict[str, Any]) -> bool:
  messages = record.get('messages')
  if not isinstance(messages, list) or not all(isinstance(entry, dict) for entry in messages):
    raise WebviewError('webview mission carried a malformed conversation')
  return any(
    entry.get('transition') == 'message' and entry.get('from') == 'worker' for entry in messages
  )


def _wait_until_ready(client: Client, mission_id: str) -> None:
  try:
    record = mission.query_mission(client, mission_id)
    mission.caller_end(record, mission_id)
  except (mission.MissionError, ConnectionError) as error:
    raise WebviewError(f'could not read webview {mission_id}: {error}') from None
  if record.get('kind') != mission.LAUNCH or record.get('type') != WEBVIEW:
    raise WebviewError(f'mission {mission_id!r} is not a webview')
  while record.get('state') in ('accepted', 'started'):
    if _worker_has_replied(record):
      return
    sequence = record.get('chat_seq')
    if not isinstance(sequence, int) or isinstance(sequence, bool):
      raise WebviewError('webview mission carried a malformed chat sequence')
    try:
      record = mission.query_mission(
        client,
        mission_id,
        wait_seconds=mission.READ_WAIT_SECONDS,
        since=sequence,
      )
    except (mission.MissionError, ConnectionError) as error:
      raise WebviewError(f'could not wait for webview {mission_id} readiness: {error}') from None
  state = record.get('state')
  result = record.get('result')
  reason = result.get('error') if isinstance(result, dict) else None
  raise WebviewError(f'webview {mission_id} ended before it was ready: {reason or state}')


def close_webview(mission_id: str) -> dict[str, Any]:
  """Ask a live webview to close and wait for its terminal outcome."""
  from bro.broker.brotocol import Message, Tag

  selected = mission.resolve(mission_id)
  with _open_client() as client:
    _wait_until_ready(client, selected)
    try:
      asked = mission.ask(selected, {'webview': 'close'}, wait=math.inf, client=client)
    except (mission.MissionError, ConnectionError) as error:
      raise WebviewError(f'could not close webview {selected}: {error}') from None
    if asked.answer != {'closed': True}:
      raise WebviewError(f'webview {selected} returned an invalid close reply: {asked.answer}')
    while True:
      try:
        record = mission.query_mission(
          client,
          selected,
          wait_seconds=mission.READ_WAIT_SECONDS,
          wait_for_settlement=True,
        )
      except (mission.MissionError, ConnectionError) as error:
        raise WebviewError(f'could not read webview {selected} outcome: {error}') from None
      if record.get('settled') is not True:
        continue
      state = record.get('state')
      if state in ('accepted', 'started'):
        raise WebviewError(f'webview {selected} settled while still {state}')
      if state not in ('ended', 'denied'):
        raise WebviewError(f'webview {selected} ended in an unknown state: {state!r}')
      result = record.get('result')
      if not isinstance(result, dict):
        raise WebviewError(f'webview {selected} ended without a retained outcome')
      if result.get('outcome') != 'ok':
        raise _result_error(Message(type=Tag.RESULT, request=selected, payload=result), 'close')
      return result


def _positive_seconds(value: str) -> float:
  seconds = float(value)
  if not math.isfinite(seconds) or seconds <= 0:
    raise ValueError('must be a finite positive number')
  return seconds


def _host_port(value: str) -> int:
  port = int(value)
  if not 1024 <= port <= 65535:
    raise ValueError('must be an integer in 1024..65535')
  return port


def _non_empty_instance(value: str) -> str:
  if value == '':
    raise argparse.ArgumentTypeError('must not be empty')
  return value


def _open(
  vnc: bool,
  port: int | None,
  cookies: str | None,
  allowed_origins: Optional[list[str]],
  blocked_origins: Optional[list[str]],
  share: Optional[list[str]],
  timeout: Optional[float],
) -> int:
  try:
    opened = open_webview(
      vnc=vnc,
      vnc_port=port,
      cookies=cookies,
      allowed_origins=allowed_origins,
      blocked_origins=blocked_origins,
      share=share,
      timeout=timeout,
    )
  except (WebviewError, ValueError) as error:
    log.error('%s', error)
    return 1
  _print_json({'mission': opened.mission_id, 'vnc': opened.vnc})
  return 0


def _close(mission_id: str) -> int:
  try:
    outcome = close_webview(mission_id)
  except (WebviewError, mission.MissionError, ValueError) as error:
    log.error('%s', error)
    return 1
  _print_json(outcome)
  return 0


def _serve() -> int:
  from bro.webview import serve

  try:
    asyncio.run(serve.serve())
  except Exception as error:
    log.error('webview serve failed: %s', error)
    return 1
  return 0


def _capture() -> int:
  from bro.webview.capture import run_capture

  return run_capture()


def _setup(
  instance: str,
  url: str | None,
  fresh: bool,
  indexed_db: bool,
  port: int | None,
) -> int:
  from bro.webview.setup import SetupError, setup_profile

  try:
    summary = setup_profile(
      instance,
      url=url,
      fresh=fresh,
      indexed_db=indexed_db,
      port=port,
    )
  except (SetupError, ValueError, OSError) as error:
    log.error('webview setup failed: %s', error)
    return 1
  print(summary)
  return 0


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(prog='webview', description='open, close, and set up browser profiles')
  verbs = parser.add_subparsers(dest='verb', metavar='<verb>')

  open_parser = verbs.add_parser(
    'open', help='launch a webview and wait until its browser is ready'
  )
  open_parser.add_argument('--vnc', action='store_true', help='publish a loopback noVNC view')
  open_parser.add_argument(
    '--port',
    type=_host_port,
    metavar='PORT',
    help='publish the noVNC view on this loopback port; requires --vnc',
  )
  open_parser.add_argument(
    '--cookies',
    type=_non_empty_instance,
    metavar='INSTANCE',
    help='start from the passed cookies profile instance',
  )
  open_parser.add_argument(
    '--allow',
    action='append',
    dest='allowed_origins',
    metavar='ORIGIN',
    help='an origin the page may request, refusing every other; repeat to add another',
  )
  open_parser.add_argument(
    '--block',
    action='append',
    dest='blocked_origins',
    metavar='ORIGIN',
    help='an origin the page may not request; repeat to add another',
  )
  open_parser.add_argument(
    '--share',
    action='append',
    metavar='REF',
    help='artifact ref to expose read-only under /workspace/artifacts; repeat to add another',
  )
  open_parser.add_argument(
    '--timeout',
    type=_positive_seconds,
    metavar='SECONDS',
    help='maximum lifetime of the webview; omitted means no lifetime bound',
  )
  open_parser.set_handler(_open)

  close_parser = verbs.add_parser('close', help='close a webview and wait for its terminal outcome')
  close_parser.add_argument('mission_id', metavar='MISSION', help='mission id')
  close_parser.set_handler(_close)

  setup_parser = verbs.add_parser(
    'setup', help='capture a browser profile through an interactive noVNC view'
  )
  setup_parser.add_argument('instance', type=_non_empty_instance, metavar='INSTANCE')
  setup_parser.add_argument('--url', help='open this URL before the user takes control')
  setup_parser.add_argument(
    '--fresh', action='store_true', help='start with an empty browser profile'
  )
  setup_parser.add_argument(
    '--indexed-db',
    action='store_true',
    help='include IndexedDB records in the captured profile',
  )
  setup_parser.add_argument(
    '--port',
    type=_host_port,
    metavar='PORT',
    help='publish the noVNC view on this loopback port',
  )
  setup_parser.set_handler(_setup)

  serve_parser = verbs.add_parser('serve', help='run the container-side webview daemon')
  serve_parser.set_handler(_serve)
  capture_parser = verbs.add_parser('capture', help='run the container-side profile capture')
  capture_parser.set_handler(_capture)

  return parser.dispatch(argv)
