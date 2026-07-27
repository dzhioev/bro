#!/usr/bin/env python
"""`rewind` — the single reader over recorded runs, every harness.

Five subcommands against the deployed `trails-server` (config from the `trails`
secret):

- `rewind list` — cross-harness listing of trail headers, newest first, paged
  through `$PAGER`; filters mirror the server's indexed selectors.
- `rewind show <trail-id>` — the default command (`rewind <id>` means
  `rewind show <id>`), renders the generalized `/messages` conversation. The
  fork chain is walked through each parent's anchor so the whole conversation
  reads as one timeline, with tool results inlined under their calls and the
  trail's stored launch context as a SESSION CONTEXT preamble.
- `rewind steps <trail-id>` — renders one trail's lossless `/steps` native
  stream for debugging.
- `rewind grep <pattern> [trail-id ...]` — greps the same conversation render
  as `show`, as if trails were files: `<id>:<line>:<text>`. With no ids it
  searches every trail, newest first (optionally filtered by `--harness`).
- `rewind tree <trail-id>` — the forked_from/fork hierarchy reachable from a
  trail.

`show` and `steps` both accept `-f` to keep polling and render new records as
they land, like `tail -f`.
"""

import datetime
import http.client
import json
import re
import sys
import time
from collections.abc import Callable, Iterator
from typing import Any, Optional

import base.args
from base import log, pager
from base.ansi import Colors, should_color
from trails.client import HTTPStatusError, TrailsClient, default_client, is_retryable_status
from trails.lineage import walk_header_chain
from trails.model import spill_descriptor

__cli_name__ = 'rewind'

_BODY_TRUNCATE_CHARS = 240
# step ids are truncated for display; trail ids are always rendered in full, so
# that any id on screen can be pasted into another rewind command — the server
# does not accept prefixes
_STEP_ID_DISPLAY_CHARS = 10


def _format_timestamp(iso: Optional[str]) -> str:
  if iso is None:
    return '-'
  # 2026-06-07T22:14:03.123456Z -> 2026-06-07 22:14:03
  return iso.replace('T', ' ')[:19]


def _format_duration(seconds: float) -> str:
  seconds_int = int(seconds)
  if seconds_int < 60:
    return f'{seconds_int}s'
  if seconds_int < 3600:
    return f'{seconds_int // 60}m {seconds_int % 60}s'
  hours, remainder = divmod(seconds_int, 3600)
  return f'{hours}h {remainder // 60}m'


def _parse_iso(value: str) -> Optional[datetime.datetime]:
  try:
    return datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
  except ValueError:
    return None


def _truncate_oneline(body: Any, limit: int = _BODY_TRUNCATE_CHARS) -> str:
  """Render a value to one line with a marker for content past `limit`."""
  if body is None:
    return ''
  if isinstance(body, str):
    text = body
  else:
    text = json.dumps(body, ensure_ascii=False)
  text = text.replace('\n', ' ').replace('\r', ' ')
  if len(text) <= limit:
    return text
  return f'{text[:limit]}... <{len(text) - limit} more chars>'


def _indent(text: str, prefix: str = '  ') -> str:
  return '\n'.join(prefix + line for line in text.splitlines())


def _who(trail: dict) -> Any:
  if trail.get('harness') == 'claude':
    return trail.get('location', {}).get('workspace', '?')
  return trail.get('bro', '?')


# --- listing ----------------------------------------------------------------------


def _format_trail_row(trail: dict, colors: Colors) -> str:
  trail_id = trail.get('id', '?')
  harness = trail.get('harness', '?')
  owner = _who(trail)
  owner = owner if owner is not None else '?'
  model = trail.get('native', {}).get('llm', {}).get('model', '?')
  started = _format_timestamp(trail.get('started_at'))
  end = trail.get('end')
  if end is None:
    status = f'{colors.yellow}live{colors.reset}'
  elif end.get('reason') == 'lost':
    status = f'{colors.red}lost{colors.reset}'
  else:
    status = f'{colors.green}done:{end.get("reason")}{colors.reset}'
  tail = ''
  forked_from = trail.get('forked_from')
  if forked_from is not None:
    tail += f'  {colors.dim}fork-of {forked_from["trail_id"]}{colors.reset}'
  subject = trail.get('subject')
  if subject is not None:
    tail += f'  {colors.dim}{_truncate_oneline(subject, 60)}{colors.reset}'
  return (
    f'{colors.yellow}{trail_id}{colors.reset}  '
    f'{colors.dim}{started}{colors.reset}  '
    f'{colors.magenta}{harness:<6}{colors.reset}  '
    f'{colors.cyan}{owner:<10}{colors.reset}  '
    f'{colors.dim}{model:<10}{colors.reset}  '
    f'{status}{tail}'
  )


