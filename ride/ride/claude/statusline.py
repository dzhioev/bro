"""Session-local projector for Claude Code's statusLine file."""

import contextlib
import fcntl
import os
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from bro.base import log, spawn
from bro.broker.client import Client
from bro.monitor import SESSION_DIR_ENV, harness_session_dir, health

__cli_name__ = 'ride.claude.statusline'

# Claude drops a shorter statusLine refresh, and the projector follows the same cadence.
REFRESH_SECONDS = 1
_QUERY_TIMEOUT = 0.25
_START_TIMEOUT = 5.0
_STOP_TIMEOUT = 5.0
_OUTPUT_NAME = 'statusline'
_PID_NAME = 'statusline.pid'
_LOG_NAME = 'statusline.log'
_LOCK_NAME = 'statusline.lock'
_LAST_OUTCOME_TTL = 900.0

_RED = '\033[1;31m'
_YELLOW = '\033[1;33m'
_GREEN = '\033[1;32m'
_DIM = '\033[2m'
_RESET = '\033[0m'


def statusline_command() -> str:
  directory = f'"${SESSION_DIR_ENV}/claude"'
  return (
    f'pid=$(cat {directory}/{_PID_NAME} 2>/dev/null) '
    f'&& kill -0 "$pid" 2>/dev/null '
    f'&& cat {directory}/{_OUTPUT_NAME} 2>/dev/null || true'
  )


def _paths() -> tuple[Path, Path, Path, Path]:
  state = harness_session_dir('claude')
  if state is None:
    raise RuntimeError(f'{SESSION_DIR_ENV} is unset: this session keeps no state dir')
  return state / _OUTPUT_NAME, state / _PID_NAME, state / _LOG_NAME, state / _LOCK_NAME


def _age(seconds: float) -> str:
  seconds = max(0, int(seconds))
  if seconds < 60:
    return f'{seconds}s'
  if seconds < 3600:
    return f'{seconds // 60}m'
  return f'{seconds // 3600}h{(seconds % 3600) // 60:02d}m'


def _trail(trail_id: Optional[str]) -> str:
  if trail_id is None:
    return 'no trail yet'
  return f'trail {trail_id}'


def _query_summons() -> list[dict[str, Any]]:
  if os.environ.get('BROKER_CHANNEL') is None:
    return []
  client = Client.from_env()
  if client is None:
    return []
  with client:
    result = client.call('query', {}, _QUERY_TIMEOUT)
  payload = result.payload
  if payload.get('outcome') != 'ok':
    return []
  value = payload.get('value')
  quests = value.get('quests') if isinstance(value, dict) else None
  if not isinstance(quests, list):
    return []
  return [quest for quest in quests if isinstance(quest, dict) and quest.get('kind') == 'summon']


def _timestamp(value: object) -> Optional[float]:
  if not isinstance(value, str):
    return None
  return datetime.fromisoformat(value).timestamp()


def _target(quest: dict[str, Any]) -> str:
  args = quest.get('args')
  target = args.get('target') if isinstance(args, dict) else None
  return target if isinstance(target, str) else 'unknown target'


def _summon_parts(now: float) -> list[str]:
  try:
    quests = _query_summons()
    parts = []
    for quest in quests:
      state = quest.get('state')
      if state not in ('accepted', 'started'):
        continue
      started_at = _timestamp(quest.get('started_at')) or _timestamp(quest.get('accepted_at'))
      if started_at is None:
        return []
      age = _age(now - started_at)
      args = quest.get('args')
      manual = isinstance(args, dict) and args.get('manual') is True
      trail_id = quest.get('trail_id')
      if manual and trail_id is None:
        parts.append(f'{_YELLOW}⚡ awaiting manual {_target(quest)} launch {age}{_RESET}')
      else:
        parts.append(
          f'{_YELLOW}⚡ summoning {_target(quest)} {age} '
          f'({_trail(trail_id if isinstance(trail_id, str) else None)}){_RESET}'
        )
    terminal = next(
      (quest for quest in quests if quest.get('state') in ('ended', 'denied')),
      None,
    )
    if terminal is not None:
      ended_at = _timestamp(terminal.get('ended_at'))
      if ended_at is None:
        return []
      if now - ended_at < _LAST_OUTCOME_TTL:
        outcome = terminal.get('outcome')
        reason = terminal.get('reason')
        rendered = str(outcome)
        if outcome == 'failed' and reason is not None:
          rendered += f':{reason}'
        color = _GREEN if outcome == 'ok' else _RED
        mark = '✓' if outcome == 'ok' else '✗'
        parts.append(f'{color}{mark} summon {_target(terminal)}: {rendered}{_RESET}')
    return parts
  except Exception:
    return []


def render_statusline(now: Optional[float] = None) -> str:
  parts = []
  if os.environ.get('RIDE_WORKSPACE') is not None and os.environ.get('RIDE_REPO') is None:
    parts.append(f'{_DIM}no repository attached{_RESET}')
  problem = health.problem()
  if problem is not None:
    parts.append(f'{_RED}⚠ session recording {problem}{_RESET}')
  parts.extend(_summon_parts(time.time() if now is None else now))
  return ' · '.join(parts)


def _write(path: Path, content: str) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  temporary = path.with_name(path.name + '.tmp')
  temporary.write_text(content + ('\n' if len(content) > 0 else ''))
  temporary.replace(path)


