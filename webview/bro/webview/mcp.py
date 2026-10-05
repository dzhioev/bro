"""Typed owner tools for webview missions."""

from __future__ import annotations

import codecs
import json
import math
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Optional

from pydantic import Field

from bro import mission
from bro.artifact import get_artifact, is_ref, mint_artifact
from bro.llm.mcp import Context
from bro.mcp import Toolset
from bro.webview.cli import OpenedWebview, close_webview, open_webview

INLINE_REPLY_BYTES = 4 * 1024
_WEBVIEW_FIELD = Field(description='mission id returned by webview::open or webview::reopen')
_REF_FIELD = Field(description='artifact ref to share with the webview')
_PAGE_URL = re.compile(r'(?m)^- Page URL: (\S.*)$')


@dataclass(frozen=True)
class OpenOptions:
  cookies: Optional[str]
  vnc: bool
  allowed_origins: tuple[str, ...]
  blocked_origins: tuple[str, ...]


@dataclass
class WebviewRecord:
  options: OpenOptions
  shares: list[str]
  last_url: Optional[str] = None


@dataclass(frozen=True)
class OpenResult:
  webview: str
  vnc: Optional[str]


class Webviews:
  def __init__(self) -> None:
    self._records: dict[str, WebviewRecord] = {}

  def add(self, opened: OpenedWebview, record: WebviewRecord) -> OpenResult:
    if opened.mission_id in self._records:
      raise RuntimeError(f'duplicate opened webview mission id: {opened.mission_id}')
    self._records[opened.mission_id] = record
    return OpenResult(opened.mission_id, opened.vnc)

  def get(self, webview: str) -> WebviewRecord:
    record = self._records.get(webview)
    if record is None:
      raise ValueError(f'webview {webview!r} was not opened by this toolset')
    return record

  def remove(self, webview: str, record: WebviewRecord) -> None:
    if self._records.get(webview) is not record:
      raise RuntimeError(f'webview record changed while removing {webview}')
    del self._records[webview]


toolset = Toolset[Webviews]('webview', state=Webviews)


def _launch(record: WebviewRecord) -> OpenedWebview:
  options = record.options
  return open_webview(
    cookies=options.cookies,
    vnc=options.vnc,
    allowed_origins=list(options.allowed_origins),
    blocked_origins=list(options.blocked_origins),
    share=list(record.shares),
  )


@toolset.tool(
  'open a browser webview and wait until it is ready. cookies names an available browser '
  'profile; vnc requests a human-visible noVNC URL; allow and block are advisory origin lists; '
  'share exposes artifact refs from startup.'
)
def open(
  context: Context[Webviews],
  cookies: Annotated[Optional[str], Field(description='cookies profile instance')] = None,
  vnc: Annotated[bool, Field(description='publish a noVNC view')] = False,
  allow: Annotated[Optional[list[str]], Field(description='advisory allowed origins')] = None,
  block: Annotated[Optional[list[str]], Field(description='advisory blocked origins')] = None,
  share: Annotated[
    Optional[list[str]], Field(description='artifact refs to expose from startup')
  ] = None,
) -> OpenResult:
  options = OpenOptions(
    cookies=cookies,
    vnc=vnc,
    allowed_origins=tuple(allow or ()),
    blocked_origins=tuple(block or ()),
  )
  record = WebviewRecord(options, list(share or ()))
  opened = _launch(record)
  return context.state.add(opened, record)


def _mission_answer(webview: str, payload: dict[str, Any]) -> dict[str, Any]:
  try:
    asked = mission.ask(webview, payload, wait=math.inf)
  except mission.MissionError as error:
    try:
      outcome = mission.check(webview)
    except mission.MissionError:
      raise error
    if outcome.result is None:
      raise error
    result = outcome.result
    result_outcome = result.get('outcome')
    if not isinstance(result_outcome, str) or len(result_outcome) == 0:
      raise ValueError(f'webview {webview} ended with a malformed outcome: {result}') from error
    detail = result.get('error') if 'error' in result else result.get('value')
    if detail is None:
      raise ValueError(f'webview {webview} ended without outcome detail: {result}') from error
    raise ValueError(f'webview {webview} ended {result_outcome}: {detail}') from error
  if asked.counter_question_id is not None:
    raise ValueError(f'webview {webview} replied with an unexpected counter-question')
  if asked.answer is None:
    raise ValueError(f'webview {webview} returned no command reply')
  return asked.answer