def _command_list(client: TrailsClient, args: dict, colors: Colors) -> int:
  trails_iter = client.iter_trails(
    harness=args.get('harness'),
    bro=args.get('bro'),
    forked_from=args.get('forked_from'),
    since=args.get('since'),
    until=args.get('until'),
    max_items=args.get('limit'),
  )
  rows = [_format_trail_row(trail, colors) for trail in trails_iter]
  if len(rows) == 0:
    print('(no trails)', file=sys.stderr)
    return 0
  _emit(('\n'.join(rows)) + '\n', args)
  return 0


def _emit(text: str, args: dict) -> None:
  if sys.stdout.isatty() and not bool(args.get('no_pager', False)):
    pager.page(text)
  else:
    sys.stdout.write(text)


# --- headers ----------------------------------------------------------------------


def _bro_native_header_fields(trail: dict) -> list[tuple[str, Any]]:
  native = trail.get('native', {})
  fields: list[tuple[str, Any]] = [('llm', native.get('llm', {}))]
  counts = native.get('step_counts_by_kind')
  if isinstance(counts, dict):
    nonzero = {key: value for key, value in counts.items() if value > 0}
    fields.append(('step kinds', nonzero))
  return fields


def _claude_native_header_fields(trail: dict) -> list[tuple[str, Any]]:
  native = trail.get('native', {})
  fields: list[tuple[str, Any]] = [
    ('llm', native.get('llm', {})),
    ('claude-code', native.get('harness_version', '?')),
    ('lines', native.get('line_count', '?')),
    ('segment', native.get('segment', '?')),
  ]
  cw_command = native.get('cw_command')
  if cw_command is not None:
    fields.append(('cw', cw_command))
  return fields


_NATIVE_HEADER_FIELDS: dict[str, Callable[[dict], list[tuple[str, Any]]]] = {
  'bro': _bro_native_header_fields,
  'claude': _claude_native_header_fields,
}


def _format_end(trail: dict) -> str:
  end = trail.get('end')
  if end is None:
    return 'live'
  end_at = end.get('at')
  duration = ''
  started_at = trail.get('started_at')
  if isinstance(started_at, str) and isinstance(end_at, str):
    start_moment = _parse_iso(started_at)
    end_moment = _parse_iso(end_at)
    if start_moment is not None and end_moment is not None:
      duration = f', {_format_duration((end_moment - start_moment).total_seconds())}'
  return f'{_format_timestamp(end_at)} ({end.get("reason")}{duration})'


def _format_pointer(pointer: dict) -> str:
  rendered = f'{pointer.get("trail_id")} @ step {pointer.get("step_id")}'
  index = pointer.get('index')
  if index is not None:
    rendered += f':{index}'
  return rendered


def _format_header_value(value: Any) -> str:
  if isinstance(value, str):
    return value
  return json.dumps(value, ensure_ascii=False)


