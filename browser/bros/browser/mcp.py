"""Focused page reading for the browser persona."""

from __future__ import annotations

import importlib.resources
import json
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, Field

from bro.artifact import get_artifact, is_ref, mint_artifact
from bro.base import credentials
from bro.mcp import Toolset

INLINE_ANSWER_BYTES = 4 * 1024
_READER_SECRET = 'openai'
_SNAPSHOT_FILE = 'browser-look-snapshot.yml'
_TEXT_FILE = 'browser-look-text.json'
_PAGE_URL = re.compile(r'(?m)^- Page URL: (\S.*)$')
_PAGE_TITLE = re.compile(r'(?m)^- Page Title: (.*)$')
_REF_FIELD = Field(description='artifact ref containing page text or a snapshot')


class _ReaderElement(BaseModel):
  ref: str
  role: str
  name: str


class _ReaderResult(BaseModel):
  answer: str
  elements: list[_ReaderElement]


class _BrowserToolset(Toolset[None]):
  optional_secrets = (_READER_SECRET,)


toolset = _BrowserToolset('browser')


@contextmanager
def _temporary_capture(content: str) -> Iterator[Path]:
  with tempfile.NamedTemporaryFile(
    dir='.', prefix='.browser-look-', suffix='.txt', delete=False, mode='w'
  ) as file:
    file.write(content)
    path = Path(file.name)
  try:
    yield path
  finally:
    path.unlink(missing_ok=True)


def _mint_text(content: str) -> str:
  with _temporary_capture(content) as path:
    return mint_artifact(path.name).ref


def _artifact_text(ref: str) -> str:
  if not is_ref(ref):
    raise ValueError(f'invalid artifact ref: {ref!r}')
  path = Path(get_artifact(ref))
  if not path.is_file():
    raise ValueError(f'artifact {ref} is not a text file')
  try:
    return path.read_text()
  except (OSError, UnicodeDecodeError) as error:
    raise ValueError(f'artifact {ref} is not readable UTF-8 text: {error}') from error


def _reply_text(payload: dict[str, Any]) -> str:
  error = payload.get('error')
  if error is not None:
    if not isinstance(error, str) or len(error) == 0:
      raise ValueError(f'webview returned a malformed error: {error!r}')
    raise ValueError(error)
  if payload.get('spilled') == 'text':
    ref = payload.get('ref')
    if not is_ref(ref):
      raise ValueError(f'webview returned an invalid text spill: {payload}')
    return _artifact_text(str(ref))
  text = payload.get('text')
  if not isinstance(text, str):
    raise ValueError(f'webview returned no capture reply text: {payload}')
  return text


def _capture_ref(payload: dict[str, Any], filename: str) -> str:
  files = payload.get('files')
  if not isinstance(files, list) or not all(isinstance(file, dict) for file in files):
    raise ValueError(f'webview returned malformed capture files: {files!r}')
  matches = [file for file in files if file.get('name') == filename]
  if len(matches) != 1:
    raise ValueError(f'webview returned no unique {filename!r} capture: {files!r}')
  ref = matches[0].get('ref')
  if not is_ref(ref) or matches[0].get('error') is not None:
    raise ValueError(f'webview could not mint {filename!r}: {matches[0]!r}')
  return str(ref)


def _capture(
  webview: str, source: Literal['snapshot', 'text'], target: Optional[str]
) -> tuple[str, str, str, str]:
  from bro.webview.mcp import command_reply

  filename = _SNAPSHOT_FILE if source == 'snapshot' else _TEXT_FILE
  arguments: dict[str, Any] = {'filename': filename}
  if target is not None:
    arguments['target'] = target
  if source == 'text':
    text_expression = 'document.body?.innerText ?? ""' if target is None else 'element.innerText'
    function_prefix = '() =>' if target is None else '(element) =>'
    arguments['function'] = (
      f'{function_prefix} ({{text: {text_expression}, title: document.title, url: location.href}})'
    )
  tool = 'browser_snapshot' if source == 'snapshot' else 'browser_evaluate'
  payload = command_reply(webview, tool, arguments)
  reply_text = _reply_text(payload)
  capture_ref = _capture_ref(payload, filename)
  captured = _artifact_text(capture_ref)
  if source == 'text':
    try:
      decoded = json.loads(captured)
    except json.JSONDecodeError as error:
      raise ValueError(f'webview text capture {capture_ref} is not JSON: {error}') from error
    if not isinstance(decoded, dict) or set(decoded) != {'text', 'title', 'url'}:
      raise ValueError(f'webview text capture {capture_ref} is not a page text object')
    text = decoded['text']
    title = decoded['title']
    url = decoded['url']
    if not all(isinstance(value, str) for value in (text, title, url)):
      raise ValueError(f'webview text capture {capture_ref} has non-text page fields')
    captured = text
    capture_ref = _mint_text(captured)
  else:
    urls = _PAGE_URL.findall(reply_text)
    titles = _PAGE_TITLE.findall(reply_text)
    if not urls or not titles:
      raise ValueError(f'webview capture omitted the page title or URL: {reply_text}')
    title = titles[-1]
    url = urls[-1]
  if len(captured.strip()) == 0:
    raise ValueError(
      'page capture is empty; the page may be in a modal state such as an open file chooser'
    )
  return captured, capture_ref, title, url


