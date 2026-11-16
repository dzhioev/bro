"""The native harness's append-only record of one run."""

import copy
import datetime
import json
import os
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional, get_args

from bro.base import configs, log
from bro.base.lulid import lulid
from bro.llm.tracker import EndReason, StepKind, ToolStepSource
from bro.trails.model import ForkedFrom, tools_sha256, validate_end
from bro.workspace.paths import runtime_base

FORMAT = 1
STEP_KINDS = frozenset(get_args(StepKind))


def private_directory(path: Path) -> None:
  if path.is_dir():
    return
  private_directory(path.parent)
  path.mkdir(mode=0o700, exist_ok=True)


def new_path(directory: Optional[Path] = None) -> Path:
  return (runtime_base() / 'bro' / 'runs' if directory is None else directory) / f'{lulid()}.jsonl'


def _now() -> str:
  return datetime.datetime.now(datetime.UTC).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


class RunLogError(ValueError):
  pass


@dataclass(frozen=True)
class Entry:
  value: dict[str, Any]
  line: str
  step_number: Optional[int]


class _State:
  def __init__(self) -> None:
    self.opened = False
    self.ended = False
    self.steps = 0
    self.tools: set[str] = set()

  def accept(self, value: dict) -> Optional[int]:
    if self.ended:
      raise ValueError('entry after end')
    kind = value.get('kind')
    at = value.get('at')
    if not isinstance(at, str):
      raise ValueError('entry requires at')
    datetime.datetime.strptime(at, '%Y-%m-%dT%H:%M:%S.%fZ')
    if not self.opened:
      if kind != 'open':
        raise ValueError('first entry must be open')
      if type(value.get('format')) is not int or value['format'] != FORMAT:
        raise ValueError(f'unsupported run log format: {value.get("format")!r}')
      for field in ('version', 'bro', 'surface', 'hold'):
        if not isinstance(value.get(field), str) or len(value[field]) == 0:
          raise ValueError(f'open requires {field}')
      if not isinstance(value.get('recipe'), dict) or not isinstance(
        value.get('interactive'), bool
      ):
        raise ValueError('open requires recipe and interactive')
      self.opened = True
      return None
    if kind == 'open':
      raise ValueError('duplicate open')
    if kind == 'tools':
      tools = value.get('tools')
      digest = value.get('sha256')
      if (
        not isinstance(tools, list) or not isinstance(digest, str) or digest != tools_sha256(tools)
      ):
        raise ValueError('tools digest does not match its list')
      if digest in self.tools:
        raise ValueError('duplicate tools digest')
      self.tools.add(digest)
      return None
    if kind == 'end':
      if self.steps == 0:
        raise ValueError('end before system_prompt')
      validate_end(value.get('reason'), value.get('detail'))
      self.ended = True
      return None
    if kind not in STEP_KINDS:
      raise ValueError(f'unknown entry kind: {kind!r}')
    if self.steps == 0 and (
      kind != 'system_prompt'
      or type(value.get('turn_index')) is not int
      or value['turn_index'] != 0
    ):
      raise ValueError('step 0 must be system_prompt with turn_index 0')
    if 'body' not in value:
      raise ValueError('step requires body')
    if kind in ('system_prompt', 'user_input', 'notification') and not isinstance(
      value['body'], str
    ):
      raise ValueError(f'{kind} body must be text')
    if kind == 'llm_call' and value.get('tools_sha256') not in self.tools:
      raise ValueError('llm_call names an unknown tools digest')
    step = self.steps
    self.steps += 1
    return step