def _format_header(trail: dict, colors: Colors) -> str:
  harness = trail['harness']
  location = trail.get('location', {})
  fields: list[tuple[str, Any]] = [
    ('trail', trail.get('id')),
    ('harness', harness),
    ('started', _format_timestamp(trail.get('started_at'))),
    ('ended', _format_end(trail)),
    ('bro', trail.get('bro')),
    ('version', trail.get('version')),
    ('interactive', trail.get('interactive')),
    ('surface', trail.get('surface')),
  ]
  if isinstance(location, dict):
    for key in ('workspace', 'host'):
      if key in location:
        fields.append((key, location[key]))
  subject = trail.get('subject')
  if subject is not None:
    fields.append(('subject', subject))
  forked_from = trail.get('forked_from')
  if isinstance(forked_from, dict):
    fields.append(('forked from', _format_pointer(forked_from)))
  summoned_by = trail.get('summoned_by')
  if summoned_by is not None:
    fields.append(('summoned by', summoned_by))
  fields.extend(
    [
      ('turns', trail.get('turn_count')),
      ('usage', trail.get('usage', {})),
      ('models', trail.get('models', [])),
    ]
  )
  fields.extend(_NATIVE_HEADER_FIELDS[harness](trail))
  width = max(len(label) for label, _ in fields)
  lines = []
  for index, (label, value) in enumerate(fields):
    emphasis = colors.bold if index == 0 else colors.dim
    lines.append(f'{emphasis}{label:<{width}}{colors.reset} {_format_header_value(value)}')
  lines.append(colors.dim + ('─' * 78) + colors.reset)
  return '\n'.join(lines)


# --- native steps -----------------------------------------------------------------


def _format_step_summary(step: dict, colors: Colors) -> str:
  kind = step.get('kind', '?')
  step_id = step.get('step_id', '?')
  timestamp = _format_timestamp(step.get('ts'))
  turn = step.get('turn_index')
  turn_text = f't{turn} ' if turn is not None else ''
  prefix = (
    f'{colors.yellow}{step_id}{colors.reset}  '
    f'{colors.dim}{timestamp}{colors.reset}  {colors.yellow}{turn_text}{kind:<14}{colors.reset}'
  )

  body = step.get('raw') if 'raw' in step else step.get('body')
  if kind == 'end' and isinstance(body, dict) and body.get('reason') == 'terminal':
    body = {**body, 'reason': 'ok'}
  spilled = spill_descriptor(body)
  if spilled is not None:
    size = spilled.get('size', '?')
    url = spilled.get('url', '-')
    summary = f'{colors.dim}<{size} bytes spilled>{colors.reset} {url}'
  else:
    summary = _truncate_oneline(body)

  omitted = {
    'trail_id',
    'step_id',
    'kind',
    'ts',
    'turn_index',
    'body',
    'raw',
    'record',
    'where',
  }
  extra_parts = []
  for key, value in step.items():
    if key in omitted:
      continue
    label = 'args' if key == 'arguments' else key
    extra_parts.append(f'{colors.cyan}{label}{colors.reset}={_truncate_oneline(value, 80)}')

  parts = [prefix]
  if len(summary) > 0:
    parts.append(summary)
  if len(extra_parts) > 0:
    parts.append(f'[{" ".join(extra_parts)}]')
  return '  '.join(parts)


def _render_native_trail(
  client: TrailsClient, trail: dict, colors: Colors
) -> tuple[str, Optional[str | int]]:
  rows = list(client.iter_steps(trail['id']))
  output = [_format_header(trail, colors)]
  output.extend(_format_step_summary(row, colors) for row in rows)
  after = rows[-1]['step_id'] if len(rows) > 0 else None
  return '\n'.join(output) + '\n', after


# --- conversation -----------------------------------------------------------------


def _user_text(content: Any) -> Optional[str]:
  if isinstance(content, str):
    stripped = content.strip()
    return stripped if len(stripped) > 0 else None
  if not isinstance(content, list):
    return None
  parts = [
    block.get('text', '')
    for block in content
    if isinstance(block, dict) and block.get('type') == 'text'
  ]
  joined = '\n'.join(parts).strip()
  return joined if len(joined) > 0 else None


def _message_text(content: Any) -> str:
  if content is None:
    return ''
  if isinstance(content, str):
    return content.strip()
  user_text = _user_text(content)
  if user_text is not None:
    return user_text
  return _format_header_value(content)


def _format_tool_call(message: dict) -> str:
  name = message.get('tool_name', '?')
  arguments = message.get('arguments', {})
  return f'{name}({json.dumps(arguments, ensure_ascii=False, separators=(", ", ": "))})'


def _tool_result_text(content: Any) -> str:
  if isinstance(content, list):
    text_parts = [
      block.get('text', '')
      for block in content
      if isinstance(block, dict) and block.get('type') == 'text'
    ]
    if len(text_parts) > 0:
      return '\n'.join(text_parts)
  if isinstance(content, str):
    return content
  return _format_header_value(content)


