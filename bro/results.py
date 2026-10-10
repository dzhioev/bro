"""the session's kept tool results.

A store keeps each tool result whole under an id; `kept` wraps servers so every
result of their tools lands in it, a reply too long for one window carrying its
head and closing on a marker naming the `bro::page` call that continues it.
`page` and `results` read kept results back without calling the producing tool
again.
"""

import contextlib
import functools
import json
import os
import re
import secrets
import string
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Optional, TextIO

from pydantic import Field

from bro.base.offload import off_loop
from bro.base.text_window import BYTE_LIMIT
from bro.llm.mcp import FunctionTool, MCPServer, Tool, canonical_name, wire_name
from bro.monitor import SESSION_DIR_ENV, session_dir

DIRNAME = 'results'
PAGE_TOOL = 'page'
LIST_TOOL = 'results'

_TAG_FILENAME = 'tag'
_TAG_LENGTH = 4
_TAG_ALPHABET = string.ascii_lowercase + string.digits
_SUFFIX = '.result'
_ID = re.compile(rf'([a-z0-9]{{{_TAG_LENGTH}}})-([1-9][0-9]*)')
# how much of a call's arguments a listing line shows
_ARGUMENTS_SHOWN = 100

StoreSource = Callable[[], 'Store']


@dataclass(frozen=True)
class Entry:
  id: str
  tool: str
  arguments: dict[str, Any]
  characters: int


class Store:
  """results kept whole under `directory`, one file each: a JSON header line
  naming the producing call, then the result text. Ids are `<tag>-<n>`, the tag
  minted once per directory, so an id another store minted is refused rather
  than read as one of these; processes sharing the directory claim numbers by
  exclusive link and never overwrite one another."""

  def __init__(self, directory: Path):
    self.directory = directory
    self._tag: Optional[str] = None
    self._next_number = 0

  def keep(self, tool: str, arguments: dict[str, Any], text: str) -> str:
    """keep `text` as the result of `tool` called with `arguments`; returns its id."""
    tag = self.tag()
    header = json.dumps(
      {'tool': tool, 'arguments': arguments, 'characters': len(text)}, ensure_ascii=False
    )
    with self._staged(f'{header}\n{text}') as staged:
      number = self._claim(staged)
    return f'{tag}-{number}'

  def read(self, result_id: str) -> tuple[Entry, str]:
    """the entry and whole text kept under `result_id`."""
    try:
      with self._open(self._number(result_id)) as content:
        header, _, text = content.read().partition('\n')
    except FileNotFoundError:
      raise ValueError(f'this session kept no result {result_id}') from None
    return _entry(result_id, header), text

  def entries(self, *, before: Optional[str] = None) -> list[Entry]:
    """the kept results, newest first, only those kept before `before` when given."""
    if not self.directory.is_dir():
      return []
    bound = self._number(before) if before is not None else None
    tag = self.tag()
    found: list[Entry] = []
    for number in sorted(self._numbers(), reverse=True):
      if bound is not None and number >= bound:
        continue
      with self._open(number) as content:
        found.append(_entry(f'{tag}-{number}', content.readline()))
    return found

  def _number(self, result_id: str) -> int:
    match = _ID.fullmatch(result_id)
    if match is None:
      raise ValueError(f'{result_id!r} is not a result id; ids look like {self.tag()}-1')
    tag, number = match.groups()
    if tag != self.tag():
      raise ValueError(
        f'result {result_id} was kept by another session; this session keeps its results '
        f'as {self.tag()}-<n>'
      )
    return int(number)

  def _open(self, number: int) -> TextIO:
    # no newline translation either way: offsets index the text as the tool returned it
    return self._path(number).open(encoding='utf-8', newline='')

  def tag(self) -> str:
    if self._tag is None:
      self.directory.mkdir(parents=True, exist_ok=True)
      path = self.directory / _TAG_FILENAME
      minted = ''.join(secrets.choice(_TAG_ALPHABET) for _ in range(_TAG_LENGTH))
      with self._staged(minted) as staged:
        with contextlib.suppress(FileExistsError):
          os.link(staged, path)
      self._tag = path.read_text(encoding='utf-8')
    return self._tag

  @contextlib.contextmanager
  def _staged(self, content: str) -> Iterator[Path]:
    with tempfile.NamedTemporaryFile(
      'w', encoding='utf-8', newline='', dir=self.directory, prefix='.staged-'
    ) as staged:
      staged.write(content)
      staged.flush()
      yield Path(staged.name)

  def _claim(self, staged: Path) -> int:
    if self._next_number == 0:
      self._next_number = max(self._numbers(), default=0) + 1
    while True:
      number = self._next_number
      self._next_number += 1
      try:
        os.link(staged, self._path(number))
      except FileExistsError:
        continue
      return number

  def _numbers(self) -> list[int]:
    return [int(path.stem) for path in self.directory.glob(f'*{_SUFFIX}')]

  def _path(self, number: int) -> Path:
    return self.directory / f'{number}{_SUFFIX}'


def _entry(result_id: str, header: str) -> Entry:
  fields = json.loads(header)
  return Entry(result_id, fields['tool'], fields['arguments'], fields['characters'])


def session_store() -> Store:
  """the managed session's store, under its session state directory."""
  state = session_dir()
  if state is None:
    raise RuntimeError(f'{SESSION_DIR_ENV} is unset: kept results live in the session state dir')
  return _store_at(state / DIRNAME)


