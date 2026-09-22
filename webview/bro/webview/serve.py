"""Container-side webview daemon over a mission chat and Playwright MCP."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import socket
import stat
import subprocess
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Iterator, Mapping, Sequence
from contextlib import AsyncExitStack, ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Optional, cast

from mcp import ClientSession, types as mcp_types
from mcp.shared.exceptions import McpError
from mcp.shared.message import SessionMessage

from bro.artifact import ArtifactError, mint_artifact
from bro.broker.brotocol import Message, Tag
from bro.broker.client import Client
from bro.broker.environment import BROKER_MISSION
from bro.broker.journal import MAX_MESSAGE_BYTES

WORKSPACE = Path('/workspace')
ARTIFACT_VIEW = WORKSPACE / 'artifacts'
MOUNTINFO = Path('/proc/self/mountinfo')
OUTPUT_DIRECTORY = WORKSPACE / 'output'
OPTIONS_ENV = 'WEBVIEW_OPTIONS'
PUBLISHED_PORTS_ENV = 'RIDE_PUBLISHED_PORTS'
PLAYWRIGHT_BROWSERS_PATH_ENV = 'PLAYWRIGHT_BROWSERS_PATH'
PLAYWRIGHT_COMMAND = 'playwright-mcp'
X11_PORT = 5900
VNC_PORT = 6080
COMMAND_DEADLINE = 120.0
OUTPUT_LIMIT_BYTES = 256 << 20
ERROR_HEAD_CHARACTERS = 1024
WITHHELD_TOOLS = frozenset({'browser_run_code_unsafe'})
_PROCESS_POLL_SECONDS = 0.05


@dataclass(frozen=True)
class Options:
  vnc: bool
  allowed_origins: tuple[str, ...]
  blocked_origins: tuple[str, ...]


@dataclass(frozen=True)
class FileState:
  kind: str
  size: int
  modified_ns: int
  digest: bytes


class DaemonError(Exception):
  """A violated daemon startup or runtime assumption."""


def _origins(value: Any, name: str) -> tuple[str, ...]:
  if not isinstance(value, list) or any(
    not isinstance(origin, str) or len(origin) == 0 or ';' in origin for origin in value
  ):
    raise DaemonError(f"{OPTIONS_ENV} field '{name}' must be a list of strings without ';'")
  return tuple(value)


def decode_options(raw: str) -> Options:
  try:
    value = json.loads(raw)
  except json.JSONDecodeError as error:
    raise DaemonError(f'{OPTIONS_ENV} is not valid JSON: {error}') from error
  if not isinstance(value, dict):
    raise DaemonError(f'{OPTIONS_ENV} must decode to an object')
  unknown = sorted(set(value) - {'vnc', 'allowed_origins', 'blocked_origins'})
  missing = sorted({'vnc', 'allowed_origins', 'blocked_origins'} - set(value))
  if len(unknown) > 0 or len(missing) > 0:
    details = []
    if len(unknown) > 0:
      details.append(f'unknown field(s): {", ".join(unknown)}')
    if len(missing) > 0:
      details.append(f'missing field(s): {", ".join(missing)}')
    raise DaemonError(f'{OPTIONS_ENV} {"; ".join(details)}')
  vnc = value['vnc']
  if not isinstance(vnc, bool):
    raise DaemonError(f"{OPTIONS_ENV} field 'vnc' must be a boolean")
  return Options(
    vnc=vnc,
    allowed_origins=_origins(value['allowed_origins'], 'allowed_origins'),
    blocked_origins=_origins(value['blocked_origins'], 'blocked_origins'),
  )


def _require_artifact_view() -> None:
  try:
    mount_lines = MOUNTINFO.read_text().splitlines()
  except OSError as error:
    raise DaemonError(f'cannot read the process mount table at {MOUNTINFO}: {error}') from error
  mounted = False
  for line in mount_lines:
    fields = line.split()
    if len(fields) < 6:
      raise DaemonError(f'{MOUNTINFO} contains a malformed mount record: {line!r}')
    if fields[4] == str(ARTIFACT_VIEW):
      mounted = True
      break
  if not ARTIFACT_VIEW.is_dir() or not mounted:
    raise DaemonError(f'artifact view is not mounted at {ARTIFACT_VIEW}')


def _terminate_process(process: subprocess.Popen) -> None:
  if process.poll() is None:
    process.terminate()
  process.wait()


@contextlib.contextmanager
def _xvfb() -> Iterator[tuple[subprocess.Popen, str]]:
  process = subprocess.Popen(
    ('Xvfb', '-displayfd', '1', '-screen', '0', '1280x720x24', '-nolisten', 'tcp'),
    stdout=subprocess.PIPE,
    text=True,
  )
  try:
    assert process.stdout is not None
    line = process.stdout.readline()
    if line == '':
      raise DaemonError(f'Xvfb exited before publishing a display (status {process.wait()})')
    display_number = line.strip()
    if not display_number.isdecimal():
      raise DaemonError(f'Xvfb published an invalid display number: {display_number!r}')
    yield process, f':{display_number}'
  finally:
    if process.stdout is not None:
      process.stdout.close()
    _terminate_process(process)


@contextlib.contextmanager
def _child(name: str, command: Sequence[str]) -> Iterator[subprocess.Popen]:
  process = subprocess.Popen(tuple(command))
  try:
    yield process
  finally:
    _terminate_process(process)


def _port_is_open(port: int) -> bool:
  try:
    with socket.create_connection(('127.0.0.1', port), timeout=_PROCESS_POLL_SECONDS):
      return True
  except OSError:
    return False


def _await_port(name: str, process: subprocess.Popen, port: int) -> None:
  while not _port_is_open(port):
    status = process.poll()
    if status is not None:
      raise DaemonError(f'{name} exited before port {port} was ready (status {status})')
    time.sleep(_PROCESS_POLL_SECONDS)


def _published_port(container_port: int, raw: str) -> int:
  ports: dict[int, int] = {}
  for pair in raw.split(',') if raw else []:
    container, separator, host = pair.partition('=')
    if separator == '' or not container.isdecimal() or not host.isdecimal():
      raise DaemonError(f'{PUBLISHED_PORTS_ENV} contains malformed mapping {pair!r}')
    container_number = int(container)
    host_number = int(host)
    if not 1 <= container_number <= 65535 or not 1 <= host_number <= 65535:
      raise DaemonError(f'{PUBLISHED_PORTS_ENV} contains out-of-range mapping {pair!r}')
    if container_number in ports:
      raise DaemonError(f'{PUBLISHED_PORTS_ENV} repeats container port {container_number}')
    ports[container_number] = host_number
  try:
    return ports[container_port]
  except KeyError as error:
    raise DaemonError(
      f'{PUBLISHED_PORTS_ENV} has no host mapping for container port {container_port}'
    ) from error


@contextlib.contextmanager
def _vnc(display: str) -> Iterator[tuple[tuple[str, subprocess.Popen], ...]]:
  with ExitStack() as stack:
    x11vnc = stack.enter_context(
      _child(
        'x11vnc',
        (
          'x11vnc',
          '-display',
          display,
          '-localhost',
          '-rfbport',
          str(X11_PORT),
          '-forever',
          '-shared',
          '-nopw',
        ),
      )
    )
    _await_port('x11vnc', x11vnc, X11_PORT)
    websockify = stack.enter_context(
      _child(
        'websockify',
        ('websockify', '--web=/usr/share/novnc', str(VNC_PORT), f'127.0.0.1:{X11_PORT}'),
      )
    )
    _await_port('websockify', websockify, VNC_PORT)
    yield (('x11vnc', x11vnc), ('websockify', websockify))


@contextlib.contextmanager
def _browser_config() -> Iterator[Path]:
  with tempfile.TemporaryDirectory(prefix='bro-webview-') as directory:
    path = Path(directory) / 'playwright.json'
    path.write_text(json.dumps({'browser': {'contextOptions': {'acceptDownloads': False}}}))
    yield path


def _playwright_arguments(options: Options, config: Path) -> list[str]:
  arguments = [
    '--isolated',
    '--no-sandbox',
    '--browser',
    'chromium',
    '--output-dir',
    str(OUTPUT_DIRECTORY),
    '--image-responses',
    'omit',
    '--file-paths',
    'absolute',
    '--config',
    str(config),
  ]
  if len(options.allowed_origins) > 0:
    arguments += ['--allowed-origins', ';'.join(options.allowed_origins)]
  if len(options.blocked_origins) > 0:
    arguments += ['--blocked-origins', ';'.join(options.blocked_origins)]
  return arguments


def _playwright_environment(display: str) -> dict[str, str]:
  names = ('PATH', 'HOME', PLAYWRIGHT_BROWSERS_PATH_ENV)
  missing = [name for name in names if not os.environ.get(name)]
  if len(missing) > 0:
    raise DaemonError(f'Playwright environment lacks {", ".join(missing)}')
  return {name: os.environ[name] for name in names} | {'DISPLAY': display}


class _ReadStream:
  def __init__(self, stdout: BinaryIO):
    self._stdout = stdout
    self._queue: asyncio.Queue[SessionMessage | Exception | None] = asyncio.Queue()

  async def pump(self) -> None:
    try:
      while line := await asyncio.to_thread(self._stdout.readline):
        try:
          message = mcp_types.JSONRPCMessage.model_validate_json(line)
        except Exception as error:
          await self._queue.put(error)
        else:
          await self._queue.put(SessionMessage(message))
    finally:
      await self._queue.put(None)

  def __aiter__(self):
    return self

  async def __anext__(self) -> SessionMessage | Exception:
    message = await self._queue.get()
    if message is None:
      raise StopAsyncIteration
    return message

  async def __aenter__(self):
    return self

  async def __aexit__(self, *args) -> None:
    await self.aclose()

  async def aclose(self) -> None:
    pass


class _WriteStream:
  def __init__(self, stdin: BinaryIO):
    self._stdin = stdin
    self._closed = False

  async def send(self, session_message: SessionMessage) -> None:
    if self._closed:
      raise ConnectionError('Playwright MCP input is closed')
    encoded = session_message.message.model_dump_json(by_alias=True, exclude_none=True).encode()

    def write() -> None:
      self._stdin.write(encoded + b'\n')
      self._stdin.flush()

    await asyncio.to_thread(write)

  async def __aenter__(self):
    return self

  async def __aexit__(self, *args) -> None:
    await self.aclose()

  async def aclose(self) -> None:
    if self._closed:
      return
    self._closed = True
    await asyncio.to_thread(self._stdin.close)


async def _await_task(task: asyncio.Task[Any]) -> None:
  await task


@contextlib.asynccontextmanager
async def _playwright(
  options: Options, config: Path, display: str
) -> AsyncIterator[tuple[ClientSession, subprocess.Popen]]:
  async with AsyncExitStack() as stack:
    process = subprocess.Popen(
      (PLAYWRIGHT_COMMAND, *_playwright_arguments(options, config)),
      stdin=subprocess.PIPE,
      stdout=subprocess.PIPE,
      cwd=WORKSPACE,
      env=_playwright_environment(display),
    )
    assert process.stdin is not None
    assert process.stdout is not None
    stack.callback(process.stdout.close)
    read_stream = _ReadStream(cast(BinaryIO, process.stdout))
    write_stream = _WriteStream(cast(BinaryIO, process.stdin))
    reader = asyncio.create_task(read_stream.pump())
    stack.push_async_callback(_await_task, reader)
    stack.push_async_callback(asyncio.to_thread, _terminate_process, process)
    stack.push_async_callback(write_stream.aclose)
    session = ClientSession(cast(Any, read_stream), cast(Any, write_stream))
    await session.__aenter__()
    stack.push_async_callback(session.__aexit__, None, None, None)
    await session.initialize()
    yield session, process


class ProcessSupervisor:
  def __init__(self):
    self._tasks: dict[asyncio.Task[int], str] = {}

  def add_sync(self, name: str, process: subprocess.Popen) -> None:
    self._tasks[asyncio.create_task(asyncio.to_thread(process.wait))] = name

  def _exit_error(self, task: asyncio.Task[int]) -> DaemonError:
    return DaemonError(f'{self._tasks[task]} exited unexpectedly with status {task.result()}')

  async def run(
    self,
    awaitable: Awaitable[Any],
    *,
    timeout: Optional[float] = None,
    closed_process: Optional[str] = None,
  ) -> Any:
    operation = asyncio.ensure_future(awaitable)
    done, _ = await asyncio.wait(
      {operation, *self._tasks},
      timeout=timeout,
      return_when=asyncio.FIRST_COMPLETED,
    )
    exited = next((task for task in done if task in self._tasks), None)
    if exited is not None:
      operation.cancel()
      with contextlib.suppress(asyncio.CancelledError, McpError):
        await operation
      raise self._exit_error(exited)
    if operation not in done:
      operation.cancel()
      with contextlib.suppress(asyncio.CancelledError):
        await operation
      raise TimeoutError
    try:
      return operation.result()
    except McpError as error:
      if closed_process is None or 'Connection closed' not in str(error):
        raise
      process_task = next(task for task, name in self._tasks.items() if name == closed_process)
      await process_task
      raise self._exit_error(process_task) from error

  async def close(self) -> None:
    for task in self._tasks:
      task.cancel()
    await asyncio.gather(*self._tasks, return_exceptions=True)


@contextlib.asynccontextmanager
async def _supervising(
  processes: Sequence[tuple[str, subprocess.Popen]],
) -> AsyncIterator[ProcessSupervisor]:
  supervisor = ProcessSupervisor()
  for name, process in processes:
    supervisor.add_sync(name, process)
  try:
    yield supervisor
  finally:
    await supervisor.close()


def _file_digest(path: Path) -> bytes:
  with path.open('rb') as content:
    return hashlib.file_digest(content, 'sha256').digest()


def workspace_files() -> dict[Path, FileState]:
  files: dict[Path, FileState] = {}
  for directory, directory_names, file_names in os.walk(WORKSPACE, followlinks=False):
    root = Path(directory)
    directory_names[:] = [
      name
      for name in directory_names
      if (root / name) != ARTIFACT_VIEW and not (root / name).is_symlink()
    ]
    for name in file_names:
      path = root / name
      metadata = path.lstat()
      if stat.S_ISREG(metadata.st_mode):
        kind = 'file'
        digest = _file_digest(path)
      elif stat.S_ISLNK(metadata.st_mode):
        kind = 'symlink'
        digest = hashlib.sha256(os.readlink(path).encode()).digest()
      else:
        kind = 'special'
        digest = b''
      files[path.relative_to(WORKSPACE)] = FileState(
        kind=kind,
        size=metadata.st_size,
        modified_ns=metadata.st_mtime_ns,
        digest=digest,
      )
  return files


def _changed_files(before: Mapping[Path, FileState], after: Mapping[Path, FileState]) -> list[Path]:
  return sorted(path for path, state in after.items() if before.get(path) != state)


def discard_changed_files(before: Mapping[Path, FileState]) -> None:
  after = workspace_files()
  for path in _changed_files(before, after):
    _remove_workspace_file(path)


def _remove_workspace_file(relative: Path) -> None:
  path = WORKSPACE / relative
  with contextlib.suppress(FileNotFoundError):
    path.unlink()
  parent = path.parent
  while parent != WORKSPACE and parent != ARTIFACT_VIEW:
    try:
      parent.rmdir()
    except OSError:
      break
    parent = parent.parent


@contextlib.contextmanager
def _removing_workspace_file(relative: Path) -> Iterator[None]:
  try:
    yield
  finally:
    _remove_workspace_file(relative)


async def collect_files(before: Mapping[Path, FileState]) -> list[dict[str, Any]]:
  after = await asyncio.to_thread(workspace_files)
  changed = _changed_files(before, after)
  total = sum(after[path].size for path in changed)
  if total > OUTPUT_LIMIT_BYTES:
    for path in changed:
      _remove_workspace_file(path)
    raise DaemonError(
      f'command wrote {total} bytes, exceeding the {OUTPUT_LIMIT_BYTES}-byte output limit'
    )
  collected: list[dict[str, Any]] = []
  for path in changed:
    with _removing_workspace_file(path):
      if after[path].kind != 'file':
        collected.append({'name': str(path), 'error': f'unsupported {after[path].kind} output'})
        continue
      try:
        minted = await asyncio.to_thread(mint_artifact, str(path))
      except (ArtifactError, OSError, ValueError) as error:
        collected.append({'name': str(path), 'error': str(error)})
      else:
        collected.append({'name': str(path), 'ref': minted.ref})
  return collected


def _content_text(result: mcp_types.CallToolResult) -> str:
  return '\n'.join(item.text for item in result.content if isinstance(item, mcp_types.TextContent))


def _tools_text(tools: Sequence[mcp_types.Tool]) -> str:
  return json.dumps(
    [
      {
        'name': tool.name,
        'description': tool.description,
        'inputSchema': tool.inputSchema,
      }
      for tool in tools
      if tool.name not in WITHHELD_TOOLS
    ],
    ensure_ascii=False,
    separators=(',', ':'),
  )


def _payload_bytes(payload: dict[str, Any]) -> bytes:
  return json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()


@contextlib.contextmanager
def _temporary_workspace_file(content: bytes, suffix: str) -> Iterator[Path]:
  with tempfile.NamedTemporaryFile(
    dir=WORKSPACE, prefix='.webview-', suffix=suffix, delete=False
  ) as file:
    file.write(content)
    path = Path(file.name)
  try:
    yield path
  finally:
    path.unlink(missing_ok=True)


async def _mint_bytes(content: bytes, suffix: str) -> tuple[str, int]:
  with _temporary_workspace_file(content, suffix) as path:
    minted = await asyncio.to_thread(mint_artifact, path.name)
  return minted.ref, len(content)


def _spill_error(error: Exception, dropped: int) -> dict[str, Any]:
  reply = {'error': str(error)[:ERROR_HEAD_CHARACTERS], 'dropped': dropped}
  if len(_payload_bytes(reply)) > MAX_MESSAGE_BYTES:
    raise RuntimeError('bounded spill error exceeds the broker message limit')
  return reply


async def bound_reply(reply: dict[str, Any]) -> dict[str, Any]:
  encoded = _payload_bytes(reply)
  if len(encoded) <= MAX_MESSAGE_BYTES:
    return reply
  text = reply.get('text')
  candidate = reply
  if isinstance(text, str):
    text_bytes = text.encode()
    try:
      ref, size = await _mint_bytes(text_bytes, '.txt')
    except (ArtifactError, OSError, ValueError) as error:
      return _spill_error(error, len(text_bytes))
    candidate = {
      'spilled': 'text',
      'ref': ref,
      'bytes': size,
      'files': reply.get('files', []),
    }
    if len(_payload_bytes(candidate)) <= MAX_MESSAGE_BYTES:
      return candidate
  try:
    ref, size = await _mint_bytes(encoded, '.json')
  except (ArtifactError, OSError, ValueError) as error:
    return _spill_error(error, len(encoded))
  return {'spilled': 'reply', 'ref': ref, 'bytes': size}


def _parse_command(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
  if set(payload) not in ({'tool'}, {'tool', 'arguments'}):
    raise ValueError(
      "command must be {'tool': <name>, 'arguments': <object>} with arguments optional"
    )
  tool = payload.get('tool')
  arguments = payload.get('arguments', {})
  if not isinstance(tool, str) or len(tool) == 0 or not isinstance(arguments, dict):
    raise ValueError("command needs a non-empty string 'tool' and object 'arguments'")
  return tool, arguments


async def _tool_reply(
  session: ClientSession,
  supervisor: ProcessSupervisor,
  payload: dict[str, Any],
) -> dict[str, Any]:
  try:
    tool, arguments = _parse_command(payload)
  except ValueError as error:
    return {'error': str(error)}
  if tool in WITHHELD_TOOLS:
    return {'error': f'webview withholds Playwright MCP tool {tool!r}'}
  before = await asyncio.to_thread(workspace_files)
  try:
    result = await supervisor.run(
      session.call_tool(tool, arguments),
      timeout=COMMAND_DEADLINE,
      closed_process='Playwright MCP',
    )
  except TimeoutError:
    raise DaemonError(f'Playwright MCP command {tool!r} exceeded {COMMAND_DEADLINE:.0f}s') from None
  try:
    files = await collect_files(before)
  except DaemonError as error:
    return {'error': str(error)}
  text = _content_text(result)
  if result.isError:
    return {'error': text}
  return {'text': text, 'files': files}


async def _receive(client: Client) -> Message:
  message = await asyncio.to_thread(client.receive, None)
  if message is None:
    raise DaemonError('broker channel closed')
  return message


def _question_id(message: Message, mission: str) -> str:
  if message.type != Tag.MESSAGE or message.request_id != mission or message.id is None:
    raise DaemonError(f'unexpected broker message while serving commands: {message.type}')
  return message.id


async def _warm_browser(session: ClientSession, supervisor: ProcessSupervisor) -> None:
  try:
    result = await supervisor.run(
      session.call_tool('browser_navigate', {'url': 'about:blank'}),
      timeout=COMMAND_DEADLINE,
      closed_process='Playwright MCP',
    )
  except TimeoutError:
    raise DaemonError(f'Playwright MCP browser warm-up exceeded {COMMAND_DEADLINE:.0f}s') from None
  if result.isError:
    raise DaemonError(f'Playwright MCP browser warm-up failed: {_content_text(result)}')


async def _live_tools(
  session: ClientSession, supervisor: ProcessSupervisor
) -> Sequence[mcp_types.Tool]:
  try:
    result = await supervisor.run(
      session.list_tools(),
      timeout=COMMAND_DEADLINE,
      closed_process='Playwright MCP',
    )
  except TimeoutError:
    raise DaemonError(f'Playwright MCP tool listing exceeded {COMMAND_DEADLINE:.0f}s') from None
  return result.tools


async def _command_loop(
  client: Client,
  mission: str,
  session: ClientSession,
  supervisor: ProcessSupervisor,
) -> None:
  commands = 0
  while True:
    message = await supervisor.run(_receive(client))
    question = _question_id(message, mission)
    payload = message.payload
    if payload == {'webview': 'tools'}:
      reply = {'text': _tools_text(await _live_tools(session, supervisor)), 'files': []}
    elif payload == {'webview': 'close'}:
      client.message(mission, {'closed': True}, reply_to=question)
      client.result(mission, {'outcome': 'ok', 'value': {'commands': commands}})
      return
    elif 'webview' in payload:
      reply = {'error': "webview command must be {'webview': 'tools' | 'close'}"}
    else:
      reply = await _tool_reply(session, supervisor, payload)
      if 'text' in reply:
        commands += 1
    client.message(mission, await bound_reply(reply), reply_to=question)


async def serve() -> None:
  raw_options = os.environ.get(OPTIONS_ENV)
  if raw_options is None:
    raise DaemonError(f'{OPTIONS_ENV} is unset')
  options = decode_options(raw_options)
  _require_artifact_view()
  mission = os.environ.get(BROKER_MISSION)
  if not mission:
    raise DaemonError(f'{BROKER_MISSION} is unset')

  with ExitStack() as process_stack:
    xvfb, display = process_stack.enter_context(_xvfb())
    sync_processes: list[tuple[str, subprocess.Popen]] = [('Xvfb', xvfb)]
    vnc_url = None
    if options.vnc:
      vnc_processes = process_stack.enter_context(_vnc(display))
      sync_processes.extend(vnc_processes)
      host_port = _published_port(VNC_PORT, os.environ.get(PUBLISHED_PORTS_ENV, ''))
      vnc_url = f'http://127.0.0.1:{host_port}/vnc.html?autoconnect=1&resize=scale'
    config = process_stack.enter_context(_browser_config())

    async with AsyncExitStack() as async_stack:
      session, mcp_process = await async_stack.enter_async_context(
        _playwright(options, config, display)
      )
      async with _supervising([*sync_processes, ('Playwright MCP', mcp_process)]) as supervisor:
        before_warmup = await asyncio.to_thread(workspace_files)
        await _warm_browser(session, supervisor)
        await asyncio.to_thread(discard_changed_files, before_warmup)
        client = await asyncio.to_thread(Client.from_env)
        if client is None:
          raise DaemonError('no broker channel')
        with client:
          client.listen(mission)
          client.message(mission, {'event': 'ready', 'vnc': vnc_url})
          await _command_loop(client, mission, session, supervisor)