def _index_tool_results(messages: list[dict]) -> dict[str, str]:
  results: dict[str, str] = {}
  for message in messages:
    if message.get('type') != 'tool_result':
      continue
    call_id = message.get('call_id')
    if isinstance(call_id, str):
      results[call_id] = _tool_result_text(message.get('content'))
  return results


def _source_step_id(message: dict) -> str | int:
  return message['source']['step_id']


def _group_messages(messages: list[dict]) -> list[list[dict]]:
  groups: list[list[dict]] = []
  for message in messages:
    if len(groups) == 0 or _source_step_id(groups[-1][0]) != _source_step_id(message):
      groups.append([message])
    else:
      groups[-1].append(message)
  return groups


class _ConversationTimeline:
  """Render generalized messages while retaining turn and tool-call state."""

  def __init__(self, colors: Colors, tool_results: dict[str, str]):
    self.colors = colors
    self.tool_results = tool_results
    self.rendered_call_ids: set[str] = set()
    self.turn = 0

  def render(self, messages: list[dict], *, incremental: bool = False) -> list[str]:
    previously_rendered_calls = set(self.rendered_call_ids)
    self.tool_results.update(_index_tool_results(messages))
    output: list[str] = []
    for group in _group_messages(messages):
      output.extend(
        self._render_group(
          group,
          incremental=incremental,
          previously_rendered_calls=previously_rendered_calls,
        )
      )
    return output

  def _render_group(
    self,
    messages: list[dict],
    *,
    incremental: bool,
    previously_rendered_calls: set[str],
  ) -> list[str]:
    user_messages = [message for message in messages if message.get('type') == 'user_input']
    if len(user_messages) > 0:
      output: list[str] = []
      for message in user_messages:
        text = _user_text(message.get('content'))
        if text is not None:
          output.extend(self._turn('USER', message, [_indent(text)], self.colors.blue))
      return output

    visible = [
      message
      for message in messages
      if message.get('type') in {'reasoning', 'assistant', 'tool_call', 'error'}
    ]
    if len(visible) > 0:
      blocks = [
        block for message in visible if (block := self._assistant_block(message)) is not None
      ]
      if len(blocks) == 0:
        return []
      role = 'ERROR' if all(message.get('type') == 'error' for message in visible) else 'ASSISTANT'
      role_color = self.colors.red if role == 'ERROR' else self.colors.green
      return self._turn(role, visible[0], [_indent(block) for block in blocks], role_color)

    if incremental:
      output = []
      for message in messages:
        call_id = message.get('call_id')
        if message.get('type') == 'tool_result' and call_id in previously_rendered_calls:
          result = _tool_result_text(message.get('content'))
          rendered = result if len(result) > 0 else '(empty)'
          output.append(_indent(f'{self.colors.dim}← {rendered}{self.colors.reset}'))
      return output
    return []

  def _assistant_block(self, message: dict) -> Optional[str]:
    message_type = message.get('type')
    if message_type == 'reasoning':
      heading = f'{self.colors.dim}[thinking]{self.colors.reset}'
      text = _message_text(message.get('content'))
      if len(text) == 0:
        return heading
      lines = [heading]
      lines.extend(f'{self.colors.dim}  {line}{self.colors.reset}' for line in text.splitlines())
      return '\n'.join(lines)
    if message_type == 'assistant':
      text = _message_text(message.get('content'))
      return text if len(text) > 0 else None
    if message_type == 'error':
      text = _message_text(message.get('content'))
      return text if len(text) > 0 else '(no detail)'
    if message_type != 'tool_call':
      return None
    call_id = message.get('call_id')
    if isinstance(call_id, str):
      self.rendered_call_ids.add(call_id)
    heading = f'{self.colors.cyan}→ {_format_tool_call(message)}{self.colors.reset}'
    result = self.tool_results.get(call_id) if isinstance(call_id, str) else None
    if result is None:
      return heading
    lines = [heading]
    if len(result) == 0:
      lines.append(f'{self.colors.dim}  (empty){self.colors.reset}')
    else:
      lines.extend(f'{self.colors.dim}  {line}{self.colors.reset}' for line in result.splitlines())
    return '\n'.join(lines)

  def _turn(self, role: str, message: dict, blocks: list[str], role_color: str) -> list[str]:
    self.turn += 1
    markers = []
    if message.get('isSidechain') is True:
      markers.append('sub')
    if message.get('isMeta') is True:
      markers.append('meta')
    tag = ''.join(f'[{marker}] ' for marker in markers)
    timestamp = _format_timestamp(message.get('ts'))
    heading = (
      f'\n{self.colors.bold}#{self.turn}{self.colors.reset} '
      f'{role_color}{tag}{role}{self.colors.reset} {self.colors.dim}{timestamp}{self.colors.reset}'
    )
    return [heading, *blocks]