@functools.cache
def _store_at(directory: Path) -> Store:
  return Store(directory)


@contextlib.contextmanager
def temporary_store() -> Iterator[Store]:
  """a store for a run outside any managed session, removed with its block."""
  with tempfile.TemporaryDirectory(prefix='bro-results-') as directory:
    yield Store(Path(directory) / DIRNAME)


def window(result_id: str, text: str, char_offset: int, char_limit: int) -> str:
  """`char_limit` characters of `text` from `char_offset`. A window short of the
  end is followed, on a line of its own, by the marker naming the call that reads
  on; the line break before the marker is never part of the result."""
  if not 0 <= char_offset <= len(text):
    raise ValueError(
      f'char_offset {char_offset:,} is outside result {result_id} (0..{len(text):,})'
    )
  end = char_offset + char_limit
  if end >= len(text):
    return text[char_offset:]
  return (
    f'{text[char_offset:end]}\n[...{len(text) - end:,} more characters — '
    f'bro::{PAGE_TOOL}(result="{result_id}", char_offset={end})...]'
  )


class _KeptTool(Tool):
  def __init__(self, tool: Tool, canonical: str, store: StoreSource):
    self._tool = tool
    self._canonical = canonical
    self._store = store

  @property
  def name(self) -> str:
    return self._tool.name

  @property
  def description(self) -> str:
    return self._tool.description

  @property
  def parameters(self) -> dict[str, Any]:
    return self._tool.parameters

  # no output schema: a structured result too long for one window arrives as
  # windowed JSON text, which a declared schema would refuse
  @property
  def output_schema(self) -> Optional[dict[str, Any]]:
    return None

  async def call(self, arguments: dict[str, Any]) -> dict[str, Any] | str:
    result = await self._tool.call(arguments)
    text = result if isinstance(result, str) else json.dumps(result, indent=2, ensure_ascii=False)
    result_id = await off_loop(self._store().keep, self._canonical, arguments, text)
    if len(text) <= BYTE_LIMIT:
      return result
    return window(result_id, text, 0, BYTE_LIMIT)


class _StoreReadTool(FunctionTool):
  """a tool reading the store back, whose own results are not kept."""


class _KeptServer(MCPServer):
  def __init__(self, server: MCPServer, store: StoreSource):
    self._server = server
    self._store = store
    self.namespace = server.namespace
    self.tool_universe = server.tool_universe
    self.needed_secrets = server.needed_secrets
    self.optional_secrets = server.optional_secrets

  async def list_tools(self) -> list[Tool]:
    return [
      tool
      if isinstance(tool, _StoreReadTool)
      else _KeptTool(tool, canonical_name(wire_name(self.namespace, tool.name)), self._store)
      for tool in await self._server.list_tools()
    ]

  def close(self) -> None:
    self._server.close()


def kept(servers: list[MCPServer], store: StoreSource) -> list[MCPServer]:
  """`servers` with every tool's result kept in the store `store` returns."""
  return [_KeptServer(server, store) for server in servers]


_PAGE_DESCRIPTION = (
  'return a window of a tool result this session kept, read from the kept copy without calling '
  'its tool again. a reply too long to return whole closes on a marker naming this call with '
  'the `char_offset` to continue at; a window that stops short of the end closes on the same '
  'kind of marker.'
)

_LIST_DESCRIPTION = (
  'list the tool results this session kept, newest first: the id `page` reads, the tool and '
  'arguments that produced it, and its length in characters. a listing too long to return '
  'whole closes on a marker naming this call with the `before` that lists the older ones.'
)


def store_tools(store: StoreSource) -> list[Tool]:
  """`page` and `results`, the service tools reading the store back."""

  def page(
    result: Annotated[str, Field(description='id of the kept result, as a marker names it')],
    char_offset: Annotated[int, Field(description='character to start the window at', ge=0)] = 0,
    char_limit: Annotated[
      int, Field(description='most characters to return', ge=1, le=BYTE_LIMIT)
    ] = BYTE_LIMIT,
  ) -> str:
    _, text = store().read(result)
    return window(result, text, char_offset, char_limit)

  def results(
    before: Annotated[
      Optional[str],
      Field(description='list only results kept before this id, as a closing marker names it'),
    ] = None,
  ) -> str:
    entries = store().entries(before=before)
    if len(entries) == 0:
      return 'no results kept yet' if before is None else f'no results kept before {before}'
    lines: list[str] = []
    size = 0
    for index, entry in enumerate(entries):
      line = _listing_line(entry)
      if size + len(line) + 1 > BYTE_LIMIT:
        lines.append(
          f'[...{len(entries) - index:,} older results — '
          f'bro::{LIST_TOOL}(before="{entries[index - 1].id}")...]'
        )
        break
      lines.append(line)
      size += len(line) + 1
    return '\n'.join(lines)

  return [
    _StoreReadTool(page, name=PAGE_TOOL, description=_PAGE_DESCRIPTION),
    _StoreReadTool(results, name=LIST_TOOL, description=_LIST_DESCRIPTION),
  ]


def _listing_line(entry: Entry) -> str:
  arguments = json.dumps(entry.arguments, ensure_ascii=False)
  if len(arguments) > _ARGUMENTS_SHOWN:
    arguments = f'{arguments[:_ARGUMENTS_SHOWN]}…'
  return f'{entry.id}  {entry.tool}  {arguments}  {entry.characters:,} characters'