class RunLog:
  """A single-run tracker; a failed write makes its remaining tail unwritable."""

  current_tool_step_id: Optional[ToolStepSource] = None

  def __init__(self, path: Optional[Path] = None):
    self.path = new_path() if path is None else path
    self._state = _State()
    self._failed = False
    self._lock = threading.RLock()

  def _write(self, kind: str, **fields: Any) -> Optional[int]:
    if self._failed:
      raise RuntimeError('run log write refused after a failed write')
    if self._state.ended:
      raise RuntimeError('run log write refused after end')
    try:
      value = {**fields, 'kind': kind, 'at': _now()}
      line = (
        json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n'
      ).encode()
      step = self._state.accept(value)
      flags = os.O_WRONLY | (os.O_CREAT | os.O_EXCL if kind == 'open' else os.O_APPEND)
      with os.fdopen(os.open(self.path, flags, 0o600), 'wb') as stream:
        stream.write(line)
      return step
    except Exception:
      self._failed = True
      raise

  def start(
    self,
    bro: str,
    recipe: dict,
    system_prompt: str,
    forked_from: Optional[ForkedFrom],
    interactive: bool,
    surface: str,
    hold: str = 'unattended',
  ) -> None:
    with self._lock:
      if self._state.opened:
        raise RuntimeError('run log already opened')
      if forked_from is not None and not isinstance(forked_from, ForkedFrom):
        raise TypeError('forked_from must be a ForkedFrom')
      private_directory(self.path.parent)
      fields = (
        {}
        if forked_from is None
        else {
          'forked_from': {
            key: value for key, value in asdict(forked_from).items() if value is not None
          }
        }
      )
      self._write(
        'open',
        format=FORMAT,
        version=configs.VERSION,
        bro=bro,
        recipe=recipe,
        interactive=interactive,
        surface=surface,
        hold=hold,
        **fields,
      )
      self._write('system_prompt', body=system_prompt, turn_index=0)

  def step(self, kind: StepKind, body: Any, **extras: Any) -> int:
    with self._lock:
      if kind not in STEP_KINDS:
        raise ValueError(f'unknown step kind: {kind!r}')
      if kind == 'llm_call':
        if not isinstance(body, dict) or not isinstance(body.get('request'), dict):
          raise ValueError('llm_call body.request must be an object')
        request = body['request']
        tools = request.get('tools')
        if not isinstance(tools, list):
          raise ValueError('llm_call body.request.tools must be a list')
        digest = tools_sha256(tools)
        if digest not in self._state.tools:
          self._write('tools', sha256=digest, tools=tools)
        body = {**body, 'request': {key: value for key, value in request.items() if key != 'tools'}}
        extras = {**extras, 'tools_sha256': digest}
      step = self._write(kind, **extras, body=body)
      assert step is not None
      return step

  def end(self, reason: EndReason, detail: Optional[str] = None) -> None:
    with self._lock:
      if self._state.ended:
        return
      try:
        self._write('end', reason=reason, **({} if detail is None else {'detail': detail}))
      except Exception as exception:
        log.warning('run log end failed for %s: %s', self.path, exception)


class Reader:
  """A bounded forward reader, retaining validation state and a byte offset."""

  def __init__(self, path: Path):
    self.path = path
    self.offset = 0
    self.line_number = 0
    self._state = _State()

  def read(
    self,
    *,
    max_entries: int,
    max_bytes: int,
    size: Optional[Callable[[Entry], int]] = None,
  ) -> list[Entry]:
    """Read at least one complete entry if present, even when it exceeds the bound.

    `size` lets a consumer bound its encoded representation rather than the file's.
    """
    if max_entries <= 0 or max_bytes <= 0:
      raise ValueError('run log read bounds must be positive')
    entries: list[Entry] = []
    total = 0
    offset, line_number = self.offset, self.line_number
    state = copy.deepcopy(self._state)
    with self.path.open('rb') as stream:
      stream.seek(offset)
      while len(entries) < max_entries:
        raw = stream.readline()
        if not raw.endswith(b'\n'):
          break
        try:
          line = raw[:-1].decode('utf-8')
          value = json.loads(line)
          if not isinstance(value, dict):
            raise ValueError('entry must be an object')
          entry = Entry(value, line, None)
          count = len(raw) if size is None else size(entry)
          if len(entries) > 0 and total + count > max_bytes:
            break
          step = state.accept(value)
        except (ValueError, TypeError, KeyError) as exception:
          raise RunLogError(f'{self.path}:{line_number + 1}: {exception}') from exception
        entries.append(Entry(value, line, step))
        total += count
        offset += len(raw)
        line_number += 1
    self.offset, self.line_number, self._state = offset, line_number, state
    return entries
