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
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Iterator, Mapping, Sequence
from contextlib import AsyncExitStack, ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Optional, cast

import anyio
from mcp import ClientSession, types as mcp_types
from mcp.shared.exceptions import MCPError
from mcp.shared.message import SessionMessage

from bro.artifact import ArtifactError, mint_artifact
from bro.base import credentials
from bro.broker.brotocol import Message, Tag
from bro.broker.client import Client
from bro.broker.environment import BROKER_MISSION
from bro.broker.journal import MAX_MESSAGE_BYTES
from bro.webview.profile import ProfileError, decode_profile
from bro.webview.worker import vnc_url as format_vnc_url

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
# Playwright MCP names each file it writes in a result section as `- [title](absolute path)`
# and each download in the Events section as `- Downloaded file name to "absolute path"`.
LINKED_FILE_SECTIONS = frozenset({'Result', 'Snapshot'})
DOWNLOAD_FILE_SECTION = 'Events'
LINKED_FILE_PREFIX = '- ['
LINKED_FILE_PATH_OPENER = '](/'
DOWNLOADED_FILE_PREFIX = '- Downloaded file '
DOWNLOADED_FILE_PATH_OPENER = ' to "/'
_PROCESS_POLL_SECONDS = 0.05


@dataclass(frozen=True)
class Options:
  vnc: bool
  downloads: bool
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
  fields = {'vnc', 'downloads', 'allowed_origins', 'blocked_origins'}
  unknown = sorted(set(value) - fields)
  missing = sorted(fields - set(value))
  if len(unknown) > 0 or len(missing) > 0:
    details = []
    if len(unknown) > 0:
      details.append(f'unknown field(s): {", ".join(unknown)}')
    if len(missing) > 0:
      details.append(f'missing field(s): {", ".join(missing)}')
    raise DaemonError(f'{OPTIONS_ENV} {"; ".join(details)}')
  flags = {}
  for flag in ('vnc', 'downloads'):
    if not isinstance(value[flag], bool):
      raise DaemonError(f"{OPTIONS_ENV} field '{flag}' must be a boolean")
    flags[flag] = value[flag]
  return Options(
    vnc=flags['vnc'],
    downloads=flags['downloads'],
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
  process = subprocess.Popen(tuple(command), stdout=sys.stderr)
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
def _browser_config(
  storage_state: dict[str, Any] | None, *, downloads: bool
) -> Iterator[tuple[Path, Path | None]]:
  with tempfile.TemporaryDirectory(prefix='bro-browser-') as directory:
    root = Path(directory)
    config = root / 'playwright.json'
    config.write_text(json.dumps({'browser': {'contextOptions': {'acceptDownloads': downloads}}}))
    storage_path = None
    if storage_state is not None:
      storage_path = root / 'storage-state.json'
      descriptor = os.open(storage_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
      with os.fdopen(descriptor, 'w') as storage_file:
        json.dump(storage_state, storage_file, ensure_ascii=False, separators=(',', ':'))
    yield config, storage_path


def _playwright_arguments(
  options: Options,
  config: Path,
  storage_state: Path | None,
  output_directory: Path | None = None,
) -> list[str]:
  selected_output_directory = OUTPUT_DIRECTORY if output_directory is None else output_directory
  arguments = ['--isolated']
  if storage_state is not None:
    arguments += ['--storage-state', str(storage_state)]
  arguments += [
    '--no-sandbox',
    '--browser',
    'chromium',
    '--output-dir',
    str(selected_output_directory),
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
          message = mcp_types.jsonrpc_message_adapter.validate_json(line)
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
      raise anyio.ClosedResourceError
    encoded = session_message.message.model_dump_json(by_alias=True, exclude_none=True).encode()

    def write() -> None:
      self._stdin.write(encoded + b'\n')
      self._stdin.flush()

    try:
      await asyncio.to_thread(write)
    except BrokenPipeError as error:
      raise anyio.BrokenResourceError from error

  async def __aenter__(self):
    return self

  async def __aexit__(self, *args) -> None:
    await self.aclose()

  async def aclose(self) -> None:
    if self._closed:
      return
    self._closed = True
    # close releases the pipe even when an exited reader leaves buffered bytes
    # undeliverable; the supervisor reports that exit
    with contextlib.suppress(BrokenPipeError):
      await asyncio.to_thread(self._stdin.close)


async def _await_task(task: asyncio.Task[Any]) -> None:
  await task


@contextlib.asynccontextmanager
async def _playwright(
  options: Options,
  config: Path,
  storage_state: Path | None,
  display: str,
  *,
  workspace: Path | None = None,
  output_directory: Path | None = None,
) -> AsyncIterator[tuple[ClientSession, subprocess.Popen]]:
  selected_workspace = WORKSPACE if workspace is None else workspace
  async with AsyncExitStack() as stack:
    process = subprocess.Popen(
      (
        PLAYWRIGHT_COMMAND,
        *_playwright_arguments(options, config, storage_state, output_directory),
      ),
      stdin=subprocess.PIPE,
      stdout=subprocess.PIPE,
      cwd=selected_workspace,
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
      with contextlib.suppress(asyncio.CancelledError, MCPError):
        await operation
      raise self._exit_error(exited)
    if operation not in done:
      operation.cancel()
      with contextlib.suppress(asyncio.CancelledError):
        await operation
      raise TimeoutError
    try:
      return operation.result()
    except MCPError as error:
      if closed_process is None or error.code != mcp_types.CONNECTION_CLOSED:
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


def _linked_file(line: str) -> str | None:
  if not (line.startswith(LINKED_FILE_PREFIX) and line.endswith(')')):
    return None
  opener = line.find(LINKED_FILE_PATH_OPENER)
  return None if opener < 0 else line[opener + len(LINKED_FILE_PATH_OPENER) - 1 : -1]


def _downloaded_file(line: str) -> str | None:
  if not (line.startswith(DOWNLOADED_FILE_PREFIX) and line.endswith('"')):
    return None
  opener = line.rfind(DOWNLOADED_FILE_PATH_OPENER)
  return None if opener < 0 else line[opener + len(DOWNLOADED_FILE_PATH_OPENER) - 1 : -1]


@dataclass(frozen=True)
class NamedFiles:
  """Files a result text names: those the command wrote, and downloads that finished."""

  written: list[Path]
  downloaded: list[Path]


def named_files(text: str) -> NamedFiles:
  """Read a Playwright MCP result text for the files it names, in order and without repeats.

  Result text includes page-controlled lines, so every line is read in linear time.
  """
  written: dict[Path, None] = {}
  downloaded: dict[Path, None] = {}
  section = ''
  for line in text.splitlines():
    if line.startswith('### '):
      section = line[len('### ') :]
    elif section in LINKED_FILE_SECTIONS:
      if (path := _linked_file(line)) is not None:
        written[Path(path)] = None
    elif section == DOWNLOAD_FILE_SECTION:
      if (path := _downloaded_file(line)) is not None:
        downloaded[Path(path)] = None
  return NamedFiles(list(written), list(downloaded))


def _workspace_relative(path: Path) -> Path | None:
  try:
    return path.relative_to(WORKSPACE)
  except ValueError:
    return None


def _is_output(relative: Path) -> bool:
  return OUTPUT_DIRECTORY.is_relative_to(WORKSPACE) and relative.is_relative_to(
    OUTPUT_DIRECTORY.relative_to(WORKSPACE)
  )


async def collect_files(before: Mapping[Path, FileState], text: str) -> list[dict[str, Any]]:
  """Mint the files `text` names and remove the files the command wrote outside the output directory.

  A file the command wrote must be named by `text`.
  A finished download is named by the first reply after it finishes, which may be a later command's,
  so it is minted from the output directory whenever it exists.
  Files left unnamed in the output directory stay there, because a download that finished during
  the command may be named by the next reply, and count with the command's own files toward the
  output limit.
  """
  after = await asyncio.to_thread(workspace_files)
  changed = _changed_files(before, after)
  held = {path for path in after if _is_output(path)} | set(changed)
  total = sum(after[path].size for path in held)
  if total > OUTPUT_LIMIT_BYTES:
    for path in held:
      _remove_workspace_file(path)
    raise DaemonError(
      f'command output holds {total} bytes, exceeding the {OUTPUT_LIMIT_BYTES}-byte output limit'
    )
  named = named_files(text)
  written = set(changed)
  collected: list[dict[str, Any]] = []
  minted: set[Path] = set()
  for path in named.written:
    relative = _workspace_relative(path)
    if relative is None or relative not in written:
      collected.append({'name': str(path), 'error': 'named file was not written by the command'})
      continue
    minted.add(relative)
    with _removing_workspace_file(relative):
      collected.append(await _minted_file(relative, after[relative]))
  for path in named.downloaded:
    relative = _workspace_relative(path)
    if relative is None or not _is_output(relative) or relative not in after:
      collected.append(
        {'name': str(path), 'error': 'named download is not in the output directory'}
      )
      continue
    if relative in minted:
      continue
    minted.add(relative)
    with _removing_workspace_file(relative):
      collected.append(await _minted_file(relative, after[relative]))
  for path in changed:
    if path not in minted and not _is_output(path):
      _remove_workspace_file(path)
  return collected


async def _minted_file(relative: Path, state: FileState) -> dict[str, Any]:
  if state.kind != 'file':
    return {'name': str(relative), 'error': f'unsupported {state.kind} output'}
  try:
    minted = await asyncio.to_thread(mint_artifact, str(relative))
  except (ArtifactError, OSError, ValueError) as error:
    return {'name': str(relative), 'error': str(error)}
  return {'name': str(relative), 'ref': minted.ref}


def _content_text(result: mcp_types.CallToolResult) -> str:
  return '\n'.join(item.text for item in result.content if isinstance(item, mcp_types.TextContent))


def _tools_text(tools: Sequence[mcp_types.Tool]) -> str:
  return json.dumps(
    [
      {
        'name': tool.name,
        'description': tool.description,
        'inputSchema': tool.input_schema,
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
    minted = await asyncio.to_thread(mint_artifact, path.name, name=f'reply{suffix}')
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
  text = _content_text(result)
  try:
    files = await collect_files(before, text)
  except DaemonError as error:
    return {'error': str(error)}
  if result.is_error:
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
  if result.is_error:
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


def _profile_storage_state() -> dict[str, Any] | None:
  raw = credentials.try_get('cookies')
  if raw is None:
    return None
  try:
    return decode_profile(raw)
  except ProfileError as error:
    raise DaemonError(f'cookies profile is invalid: {error}') from error


async def serve() -> None:
  raw_options = os.environ.get(OPTIONS_ENV)
  if raw_options is None:
    raise DaemonError(f'{OPTIONS_ENV} is unset')
  options = decode_options(raw_options)
  storage_state = _profile_storage_state()
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
      vnc_url = format_vnc_url(host_port)
    config, storage_path = process_stack.enter_context(
      _browser_config(storage_state, downloads=options.downloads)
    )

    async with AsyncExitStack() as async_stack:
      session, mcp_process = await async_stack.enter_async_context(
        _playwright(options, config, storage_path, display)
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