def _close_fd(file_descriptor: int) -> None:
  with contextlib.suppress(OSError):
    os.close(file_descriptor)


@contextlib.contextmanager
def _projection_lifetime():
  output_path, pid_path, _, lock_path = _paths()
  lock_path.parent.mkdir(parents=True, exist_ok=True)
  with open(lock_path, 'a+') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    _write(pid_path, str(os.getpid()))
    try:
      yield output_path
    finally:
      _clear_owned_paths(output_path, pid_path, os.getpid())


def _exit_with_parent(liveness_fd: int) -> None:
  """Exit even if rendering is blocked; releasing the lock gates the next projector."""
  while len(os.read(liveness_fd, 1)) > 0:
    pass
  os._exit(0)


def project_statusline(liveness_fd: int) -> None:
  threading.Thread(target=_exit_with_parent, args=(liveness_fd,), daemon=True).start()
  with _projection_lifetime() as output_path:
    while True:
      _write(output_path, render_statusline())
      time.sleep(REFRESH_SECONDS)


@dataclass
class StatuslineProjector:
  process: subprocess.Popen
  output_path: Path
  pid_path: Path
  liveness_fd: int
  monitor: threading.Thread

  def stop(self) -> None:
    if self.process.poll() is None:
      self.process.terminate()
    self.monitor.join(timeout=_STOP_TIMEOUT)
    if self.monitor.is_alive():
      self.process.kill()
      self.monitor.join()
    _clear_owned_paths(self.output_path, self.pid_path, self.process.pid)


def _monitor_projector(
  process: subprocess.Popen,
  output_path: Path,
  pid_path: Path,
  liveness_fd: int,
) -> StatuslineProjector:
  def monitor() -> None:
    process.wait()
    _close_fd(liveness_fd)
    _clear_owned_paths(output_path, pid_path, process.pid)

  thread = threading.Thread(target=monitor, daemon=True)
  thread.start()
  return StatuslineProjector(process, output_path, pid_path, liveness_fd, thread)


def _wait_for_projector_lock(lock_path: Path) -> None:
  deadline = time.monotonic() + _START_TIMEOUT
  lock_path.parent.mkdir(parents=True, exist_ok=True)
  with open(lock_path, 'a+') as lock:
    while True:
      try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
      except BlockingIOError:
        if time.monotonic() >= deadline:
          raise RuntimeError('the previous statusLine projector did not retire') from None
        time.sleep(0.05)


def start_statusline_projector(env: Mapping[str, str]) -> StatuslineProjector:
  output_path, pid_path, log_path, lock_path = _paths()
  _wait_for_projector_lock(lock_path)
  _clear_paths(output_path, pid_path)
  log_path.parent.mkdir(parents=True, exist_ok=True)
  with contextlib.ExitStack() as startup:
    liveness_read_fd, liveness_write_fd = os.pipe()
    startup.callback(_close_fd, liveness_write_fd)
    startup.callback(_close_fd, liveness_read_fd)
    try:
      with open(log_path, 'a') as log_file:
        process = spawn.popen(
          [
            spawn.console_script(__cli_name__),
            '--liveness-fd',
            str(liveness_read_fd),
          ],
          env=dict(env),
          stdout=log_file,
          stderr=subprocess.STDOUT,
          pass_fds=(liveness_read_fd,),
        )
    except OSError as error:
      raise RuntimeError(f'cannot start the statusLine projector: {error}') from error
    startup.callback(_clear_paths, output_path, pid_path)
    startup.callback(_stop_process, process)
    _close_fd(liveness_read_fd)
    deadline = time.monotonic() + _START_TIMEOUT
    while time.monotonic() < deadline:
      if process.poll() is not None:
        raise RuntimeError(f'statusLine projector exited during startup (log: {log_path})')
      try:
        ready = pid_path.read_text().strip() == str(process.pid) and output_path.is_file()
      except OSError:
        ready = False
      if ready:
        projector = _monitor_projector(
          process,
          output_path,
          pid_path,
          liveness_write_fd,
        )
        startup.pop_all()
        log.verbose('statusLine projector started (log: %s)', log_path)
        return projector
      time.sleep(0.05)
  raise RuntimeError(
    f'statusLine projector not ready within {_START_TIMEOUT:.0f}s (log: {log_path})'
  )


def _stop_process(process: subprocess.Popen) -> None:
  if process.poll() is not None:
    return
  process.terminate()
  try:
    process.wait(timeout=_STOP_TIMEOUT)
  except subprocess.TimeoutExpired:
    process.kill()
    process.wait()


def _clear_paths(*paths: Path) -> None:
  for path in paths:
    with contextlib.suppress(FileNotFoundError):
      path.unlink()


def _clear_owned_paths(output_path: Path, pid_path: Path, process_id: int) -> None:
  try:
    owns_projection = pid_path.read_text().strip() == str(process_id)
  except OSError:
    owns_projection = False
  if owns_projection:
    _clear_paths(output_path, pid_path)


def main(argv: list[str]) -> Optional[int]:
  if len(argv) != 3 or argv[1] != '--liveness-fd':
    log.error('usage: %s --liveness-fd FD', __cli_name__)
    return 2
  try:
    liveness_fd = int(argv[2])
  except ValueError:
    log.error('liveness fd must be an integer')
    return 2
  if liveness_fd < 0:
    log.error('liveness fd must be non-negative')
    return 2
  project_statusline(liveness_fd)
  return 0