def _step_is_after(candidate: str | int, bound: str | int) -> bool:
  try:
    return int(candidate) > int(bound)
  except (TypeError, ValueError):
    return str(candidate) > str(bound)


def _segment_messages(client: TrailsClient, trail_id: str, bound: Optional[dict]) -> list[dict]:
  messages: list[dict] = []
  if bound is None:
    return list(client.iter_messages(trail_id))
  bound_step_id = bound['step_id']
  bound_index = bound.get('index')
  reached_step = False
  for message in client.iter_messages(trail_id):
    source = message['source']
    step_id = source['step_id']
    if str(step_id) == str(bound_step_id):
      source_index = source.get('index', 0)
      if bound_index is not None and source_index > bound_index:
        return messages
      messages.append(message)
      reached_step = True
      continue
    if reached_step or _step_is_after(step_id, bound_step_id):
      break
    messages.append(message)
  return messages


def _format_context(records: Any, colors: Colors) -> Optional[str]:
  if not isinstance(records, list) or len(records) == 0:
    return None
  output = [f'{colors.bold}SESSION CONTEXT{colors.reset}']
  for record in records:
    if not isinstance(record, dict):
      continue
    title_value = record.get('title')
    title = (
      title_value
      if isinstance(title_value, str) and len(title_value) > 0
      else f'{record.get("kind", "?")}/{record.get("subtype", "?")}'
    )
    output.append(f'{colors.yellow}▸ {title}{colors.reset}')
    content = record.get('content')
    fields = record.get('fields')
    if isinstance(content, str) and len(content) > 0:
      output.extend(f'{colors.dim}  {line}{colors.reset}' for line in content.splitlines())
    elif isinstance(fields, dict):
      for key, value in fields.items():
        rendered = ', '.join(str(item) for item in value) if isinstance(value, list) else str(value)
        output.append(f'{colors.dim}  {key}{colors.reset} {rendered}')
  output.append(colors.dim + ('─' * 78) + colors.reset)
  return '\n'.join(output)


def _render_conversation(
  client: TrailsClient, trail: dict, colors: Colors
) -> tuple[str, _ConversationTimeline, Optional[str | int]]:
  segments = walk_header_chain(trail, client.get_trail)
  message_lists = [_segment_messages(client, header['id'], bound) for header, bound in segments]
  all_messages = [message for messages in message_lists for message in messages]
  timeline = _ConversationTimeline(colors, _index_tool_results(all_messages))
  output = [_format_header(trail, colors)]
  context = _format_context(client.get_launch_context(trail['id']), colors)
  if context is not None:
    output.append(context)
  for index, ((header, _), messages) in enumerate(zip(segments, message_lists, strict=True)):
    if index > 0:
      segment = str(header.get('native', {}).get('segment', '?'))[:8]
      output.append(
        f'\n{colors.dim}── resumed as trail {header["id"]} '
        f'(segment {segment}) · {_format_timestamp(header.get("started_at"))} ──{colors.reset}'
      )
    output.extend(timeline.render(messages))
  target_messages = message_lists[-1]
  after = _source_step_id(target_messages[-1]) if len(target_messages) > 0 else None
  return '\n'.join(output) + '\n', timeline, after


# --- follow and views --------------------------------------------------------------


