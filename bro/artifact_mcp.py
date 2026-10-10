import codecs
import re
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from io import DEFAULT_BUFFER_SIZE
from pathlib import Path, PurePosixPath
from typing import Annotated, Optional

from pydantic import Field

from bro.artifact import REF_FORM, get_artifact
from bro.base.text_window import DEFAULT_LIMIT, MAX_LIMIT, apply_limit
from bro.mcp import Toolset

toolset = Toolset[None]('artifact')

_REF_FIELD = Field(description=f'artifact ref ({REF_FORM})')
_PATH_FIELD = Field(
  description='file path inside a directory artifact; omit when the ref is a file artifact'
)
_LINE_BREAK = re.compile(r'\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]')
_LINE_END = re.compile(r'(?:\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029])\Z')
# the longest line held in memory whole; the tools refuse a longer one they
# would have to read or search rather than materialize it
MAXIMUM_LINE_LENGTH = 1_000_000


@dataclass(frozen=True)
class _Line:
  length: int
  prefix: str

  @property
  def whole(self) -> bool:
    return len(self.prefix) == self.length


class _HeadWindow:
  def __init__(self, limit: int):
    self.limit = limit
    self.effective_limit = min(max(limit, 1), MAX_LIMIT)
    self.parts: list[str] = []
    self.kept_lines = 0
    self.kept_bytes = 0
    self.total_lines = 0
    self.total_bytes = 0

  @property
  def full(self) -> bool:
    return self.kept_lines >= self.effective_limit

  def add_line(self, text: str) -> None:
    if self.full:
      self.count_line(len(text))
      return
    self.total_lines += 1
    self.total_bytes += len(text)
    self.parts.append(text)
    self.kept_lines += 1
    self.kept_bytes += len(text)

  def count_line(self, length: int) -> None:
    self.total_lines += 1
    self.total_bytes += length

  def render(self, *, before_lines: int = 0, before_bytes: int = 0, after_note: str = '') -> str:
    return apply_limit(
      ''.join(self.parts),
      self.limit,
      skipped_before_lines=before_lines,
      skipped_before_bytes=before_bytes,
      skipped_after_lines=self.total_lines - self.kept_lines,
      skipped_after_bytes=self.total_bytes - self.kept_bytes,
      after_note=after_note,
    )


def _resolve_file(ref: str, path: Optional[str]) -> Path:
  artifact_path = Path(get_artifact(ref))
  if artifact_path.is_file():
    if path is not None:
      raise ValueError(f'artifact {ref} is a file; path must be omitted')
    return artifact_path
  if not artifact_path.is_dir():
    raise ValueError(f'artifact {ref} resolved to neither a file nor a directory')
  if path is None:
    raise ValueError(f'artifact {ref} is a directory; path is required')

  relative_path = PurePosixPath(path)
  if relative_path.is_absolute() or '..' in relative_path.parts:
    raise ValueError(f'artifact path must stay inside {ref}: {path!r}')
  root = artifact_path.resolve()
  candidate = (root / Path(*relative_path.parts)).resolve()
  if not candidate.is_relative_to(root):
    raise ValueError(f'artifact path must stay inside {ref}: {path!r}')
  if not candidate.is_file():
    raise ValueError(f'artifact {ref} has no file at {path!r}')
  return candidate


def _location(ref: str, path: Optional[str]) -> str:
  return ref if path is None else f'{ref}/{path}'


def _decoded_lines(artifact_path: Path, location: str) -> Iterator[_Line]:
  decoder = codecs.getincrementaldecoder('utf-8')()
  line_length = 0
  line_prefix = ''
  pending_carriage_return = ''

  def add(segment: str) -> None:
    nonlocal line_length, line_prefix
    line_length += len(segment)
    remaining = MAXIMUM_LINE_LENGTH - len(line_prefix)
    if remaining > 0:
      line_prefix += segment[:remaining]

  def consume(text: str) -> Iterator[_Line]:
    nonlocal line_length, line_prefix
    start = 0
    for match in _LINE_BREAK.finditer(text):
      add(text[start : match.end()])
      yield _Line(line_length, line_prefix)
      line_length = 0
      line_prefix = ''
      start = match.end()
    add(text[start:])

  try:
    with artifact_path.open('rb') as content:
      while chunk := content.read(DEFAULT_BUFFER_SIZE):
        text = pending_carriage_return + decoder.decode(chunk)
        pending_carriage_return = ''
        if text.endswith('\r'):
          pending_carriage_return = '\r'
          text = text[:-1]
        yield from consume(text)
      yield from consume(pending_carriage_return + decoder.decode(b'', final=True))
  except UnicodeDecodeError as error:
    raise ValueError(f'artifact {location} is not UTF-8 text') from error
  if line_length > 0:
    yield _Line(line_length, line_prefix)


