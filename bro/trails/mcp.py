import builtins
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Annotated, Optional

from pydantic import Field

from bro.base.ansi import Colors
from bro.base.text_window import DEFAULT_LIMIT, MAX_LIMIT, numbered_window, window
from bro.llm.mcp import Context, Toolset
from bro.trails.client import TrailsClient, default_client
from bro.trails.display import (
  ColorMode,
  DisplayRecord,
  PresetName,
  RecordedAdapter,
  preset,
  retained_document,
)
from bro.trails.search import grep_lines


class _Toolset(Toolset[TrailsClient]):
  secrets = ('trails',)


toolset = _Toolset('trails', state=default_client, close=TrailsClient.close)

_NO_COLOR = Colors(False)
_SUBJECT_LIMIT = 60
_TRAIL_ID_FIELD = Field(description='recorded trail id')
_OFFSET_FIELD = Field(description='0-based output line offset', ge=0)
_LIMIT_FIELD = Field(
  description=(
    f'max output lines; values above {MAX_LIMIT:,} are clamped, with the clamp announced inline'
  )
)


@dataclass
class TrailSummary:
  id: str
  started_at: str
  harness: str
  owner: Optional[str]
  model: Optional[str]
  status: str
  forked_from: Optional[str]
  subject: Optional[str]


def _subject(subject: Optional[str]) -> Optional[str]:
  if subject is None:
    return None
  oneline = subject.replace('\n', ' ').replace('\r', ' ')
  if len(oneline) <= _SUBJECT_LIMIT:
    return oneline
  return f'{oneline[:_SUBJECT_LIMIT]}... <{len(oneline) - _SUBJECT_LIMIT} more chars>'


def _summary(adapter: RecordedAdapter, trail: dict) -> TrailSummary:
  row = adapter.trail_list_row(trail)
  return TrailSummary(
    id=row.trail_id,
    started_at=trail['started_at'],
    harness=row.harness,
    owner=row.owner,
    model=row.model,
    status=row.status,
    forked_from=row.forked_from,
    subject=_subject(row.subject),
  )


@toolset.tool(
  'list recorded trails newest first. Each summary carries the id, timestamp, harness, '
  'workspace or bro owner, model, status, fork parent, and subject; use show for detail. '
  'The harness, bro, and forked_from filters are mutually exclusive.'
)
def list(
  context: Context[TrailsClient],
  harness: Annotated[
    Optional[str],
    Field(description='filter by harness (for example bro or claude)'),
  ] = None,
  bro: Annotated[
    Optional[str],
    Field(description='filter by bro name'),
  ] = None,
  since: Annotated[
    Optional[str],
    Field(description='ISO timestamp lower bound on started_at'),
  ] = None,
  until: Annotated[
    Optional[str],
    Field(description='ISO timestamp upper bound on started_at'),
  ] = None,
  forked_from: Annotated[
    Optional[str],
    Field(description='list direct forks of this trail id'),
  ] = None,
  limit: Annotated[
    int,
    Field(description='max trails to return (1-100)', ge=1, le=100),
  ] = 20,
) -> list[TrailSummary]:
  selectors = [selector for selector in (harness, bro, forked_from) if selector is not None]
  if len(selectors) > 1:
    raise ValueError('harness, bro, and forked_from are mutually exclusive filters')
  page = context.state.list_trails(
    harness=harness,
    bro=bro,
    since=since,
    until=until,
    forked_from=forked_from,
    limit=limit,
  )
  adapter = RecordedAdapter(context.state)
  return [_summary(adapter, trail) for trail in page['trails']]


def _document(records: Iterable[DisplayRecord], preset_name: PresetName) -> str:
  configuration = preset(preset_name, color=ColorMode.NEVER)
  return retained_document(records, configuration)


def _show_document(client: TrailsClient, trail_id: str) -> str:
  adapter = RecordedAdapter(client)
  return _document(
    adapter.conversation_records(client.get_trail(trail_id)),
    PresetName.REWIND_SHOW,
  )


def _steps_document(client: TrailsClient, trail_id: str) -> str:
  adapter = RecordedAdapter(client)
  records = [
    adapter.trail_metadata(client.get_trail(trail_id)),
    *adapter.native_step_records(trail_id, client.iter_steps(trail_id)),
  ]
  return _document(records, PresetName.REWIND_STEPS)