def _follow_batches(
  client: TrailsClient,
  trail_id: str,
  *,
  iterator: Callable[[str, Optional[str | int]], Iterator[dict]],
  cursor: Callable[[dict], str | int],
  terminal: Callable[[dict], bool],
  interval: float,
  after: Optional[str | int] = None,
  sleep: Callable[[float], None] = time.sleep,
) -> Iterator[list[dict]]:
  while True:
    try:
      rows = list(iterator(trail_id, after))
      if len(rows) > 0:
        terminal_index = next((index for index, row in enumerate(rows) if terminal(row)), None)
        emitted = rows if terminal_index is None else rows[: terminal_index + 1]
        after = cursor(emitted[-1])
        yield emitted
        if terminal_index is not None:
          return
      if client.get_trail(trail_id).get('end') is not None:
        drained = list(iterator(trail_id, after))
        if len(drained) > 0:
          yield drained
        return
    except HTTPStatusError as exception:
      if not is_retryable_status(exception.status):
        raise
      log.warning('transient trails-server error, retrying: %s', exception)
    except (OSError, http.client.HTTPException) as exception:
      log.warning('transient trails-server error, retrying: %s', exception)
    sleep(interval)


def _show_or_follow(
  client: TrailsClient,
  args: dict,
  initial: str,
  after: Optional[str | int],
  *,
  iterator: Callable[[str, Optional[str | int]], Iterator[dict]],
  cursor: Callable[[dict], str | int],
  terminal: Callable[[dict], bool],
  render_batch: Callable[[list[dict]], str],
) -> int:
  if not bool(args.get('follow', False)):
    _emit(initial, args)
    return 0
  sys.stdout.write(initial)
  sys.stdout.flush()
  try:
    for rows in _follow_batches(
      client,
      args['trail_id'],
      iterator=iterator,
      cursor=cursor,
      terminal=terminal,
      interval=args['interval'],
      after=after,
    ):
      rendered = render_batch(rows)
      if len(rendered) > 0:
        sys.stdout.write(rendered)
        sys.stdout.flush()
  except KeyboardInterrupt:
    return 130
  return 0


def _command_show(client: TrailsClient, args: dict, colors: Colors) -> int:
  trail = client.get_trail(args['trail_id'])
  initial, timeline, after = _render_conversation(client, trail, colors)

  def render_batch(messages: list[dict]) -> str:
    lines = timeline.render(messages, incremental=True)
    return '\n'.join(lines) + ('\n' if len(lines) > 0 else '')

  return _show_or_follow(
    client,
    args,
    initial,
    after,
    iterator=lambda trail_id, cursor: client.iter_messages(trail_id, after=cursor),
    cursor=_source_step_id,
    terminal=lambda message: False,
    render_batch=render_batch,
  )


def _command_steps(client: TrailsClient, args: dict, colors: Colors) -> int:
  trail = client.get_trail(args['trail_id'])
  initial, after = _render_native_trail(client, trail, colors)
  return _show_or_follow(
    client,
    args,
    initial,
    after,
    iterator=lambda trail_id, cursor: client.iter_steps(trail_id, after=cursor),
    cursor=lambda step: step['step_id'],
    terminal=lambda step: step.get('kind') == 'end',
    render_batch=lambda steps: (
      '\n'.join(_format_step_summary(step, colors) for step in steps) + '\n'
    ),
  )


# --- grep -------------------------------------------------------------------------


def _grep_lines(
  name: str, text: str, regex: re.Pattern[str], colors: Colors, before: int = 0, after: int = 0
) -> list[str]:
  """Return grep-style matching lines and optional context."""

  def highlight(match: re.Match[str]) -> str:
    return f'{colors.bold}{colors.red}{match.group(0)}{colors.reset}'

  lines = text.splitlines()
  match_indexes = {index for index, line in enumerate(lines) if regex.search(line) is not None}
  shown: set[int] = set()
  for index in match_indexes:
    shown.update(range(max(index - before, 0), min(index + after + 1, len(lines))))

  has_context = before > 0 or after > 0
  output: list[str] = []
  previous: Optional[int] = None
  for index in sorted(shown):
    if has_context and previous is not None and index > previous + 1:
      output.append(f'{colors.cyan}--{colors.reset}')
    previous = index
    line = lines[index]
    if index in match_indexes:
      separator = f'{colors.cyan}:{colors.reset}'
      if colors.enabled:
        line = regex.sub(highlight, line)
    else:
      separator = f'{colors.cyan}-{colors.reset}'
    output.append(
      f'{colors.magenta}{name}{colors.reset}{separator}'
      f'{colors.green}{index + 1}{colors.reset}{separator}{line}'
    )
  return output