def command_reply(
  webview: str, tool: str, arguments: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
  """Call one Playwright tool and return its unspilled worker reply."""
  payload: dict[str, Any] = {'tool': tool}
  if arguments is not None:
    payload['arguments'] = arguments
  return _spilled_reply(_mission_answer(webview, payload))


def _artifact_path(ref: str) -> Path:
  return Path(get_artifact(ref))


def _spilled_reply(payload: dict[str, Any]) -> dict[str, Any]:
  if payload.get('spilled') != 'reply':
    return payload
  ref = payload.get('ref')
  if not is_ref(ref):
    raise ValueError(f'webview returned an invalid whole-reply spill: {payload}')
  try:
    value = json.loads(_artifact_path(str(ref)).read_text())
  except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
    raise ValueError(f'webview whole-reply spill {ref} is not valid JSON: {error}') from error
  if not isinstance(value, dict):
    raise ValueError(f'webview whole-reply spill {ref} is not an object')
  return value


def _files(payload: dict[str, Any]) -> list[dict[str, str]]:
  files = payload.get('files', [])
  if not isinstance(files, list) or not all(isinstance(file, dict) for file in files):
    raise ValueError(f'webview returned malformed files: {files!r}')
  validated: list[dict[str, str]] = []
  for file in files:
    name = file.get('name')
    ref = file.get('ref')
    error = file.get('error')
    if not isinstance(name, str) or len(name) == 0:
      raise ValueError(f'webview returned a malformed file: {file!r}')
    if is_ref(ref) and error is None:
      validated.append({'name': name, 'ref': str(ref)})
    elif isinstance(error, str) and len(error) > 0 and ref is None:
      validated.append({'name': name, 'error': error})
    else:
      raise ValueError(f'webview returned a malformed file: {file!r}')
  return validated


@contextmanager
def _temporary_reply(content: bytes) -> Iterator[Path]:
  with tempfile.NamedTemporaryFile(
    dir='.', prefix='.webview-reply-', suffix='.txt', delete=False
  ) as file:
    file.write(content)
    path = Path(file.name)
  try:
    yield path
  finally:
    path.unlink(missing_ok=True)


def _mint_text(text: str) -> str:
  with _temporary_reply(text.encode()) as path:
    return mint_artifact(path.name).ref


def _head(text: str, maximum_bytes: int) -> str:
  return text.encode()[:maximum_bytes].decode(errors='ignore')


def _decode_artifact_head(content: bytes, complete: bool, ref: str) -> str:
  decoder = codecs.getincrementaldecoder('utf-8')()
  try:
    return decoder.decode(content, final=complete)
  except UnicodeDecodeError as error:
    raise ValueError(f'webview text spill {ref} is not UTF-8') from error


def _cut(text: str, ref: str, size: int) -> str:
  marker = f'\n[...cut: whole {size:,}-byte reply at {ref}]'
  head_bytes = max(0, INLINE_REPLY_BYTES - len(marker.encode()))
  return _head(text, head_bytes) + marker


def _text(payload: dict[str, Any]) -> tuple[str, Optional[str], int]:
  if payload.get('spilled') == 'text':
    ref = payload.get('ref')
    if not is_ref(ref):
      raise ValueError(f'webview returned an invalid text spill: {payload}')
    path = _artifact_path(str(ref))
    declared_size = payload.get('bytes')
    if not isinstance(declared_size, int) or isinstance(declared_size, bool) or declared_size < 0:
      raise ValueError(f'webview returned an invalid text spill size: {declared_size!r}')
    try:
      if path.stat().st_size != declared_size:
        raise ValueError(
          f'webview text spill {ref} size does not match its declared {declared_size} bytes'
        )
      with path.open('rb') as content:
        head = content.read(INLINE_REPLY_BYTES)
    except OSError as error:
      raise ValueError(f'could not read webview text spill {ref}: {error}') from error
    text = _decode_artifact_head(head, declared_size <= len(head), str(ref))
    if declared_size <= INLINE_REPLY_BYTES:
      return text, None, declared_size
    return text, str(ref), declared_size
  text = payload.get('text')
  if not isinstance(text, str):
    raise ValueError(f'webview returned no text: {payload}')
  size = len(text.encode())
  if size <= INLINE_REPLY_BYTES:
    return text, None, size
  return text, _mint_text(text), size


def _render_reply(payload: dict[str, Any]) -> tuple[str, str]:
  payload = _spilled_reply(payload)
  error = payload.get('error')
  if error is not None:
    if not isinstance(error, str) or len(error) == 0:
      raise ValueError(f'webview returned a malformed error: {error!r}')
    raise ValueError(error)
  text, ref, size = _text(payload)
  rendered = text if ref is None else _cut(text, ref, size)
  files = _files(payload)
  if files:
    entries = []
    for file in files:
      value = file['ref'] if 'ref' in file else f'error: {file["error"]}'
      entries.append(f'- {file["name"]}: {value}')
    rendered += '\n\nFiles:\n' + '\n'.join(entries)
  return rendered, text


@toolset.tool(
  'call one Playwright tool in a live webview. Returns bounded reply text followed by every '
  'written file and its artifact ref; a larger reply names the ref containing its whole text.'
)
def command(
  context: Context[Webviews],
  webview: Annotated[str, _WEBVIEW_FIELD],
  tool: Annotated[str, Field(description='Playwright tool name')],
  arguments: Annotated[
    Optional[dict[str, Any]], Field(description='Playwright tool arguments')
  ] = None,
) -> str:
  record = context.state.get(webview)
  rendered, text = _render_reply(command_reply(webview, tool, arguments))
  urls = _PAGE_URL.findall(text)
  if urls:
    record.last_url = urls[-1]
  return rendered


def _schema_type(schema: object) -> str:
  if not isinstance(schema, dict):
    return 'any'
  enum = schema.get('enum')
  if isinstance(enum, list) and len(enum) > 0:
    return '|'.join(json.dumps(value, ensure_ascii=False) for value in enum)
  alternatives = schema.get('anyOf')
  if isinstance(alternatives, list):
    values = [_schema_type(value) for value in alternatives]
    return '|'.join(dict.fromkeys(values))
  kind = schema.get('type')
  if kind == 'array':
    return f'{_schema_type(schema.get("items"))}[]'
  if isinstance(kind, str):
    return kind
  return 'any'


def _first_sentence(description: object) -> str:
  if not isinstance(description, str):
    return ''
  sentence = re.split(r'(?<=[.!?])\s+', description.strip(), maxsplit=1)[0]
  return sentence


def _render_tools(text: str) -> str:
  try:
    roster = json.loads(text)
  except json.JSONDecodeError as error:
    raise ValueError(f'webview tool roster is not valid JSON: {error}') from error
  if not isinstance(roster, list) or not all(isinstance(entry, dict) for entry in roster):
    raise ValueError('webview tool roster is not a list of objects')
  lines: list[str] = []
  for entry in roster:
    name = entry.get('name')
    schema = entry.get('inputSchema')
    if not isinstance(name, str) or not isinstance(schema, dict):
      raise ValueError(f'webview tool roster entry is malformed: {entry!r}')
    properties = schema.get('properties', {})
    required = schema.get('required', [])
    if (
      not isinstance(properties, dict)
      or not isinstance(required, list)
      or not all(isinstance(value, str) for value in required)
    ):
      raise ValueError(f'webview tool parameters are malformed for {name!r}')
    parameters = ', '.join(
      f'{parameter}{"" if parameter in required else "?"}: {_schema_type(parameter_schema)}'
      for parameter, parameter_schema in properties.items()
    )
    sentence = _first_sentence(entry.get('description'))
    suffix = '' if len(sentence) == 0 else f' — {sentence}'
    lines.append(f'{name}({parameters}){suffix}')
  return '\n'.join(lines)


@toolset.tool(
  'list the live Playwright tools compactly as each tool name, its parameters, and the first '
  'sentence of its description.'
)
def tools(context: Context[Webviews], webview: Annotated[str, _WEBVIEW_FIELD]) -> str:
  context.state.get(webview)
  payload = _spilled_reply(_mission_answer(webview, {'webview': 'tools'}))
  error = payload.get('error')
  if error is not None:
    if not isinstance(error, str) or len(error) == 0:
      raise ValueError(f'webview returned a malformed error: {error!r}')
    raise ValueError(error)
  if payload.get('spilled') == 'text':
    ref = payload.get('ref')
    if not is_ref(ref):
      raise ValueError(f'webview returned an invalid tool-roster spill: {payload}')
    try:
      text = _artifact_path(str(ref)).read_text()
    except (OSError, UnicodeDecodeError) as read_error:
      raise ValueError(
        f'could not read webview tool-roster spill {ref}: {read_error}'
      ) from read_error
  else:
    text = payload.get('text')
    if not isinstance(text, str):
      raise ValueError(f'webview returned no tool roster: {payload}')
  return _render_tools(text)


@toolset.tool(
  'share an artifact with a live webview. Returns the read-only path to pass to '
  'browser_file_upload and, for a directory artifact, its relative entries.'
)
def share(
  context: Context[Webviews],
  webview: Annotated[str, _WEBVIEW_FIELD],
  ref: Annotated[str, _REF_FIELD],
) -> dict[str, Any]:
  record = context.state.get(webview)
  mission.share(webview, ref)
  record.shares.append(ref)
  path = _artifact_path(ref)
  result: dict[str, Any] = {'path': f'/workspace/artifacts/{ref}'}
  if path.is_dir():
    entries = []
    for entry in sorted(path.rglob('*')):
      relative = entry.relative_to(path).as_posix()
      entries.append(relative + ('/' if entry.is_dir() else ''))
    result['entries'] = entries
  elif not path.is_file():
    raise ValueError(f'artifact {ref} is neither a file nor a directory')
  return result


@contextmanager
def _restoring_replacement(
  state: Webviews, opened: OpenedWebview, record: WebviewRecord
) -> Iterator[OpenResult]:
  result = state.add(opened, record)
  try:
    yield result
  except BaseException as restore_error:
    try:
      mission.cancel(opened.mission_id)
    except Exception as cancel_error:
      raise RuntimeError(
        f'restoring replacement webview {opened.mission_id} failed: {restore_error}; '
        f'ending it also failed: {cancel_error}; the replacement remains available as '
        f'{opened.mission_id}'
      ) from restore_error
    state.remove(opened.mission_id, record)
    raise


@toolset.tool(
  'relaunch an ended webview with its original options and all later shares, then navigate '
  'to the last URL its replies named. Returns the replacement mission id and noVNC URL.'
)
def reopen(context: Context[Webviews], webview: Annotated[str, _WEBVIEW_FIELD]) -> OpenResult:
  record = context.state.get(webview)
  outcome = mission.check(webview)
  if outcome.result is None:
    raise ValueError(f'webview {webview} is still running')
  opened = _launch(record)
  with _restoring_replacement(context.state, opened, record) as result:
    if record.last_url is not None:
      command(
        context,
        opened.mission_id,
        'browser_navigate',
        {'url': record.last_url},
      )
    return result


@toolset.tool('close a live webview and return its terminal outcome.')
def close(context: Context[Webviews], webview: Annotated[str, _WEBVIEW_FIELD]) -> dict[str, Any]:
  context.state.get(webview)
  return close_webview(webview)