def _grep_document(
  client: TrailsClient,
  pattern: str,
  *,
  trails: builtins.list[str],
  harness: Optional[str],
  ignore_case: bool,
  before_context: int,
  after_context: int,
  trail_limit: int,
) -> str:
  try:
    regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
  except re.error as exception:
    raise ValueError(f'invalid pattern {pattern!r}: {exception}') from exception
  if len(trails) > 0:
    headers = [client.get_trail(trail_id) for trail_id in trails]
  else:
    headers = client.list_trails(harness=harness, limit=trail_limit)['trails']

  groups: builtins.list[str] = []
  for header in headers:
    adapter = RecordedAdapter(client)
    rendered = _document(adapter.conversation_records(header), PresetName.REWIND_GREP)
    matches = grep_lines(
      header['id'],
      rendered,
      regex,
      _NO_COLOR,
      before=before_context,
      after=after_context,
    )
    if len(matches) > 0:
      groups.append('\n'.join(matches))
  separator = '\n--\n' if before_context > 0 or after_context > 0 else '\n'
  return separator.join(groups) + ('\n' if len(groups) > 0 else '')


def _tree_document(client: TrailsClient, trail_id: str) -> str:
  adapter = RecordedAdapter(client)
  return _document(adapter.lineage_records(trail_id), PresetName.REWIND_TREE)


@toolset.tool(
  'read a line-numbered window of one trail as a generalized cross-harness conversation. '
  'The view follows its fork chain through parent anchors and includes launch context, '
  'reasoning, assistant text, tool calls, and results.'
)
def show(
  context: Context[TrailsClient],
  trail_id: Annotated[str, _TRAIL_ID_FIELD],
  offset: Annotated[int, _OFFSET_FIELD] = 0,
  limit: Annotated[int, _LIMIT_FIELD] = DEFAULT_LIMIT,
) -> str:
  return numbered_window(
    _show_document(context.state, trail_id),
    offset,
    limit,
  )


@toolset.tool(
  "read a line-numbered window of one trail's lossless harness-native step stream. "
  'Use this debugging view when the generalized conversation omits needed record detail.'
)
def steps(
  context: Context[TrailsClient],
  trail_id: Annotated[str, _TRAIL_ID_FIELD],
  offset: Annotated[int, _OFFSET_FIELD] = 0,
  limit: Annotated[int, _LIMIT_FIELD] = DEFAULT_LIMIT,
) -> str:
  return numbered_window(_steps_document(context.state, trail_id), offset, limit)


@toolset.tool(
  'search rendered conversations with a Python regular expression and return a bounded '
  '<trail-id>:<line>:<text> result window. With no trail ids, searches the newest matching '
  'trails up to trail_limit.'
)
def grep(
  context: Context[TrailsClient],
  pattern: Annotated[str, Field(description='Python regular expression to search for')],
  trails: Annotated[
    Optional[builtins.list[str]],
    Field(description='trail ids to search; omit to search the newest trails', max_length=100),
  ] = None,
  harness: Annotated[
    Optional[str],
    Field(description='filter by harness when trail ids are omitted'),
  ] = None,
  ignore_case: Annotated[bool, Field(description='ignore case')] = False,
  before_context: Annotated[
    int,
    Field(description='lines of leading context around each match', ge=0, le=100),
  ] = 0,
  after_context: Annotated[
    int,
    Field(description='lines of trailing context around each match', ge=0, le=100),
  ] = 0,
  trail_limit: Annotated[
    int,
    Field(description='max trails to search when trail ids are omitted (1-100)', ge=1, le=100),
  ] = 20,
  offset: Annotated[int, _OFFSET_FIELD] = 0,
  limit: Annotated[int, _LIMIT_FIELD] = DEFAULT_LIMIT,
) -> str:
  rendered = _grep_document(
    context.state,
    pattern,
    trails=trails or [],
    harness=harness,
    ignore_case=ignore_case,
    before_context=before_context,
    after_context=after_context,
    trail_limit=trail_limit,
  )
  return window(rendered, offset, limit)


@toolset.tool(
  'read a line-numbered window of the fork ancestry and descendants reachable from one trail'
)
def tree(
  context: Context[TrailsClient],
  trail_id: Annotated[str, _TRAIL_ID_FIELD],
  offset: Annotated[int, _OFFSET_FIELD] = 0,
  limit: Annotated[int, _LIMIT_FIELD] = DEFAULT_LIMIT,
) -> str:
  return numbered_window(_tree_document(context.state, trail_id), offset, limit)