def _artifact_lines(ref: str, path: Optional[str]) -> Iterator[_Line]:
  return _decoded_lines(_resolve_file(ref, path), _location(ref, path))


@toolset.tool(
  'read a text artifact as a numbered line window. For a directory artifact, path '
  f'names the file to read. Content is capped at {MAX_LIMIT:,} lines, with skipped-content '
  f'markers naming the offset that reads on; a line longer than {MAXIMUM_LINE_LENGTH:,} '
  'characters is refused rather than materialized.'
)
def read(
  ref: Annotated[str, _REF_FIELD],
  path: Annotated[Optional[str], _PATH_FIELD] = None,
  offset: Annotated[int, Field(description='0-based line index to start reading from', ge=0)] = 0,
  limit: Annotated[
    int,
    Field(
      description=(
        f'max lines to return; values above {MAX_LIMIT:,} are clamped, with the clamp '
        'announced inline'
      )
    ),
  ] = DEFAULT_LIMIT,
) -> str:
  window = _HeadWindow(limit)
  before_lines = 0
  before_bytes = 0
  effective_offset = max(offset, 0)
  for line_number, line in enumerate(_artifact_lines(ref, path), start=1):
    if line_number <= effective_offset:
      before_lines += 1
      before_bytes += line.length
      continue
    number = f'{line_number:>5}\t'
    if window.full:
      window.count_line(len(number) + line.length)
      continue
    if not line.whole:
      raise ValueError(
        f'line {line_number} of artifact {_location(ref, path)} is longer than '
        f'{MAXIMUM_LINE_LENGTH:,} characters; read refuses to materialize it'
      )
    window.add_line(number + line.prefix)
  return window.render(
    before_lines=before_lines,
    before_bytes=before_bytes,
    after_note=f'read on with offset={effective_offset + window.kept_lines}',
  )


def _body(line: _Line) -> str:
  ending = _LINE_END.search(line.prefix)
  return line.prefix if ending is None else line.prefix[: ending.start()]


@toolset.tool(
  'search a text artifact with a regular expression and return matching lines with their '
  '1-based numbers and optional surrounding context. For a directory artifact, path names '
  f'the file to search. Output follows the shared line cap; a line longer than '
  f'{MAXIMUM_LINE_LENGTH:,} characters is refused rather than materialized.'
)
def grep(
  ref: Annotated[str, _REF_FIELD],
  pattern: Annotated[str, Field(description='regular expression to search for')],
  path: Annotated[Optional[str], _PATH_FIELD] = None,
  context: Annotated[
    int,
    Field(
      description='lines of context to include before and after each match',
      ge=0,
      le=DEFAULT_LIMIT,
    ),
  ] = 0,
) -> str:
  if context < 0 or context > DEFAULT_LIMIT:
    raise ValueError(f'context must be between 0 and {DEFAULT_LIMIT:,}')
  try:
    expression = re.compile(pattern)
  except re.error as error:
    raise ValueError(f'invalid regular expression: {error}') from error

  window = _HeadWindow(DEFAULT_LIMIT)
  history: deque[tuple[int, str]] = deque(maxlen=context)
  last_selected: Optional[int] = None
  after_context_until = 0

  def select(line_number: int, body: str, *, matched: bool) -> None:
    nonlocal last_selected
    if last_selected is not None and line_number <= last_selected:
      return
    if last_selected is not None and line_number != last_selected + 1:
      window.add_line('--\n')
    separator = ':' if matched else '-'
    window.add_line(f'{line_number}{separator}{body}\n')
    last_selected = line_number

  location = _location(ref, path)
  for line_number, line in enumerate(_artifact_lines(ref, path), start=1):
    if not line.whole:
      raise ValueError(
        f'artifact {location} has a line longer than {MAXIMUM_LINE_LENGTH:,} characters; '
        'grep cannot search it with bounded memory'
      )
    body = _body(line)
    matched = expression.search(body) is not None
    if matched:
      for prior_number, prior_body in history:
        select(prior_number, prior_body, matched=False)
      select(line_number, body, matched=True)
      after_context_until = line_number + context
    elif line_number <= after_context_until:
      select(line_number, body, matched=False)
    history.append((line_number, body))

  return window.render()