def _command_grep(client: TrailsClient, args: dict, colors: Colors) -> int:
  """Exit 0 when at least one line matched, 1 otherwise — like grep."""
  try:
    regex = re.compile(args['pattern'], re.IGNORECASE if args.get('ignore_case', False) else 0)
  except re.error as exception:
    raise SystemExit(f'invalid pattern {args["pattern"]!r}: {exception}') from exception

  context_value: Optional[int] = args.get('context')
  default_context: int = context_value if context_value is not None else 0
  after_value: Optional[int] = args.get('after_context')
  after: int = after_value if after_value is not None else default_context
  before_value: Optional[int] = args.get('before_context')
  before: int = before_value if before_value is not None else default_context
  if before < 0 or after < 0:
    raise SystemExit('context lengths must be non-negative')
  has_context = before > 0 or after > 0

  trail_ids: list[str] = args.get('trails', [])
  if len(trail_ids) > 0:
    headers = [client.get_trail(trail_id) for trail_id in trail_ids]
  else:
    headers = list(client.iter_trails(harness=args.get('harness'), max_items=args.get('limit')))

  log.info('searching %d trails', len(headers))
  found = False
  plain = Colors(False)
  for header in headers:
    rendered, _, _ = _render_conversation(client, header, plain)
    matches = _grep_lines(header['id'], rendered, regex, colors, before=before, after=after)
    if len(matches) > 0:
      if found and has_context:
        sys.stdout.write(f'{colors.cyan}--{colors.reset}\n')
      found = True
      sys.stdout.write('\n'.join(matches) + '\n')
      sys.stdout.flush()
  return 0 if found else 1


# --- tree -------------------------------------------------------------------------


def _command_tree(client: TrailsClient, args: dict, colors: Colors) -> int:
  trail_id = args['trail_id']
  start = client.get_trail(trail_id)

  root = walk_header_chain(start, client.get_trail)[0][0]

  lines: list[str] = []
  _render_tree(client, root, '', is_last=True, lines=lines, colors=colors, highlight=trail_id)
  sys.stdout.write('\n'.join(lines) + '\n')
  return 0


def _render_tree(
  client: TrailsClient,
  trail: dict,
  prefix: str,
  *,
  is_last: bool,
  lines: list[str],
  colors: Colors,
  highlight: str,
) -> None:
  trail_id = trail['id']
  connector = '└── ' if is_last else '├── '
  marker = f' {colors.bold}<-- here{colors.reset}' if trail_id == highlight else ''
  model = trail.get('native', {}).get('llm', {}).get('model', '?')
  owner = _who(trail)
  forked_from_step = ''
  forked_from = trail.get('forked_from')
  if forked_from is not None:
    step_id = str(forked_from['step_id'])[:_STEP_ID_DISPLAY_CHARS]
    forked_from_step = f' {colors.dim}@step {step_id}{colors.reset}'
  lines.append(
    f'{prefix}{connector}{colors.yellow}{trail_id}{colors.reset}  '
    f'{colors.cyan}{owner}{colors.reset}/{colors.dim}{model}{colors.reset}'
    f'{forked_from_step}{marker}'
  )
  children = list(client.iter_trails(forked_from=trail_id))
  children.reverse()
  child_prefix = prefix + ('    ' if is_last else '│   ')
  for index, child in enumerate(children):
    child_is_last = index == len(children) - 1
    _render_tree(
      client,
      child,
      child_prefix,
      is_last=child_is_last,
      lines=lines,
      colors=colors,
      highlight=highlight,
    )


# --- CLI --------------------------------------------------------------------------


_COMMANDS = ('list', 'show', 'steps', 'grep', 'tree')