def _reader_prompt(question: str) -> str:
  instructions = (
    importlib.resources.files('bros.browser').joinpath('reader.prompt.md').read_text().strip()
  )
  return f'{instructions}\n\nQuestion:\n{question}'


def _read_page(prompt: str, content: str) -> _ReaderResult:
  from bro.llm.mu import Text, mu

  return mu(
    prompt,
    _ReaderResult,
    Text(content),
    model='gpt-5.6-luna',
    reasoning_effort='low',
  )


def _element_present(content: str, element: _ReaderElement) -> bool:
  decoder = json.JSONDecoder()
  for line in content.splitlines():
    candidate = line.lstrip()
    if not candidate.startswith('- '):
      continue
    candidate = candidate[2:]
    role, separator, remainder = candidate.partition(' ')
    if separator == '' or role != element.role:
      continue
    try:
      name, end = decoder.raw_decode(remainder)
    except json.JSONDecodeError:
      continue
    refs = re.findall(r'\[ref=([^\]]+)\]', remainder[end:])
    if name == element.name and refs == [element.ref]:
      return True
  return False


def _cut_answer(answer: str) -> str:
  size = len(answer.encode())
  if size <= INLINE_ANSWER_BYTES:
    return answer
  marker = f'\n[...cut: {size:,}-byte reader answer]'
  head_size = max(0, INLINE_ANSWER_BYTES - len(marker.encode()))
  return answer.encode()[:head_size].decode(errors='ignore') + marker


@toolset.tool(
  'answer a focused question from a fresh page snapshot or page text, or from an existing '
  'text artifact. Element refs are returned only when they match the snapshot read.'
)
def look(
  question: Annotated[str, Field(description='focused question to answer from the page')],
  webview: Annotated[Optional[str], Field(description='live webview mission id to capture')] = None,
  source: Annotated[
    Literal['snapshot', 'text'], Field(description='page representation to capture or read')
  ] = 'snapshot',
  target: Annotated[
    Optional[str], Field(description='element ref or unique selector to capture')
  ] = None,
  ref: Annotated[Optional[str], _REF_FIELD] = None,
) -> dict[str, Any]:
  if (webview is None) == (ref is None):
    raise ValueError('give exactly one of webview and ref')
  if webview is None and target is not None:
    raise ValueError('target is available only with a live webview capture')
  if not credentials.available(_READER_SECRET):
    raise ValueError(
      '`browser::look` needs the optional `openai` credential; use `browser_find`, a targeted '
      'snapshot, or `artifact::read` and `artifact::grep` instead'
    )

  title: Optional[str] = None
  url: Optional[str] = None
  if webview is not None:
    content, content_ref, title, url = _capture(webview, source, target)
  else:
    assert ref is not None
    content_ref = ref
    content = _artifact_text(ref)
    if len(content.strip()) == 0:
      raise ValueError(f'artifact {ref} is empty')

  try:
    reader = _ReaderResult.model_validate(_read_page(_reader_prompt(question), content))
  except Exception as error:
    raise RuntimeError(
      f'page reader failed: {error}; if the capture is too large, narrow it with `target` '
      'or use `source="text"`'
    ) from error
  elements = []
  warnings = []
  for element in reader.elements:
    if source == 'snapshot' and _element_present(content, element):
      elements.append(element.model_dump())
    else:
      warnings.append(
        f'dropped element {element.ref!r}: role {element.role!r} and name '
        f'{element.name!r} do not match the capture representation'
      )
  result: dict[str, Any] = {
    'answer': _cut_answer(reader.answer),
    'elements': elements,
    'ref': content_ref,
  }
  if title is not None and url is not None:
    result['title'] = title
    result['url'] = url
  if warnings:
    result['warnings'] = warnings
  return result
