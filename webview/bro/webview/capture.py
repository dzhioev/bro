"""Container-side browser profile capture over a bounded JSON-line exchange."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, NoReturn

from bro.base.offload import off_loop
from bro.webview import serve
from bro.webview.capture_protocol import CAPTURE_OPTIONS_ENV, EXCHANGE_MAX_BYTES
from bro.webview.profile import (
  PROFILE_MAX_BYTES,
  ProfileError,
  decode_storage_state,
  validate_storage_state,
)

_STATE_FILENAME = 'captured-state.json'
_STORAGE_STATE_CODE = (
  'async (page) => { await page.context().storageState({ path: %s, indexedDB: %s }); }'
)


class CaptureError(Exception):
  """The setup-to-capture exchange or browser capture failed."""


@dataclass(frozen=True)
class CaptureOptions:
  url: str | None
  indexed_db: bool


def _reject_json_constant(_constant: str) -> NoReturn:
  raise ValueError('not strict JSON')


def decode_options(raw: str) -> CaptureOptions:
  try:
    value = json.loads(raw, parse_constant=_reject_json_constant)
  except (json.JSONDecodeError, ValueError) as error:
    raise CaptureError(f'{CAPTURE_OPTIONS_ENV} is not valid JSON') from error
  if not isinstance(value, dict) or set(value) != {'url', 'indexed_db'}:
    raise CaptureError(f'{CAPTURE_OPTIONS_ENV} must carry exactly url and indexed_db')
  url = value['url']
  if url is not None and (not isinstance(url, str) or url == ''):
    raise CaptureError(f'{CAPTURE_OPTIONS_ENV} field url must be null or a non-empty string')
  indexed_db = value['indexed_db']
  if not isinstance(indexed_db, bool):
    raise CaptureError(f'{CAPTURE_OPTIONS_ENV} field indexed_db must be a boolean')
  return CaptureOptions(url, indexed_db)


def _read_line(stream: BinaryIO, subject: str) -> dict[str, Any]:
  line = stream.readline(EXCHANGE_MAX_BYTES + 1)
  if line == b'':
    raise CaptureError(f'capture exchange ended before {subject}')
  if len(line) > EXCHANGE_MAX_BYTES or not line.endswith(b'\n'):
    raise CaptureError(
      f'capture exchange {subject} exceeds the {EXCHANGE_MAX_BYTES}-byte line limit'
    )
  try:
    value = json.loads(line, parse_constant=_reject_json_constant)
  except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
    raise CaptureError(f'capture exchange {subject} is not valid JSON') from error
  if not isinstance(value, dict):
    raise CaptureError(f'capture exchange {subject} must be an object')
  return value


def _write_line(stream: BinaryIO, value: dict[str, Any]) -> None:
  try:
    encoded = json.dumps(
      value,
      ensure_ascii=True,
      separators=(',', ':'),
      allow_nan=False,
    ).encode()
  except ValueError as error:
    raise CaptureError('capture result is not valid JSON') from error
  if len(encoded) + 1 > EXCHANGE_MAX_BYTES:
    raise CaptureError(f'capture result exceeds the {EXCHANGE_MAX_BYTES}-byte line limit')
  stream.write(encoded + b'\n')
  stream.flush()


def _read_seed(stream: BinaryIO) -> dict[str, Any] | None:
  message = _read_line(stream, 'seed')
  if set(message) != {'seed'}:
    raise CaptureError("capture exchange first line must be {'seed': <state>|null}")
  seed = message['seed']
  if seed is None:
    return None
  try:
    return validate_storage_state(seed)
  except ProfileError as error:
    raise CaptureError(f'capture seed is invalid: {error}') from error


def _read_request(stream: BinaryIO) -> None:
  if _read_line(stream, 'state request') != {'request': 'state'}:
    raise CaptureError("capture exchange request must be {'request':'state'}")


async def _navigate(
  session: Any,
  supervisor: serve.ProcessSupervisor,
  url: str,
) -> None:
  result = await supervisor.run(
    session.call_tool('browser_navigate', {'url': url}),
    timeout=serve.COMMAND_DEADLINE,
    closed_process='Playwright MCP',
  )
  if result.isError:
    print(f'initial navigation failed: {serve._content_text(result)}', file=sys.stderr)


async def _capture_state(
  session: Any,
  supervisor: serve.ProcessSupervisor,
  path: Path,
  indexed_db: bool,
) -> dict[str, Any]:
  code = _STORAGE_STATE_CODE % (
    json.dumps(str(path), ensure_ascii=True),
    'true' if indexed_db else 'false',
  )
  result = await supervisor.run(
    session.call_tool('browser_run_code_unsafe', {'code': code}),
    timeout=serve.COMMAND_DEADLINE,
    closed_process='Playwright MCP',
  )
  if result.isError:
    raise CaptureError(f'Playwright storage-state capture failed: {serve._content_text(result)}')
  try:
    with path.open('rb') as state_file:
      raw = state_file.read(PROFILE_MAX_BYTES + 1)
  except FileNotFoundError as error:
    raise CaptureError('Playwright storage-state capture wrote no state file') from error
  finally:
    path.unlink(missing_ok=True)
  if len(raw) > PROFILE_MAX_BYTES:
    raise CaptureError(f'captured profile exceeds the {PROFILE_MAX_BYTES}-byte limit')
  try:
    return decode_storage_state(raw.decode())
  except (UnicodeDecodeError, ProfileError) as error:
    raise CaptureError(f'captured profile is invalid: {error}') from error


async def capture(stdin: BinaryIO, stdout: BinaryIO) -> None:
  raw_options = os.environ.get(CAPTURE_OPTIONS_ENV)
  if raw_options is None:
    raise CaptureError(f'{CAPTURE_OPTIONS_ENV} is unset')
  options = decode_options(raw_options)
  seed = _read_seed(stdin)

  with tempfile.TemporaryDirectory(prefix='bro-webview-capture-') as directory:
    root = Path(directory)
    with contextlib.ExitStack() as process_stack:
      xvfb, display = process_stack.enter_context(serve._xvfb())
      vnc_processes = process_stack.enter_context(serve._vnc(display))
      config, storage_path = process_stack.enter_context(serve._browser_config(seed))
      processes = [('Xvfb', xvfb), *vnc_processes]
      async with contextlib.AsyncExitStack() as async_stack:
        session, mcp_process = await async_stack.enter_async_context(
          serve._playwright(
            serve.Options(True, (), ()),
            config,
            storage_path,
            display,
            workspace=root,
            output_directory=root / 'output',
          )
        )
        async with serve._supervising([*processes, ('Playwright MCP', mcp_process)]) as supervisor:
          await serve._warm_browser(session, supervisor)
          if options.url is not None:
            await _navigate(session, supervisor, options.url)
          _write_line(stdout, {'event': 'ready'})
          await supervisor.run(off_loop(_read_request, stdin))
          try:
            state = await _capture_state(
              session,
              supervisor,
              root / _STATE_FILENAME,
              options.indexed_db,
            )
          except Exception as error:
            _write_line(stdout, {'event': 'error', 'error': str(error)})
            raise
          _write_line(stdout, {'event': 'state', 'state': state})


def run_capture() -> int:
  try:
    asyncio.run(capture(sys.stdin.buffer, sys.stdout.buffer))
  except Exception as error:
    print(f'webview capture failed: {error}', file=sys.stderr)
    return 1
  return 0