def _with_default_command(argv: list[str]) -> list[str]:
  """Default the subcommand to `show`, so `rewind <trail-id>` keeps working."""
  remaining = argv[1:]
  if len(remaining) == 0 or remaining[0] in _COMMANDS or remaining[0] in ('-h', '--help'):
    return argv
  return [argv[0], 'show', *remaining]


def _add_color_argument(parser: base.args.Parser) -> None:
  parser.add_argument(
    '--color',
    default='auto',
    choices=['auto', 'always', 'never'],
    help='color output (default: auto = on if stdout is a TTY and NO_COLOR is unset)',
  )


def _add_view_arguments(parser: base.args.Parser) -> None:
  parser.add_argument('trail_id', help='trail id (or a legacy claude session id)')
  parser.add_argument('--no-pager', action='store_true', help='do not pipe output through a pager')
  parser.add_argument(
    '-f',
    '--follow',
    action='store_true',
    help='keep polling and render new records as they arrive, like tail -f; '
    'exits once the trail ends (no pager)',
  )
  parser.add_argument(
    '--interval', type=float, default=2.0, help='seconds between polls with --follow'
  )
  _add_color_argument(parser)


def main(argv: list[str]) -> Optional[int]:
  parser = base.args.Parser(
    description='read recorded runs across harnesses; `rewind <trail-id>` means '
    '`rewind show <trail-id>`'
  )
  subparsers = parser.add_subparsers(dest='command')

  list_parser = subparsers.add_parser('list', help='list trail headers, newest first')
  list_parser.add_argument('--harness', help='filter by harness (bro, claude)')
  list_parser.add_argument('--bro', help='filter by bro name')
  list_parser.add_argument('--since', help='ISO timestamp lower bound on started_at')
  list_parser.add_argument('--until', help='ISO timestamp upper bound on started_at')
  list_parser.add_argument('--forked-from', help='list forks of this trail id')
  list_parser.add_argument('--limit', type=int, default=50, help='max trails to list')
  list_parser.add_argument(
    '--no-pager', action='store_true', help='do not pipe output through a pager'
  )
  _add_color_argument(list_parser)
  list_parser.set_handler(lambda **args: _dispatch(_command_list, args))

  show_parser = subparsers.add_parser(
    'show', help='render one generalized conversation (the default command)'
  )
  _add_view_arguments(show_parser)
  show_parser.set_handler(lambda **args: _dispatch(_command_show, args))

  steps_parser = subparsers.add_parser('steps', help='render one trail native record stream')
  _add_view_arguments(steps_parser)
  steps_parser.set_handler(lambda **args: _dispatch(_command_steps, args))

  grep_parser = subparsers.add_parser(
    'grep', help='grep rendered conversations as files: <id>:<line>:<text>'
  )
  grep_parser.add_argument('pattern', help='Python regular expression to search for')
  grep_parser.add_argument(
    'trails', nargs='*', help='trail ids to search (default: every trail, newest first)'
  )
  grep_parser.add_argument('--harness', help='filter by harness when searching every trail')
  grep_parser.add_argument('-i', '--ignore-case', action='store_true', help='ignore case')
  grep_parser.add_argument(
    '-A', '--after-context', type=int, metavar='N', help='print N lines of trailing context'
  )
  grep_parser.add_argument(
    '-B', '--before-context', type=int, metavar='N', help='print N lines of leading context'
  )
  grep_parser.add_argument(
    '-C',
    '--context',
    type=int,
    metavar='N',
    help='print N lines of leading and trailing context (-A / -B take precedence)',
  )
  grep_parser.add_argument(
    '--limit', type=int, default=None, help='max trails to search (default: all)'
  )
  _add_color_argument(grep_parser)
  grep_parser.set_handler(lambda **args: _dispatch(_command_grep, args))

  tree_parser = subparsers.add_parser(
    'tree', help='render the forked_from/fork hierarchy reachable from a trail'
  )
  tree_parser.add_argument('trail_id')
  _add_color_argument(tree_parser)
  tree_parser.set_handler(lambda **args: _dispatch(_command_tree, args))

  return parser.dispatch(_with_default_command(argv))


def _dispatch(command: Callable[[TrailsClient, dict, Colors], int], args: dict) -> int:
  colors = Colors(should_color(args['color']))
  with default_client() as client:
    return command(client, args, colors)
