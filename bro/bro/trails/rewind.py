#!/usr/bin/env python
"""`rewind` — the single reader over recorded runs, every harness.

Four subcommands against the deployed `trails-server` (config from the `trails`
secret):

- `rewind list` — cross-harness listing of trail headers, newest first, paged
  through `$PAGER`; filters mirror the server's indexed selectors.
- `rewind show <trail-id>` — the default command (`rewind <id>` means
  `rewind show <id>`), harness-aware: a bro trail renders as its header plus
  the step listing; a claude trail renders as a human-readable conversation —
  the fork chain is walked through each parent's anchor so the whole
  conversation reads as one timeline, with the trail's stored launch context as
  a SESSION CONTEXT preamble. `-f` keeps polling and renders new records as
  they land, like `tail -f`.
- `rewind grep <pattern> [trail-id ...]` — greps rendered timelines as if they
  were files: `<id>:<line>:<text>`. With no ids it searches every trail,
  newest first (optionally filtered by `--harness`).
- `rewind tree <trail-id>` — the forked_from/fork hierarchy reachable from a
  trail.

Historical bro trails recorded before the `terminal`→`ok` end-reason rename
carry `{reason: 'terminal'}` end steps; the renderer maps the value rather than
rewriting stored steps.
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
  s = int(seconds)
  if s < 60:
    return f'{s}s'
  if s < 3600:
    return f'{s // 60}m {s % 60}s'
  h, remainder = divmod(s, 3600)
  return f'{h}h {remainder // 60}m'


def _parse_iso(value: str) -> Optional[datetime.datetime]:
  try:
    return datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
  except ValueError:
    return None


def _truncate_oneline(body: Any, limit: int = _BODY_TRUNCATE_CHARS) -> str:
  """render a step body to a single line, capped at `limit` chars with a
  `... <N more chars>` marker pointing at the part that got dropped."""
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


def _indent(s: str, prefix: str = '  ') -> str:
  return '\n'.join(prefix + line for line in s.splitlines())


# --- listing ----------------------------------------------------------------------


def _format_trail_row(trail: dict, colors: Colors) -> str:
  trail_id = trail.get('id', '?')
  harness = trail.get('harness', '?')
  if harness == 'claude':
    who = trail.get('location', {}).get('workspace', '?')
  else:
    who = trail.get('bro', '?')
  who = who if who is not None else '?'
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
    f'{colors.cyan}{who:<10}{colors.reset}  '
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
  if sys.stdout.isatty() and not args.get('no_pager', False):
    pager.page(text)
  else:
    sys.stdout.write(text)


# --- bro rendering ----------------------------------------------------------------


def _format_step_summary(step: dict, colors: Colors) -> str:
  kind = step.get('kind', '?')
  step_id = step.get('step_id', '?')
  timestamp = _format_timestamp(step.get('ts'))
  turn = step.get('turn_index')
  turn_str = f't{turn} ' if turn is not None else ''
  prefix = (
    f'{colors.yellow}{step_id}{colors.reset}  '
    f'{colors.dim}{timestamp}{colors.reset}  {colors.yellow}{turn_str}{kind:<14}{colors.reset}'
  )

  body = step.get('body')
  if kind == 'end' and isinstance(body, dict) and body.get('reason') == 'terminal':
    # historical end steps predate the terminal→ok rename; map at render
    body = {**body, 'reason': 'ok'}
  spilled = spill_descriptor(body)
  if spilled is not None:
    size = spilled.get('size', '?')
    url = spilled.get('url', '-')
    summary = f'{colors.dim}<{size} bytes spilled>{colors.reset} {url}'
  else:
    summary = _truncate_oneline(body)

  extras_parts: list[str] = []
  for key in (
    'tool_name',
    'call_id',
    'is_error',
    'response_id',
    'tokens_in',
    'tokens_out',
    'tokens_reasoning',
    'tokens_cached',
  ):
    if key in step:
      extras_parts.append(f'{colors.cyan}{key}{colors.reset}={step[key]}')
  arguments = step.get('arguments')
  if arguments is not None:
    extras_parts.append(f'{colors.cyan}args{colors.reset}={_truncate_oneline(arguments, 80)}')

  parts = [prefix]
  if len(summary) > 0:
    parts.append(summary)
  if len(extras_parts) > 0:
    parts.append(f'[{" ".join(extras_parts)}]')
  return '  '.join(parts)


def _format_bro_header(trail: dict, colors: Colors) -> str:
  native = trail.get('native', {})
  spec = native.get('llm', {})
  end = trail.get('end')
  forked_from = trail.get('forked_from')
  lines = [
    f'{colors.bold}trail     {colors.reset} {trail.get("id")}',
    f'{colors.dim}harness   {colors.reset} {trail.get("harness")}',
    f'{colors.dim}bro       {colors.reset} {trail.get("bro")} (version {trail.get("version")})',
    f'{colors.dim}llm       {colors.reset} {json.dumps(spec, ensure_ascii=False)}',
    f'{colors.dim}started   {colors.reset} {_format_timestamp(trail.get("started_at"))}',
    f'{colors.dim}ended     {colors.reset} '
    f'{_format_timestamp(end.get("at") if end is not None else None)}  '
    f'({end.get("reason") if end is not None else None})',
    f'{colors.dim}interactive{colors.reset} {trail.get("interactive")}  '
    f'{colors.dim}surface{colors.reset} {trail.get("surface")}',
  ]
  if forked_from is not None:
    lines.append(
      f'{colors.dim}forked from{colors.reset} '
      f'{forked_from.get("trail_id")} @ step {forked_from.get("step_id")}'
    )
  summoned_by = trail.get('summoned_by')
  if summoned_by is not None:
    lines.append(
      f'{colors.dim}summoned_by  {colors.reset} {json.dumps(summoned_by, ensure_ascii=False)}'
    )
  lines.append(f'{colors.dim}turns     {colors.reset} {trail.get("turn_count")}')
  lines.append(
    f'{colors.dim}usage     {colors.reset} {json.dumps(trail.get("usage", {}), ensure_ascii=False)}'
  )
  lines.append(
    f'{colors.dim}models    {colors.reset} '
    f'{json.dumps(trail.get("models", []), ensure_ascii=False)}'
  )
  counts = native.get('step_counts_by_kind')
  if counts is not None:
    parts = ', '.join(f'{k}={v}' for k, v in counts.items() if v > 0)
    lines.append(f'{colors.dim}step kinds{colors.reset} {parts}')
  lines.append(colors.dim + ('─' * 78) + colors.reset)
  return '\n'.join(lines)


def _follow_steps(
  client: TrailsClient,
  trail_id: str,
  *,
  interval: float,
  after: Optional[str] = None,
  sleep: Callable[[float], None] = time.sleep,
) -> Iterator[dict]:
  """yield the trail's steps past `after`, then keep polling for new ones every
  `interval` seconds — `tail -f` over a trail's native stream.

  terminates once an `end` step arrives (bro) or — for a harness without end
  steps, or a trail that never got one — once an idle poll finds `end` set on
  the header; a still-live trail is followed until interrupted. transient
  failures (network blips, 5xx / 429) are logged and retried on the next tick;
  a deterministic 4xx propagates.
  """
  while True:
    try:
      for row in client.iter_steps(trail_id, after=after):
        after = row['step_id']
        yield row
        if row.get('kind') == 'end':
          return
      if client.get_trail(trail_id).get('end') is not None:
        # the end may have committed between the steps poll and this header
        # read — drain once more so its records are not dropped
        yield from client.iter_steps(trail_id, after=after)
        return
    except HTTPStatusError as exception:
      if not is_retryable_status(exception.status):
        raise
      log.warning('transient trails-server error, retrying: %s', exception)
    except (OSError, http.client.HTTPException) as exception:
      log.warning('transient trails-server error, retrying: %s', exception)
    sleep(interval)


# --- claude rendering -------------------------------------------------------------


def _index_tool_results(entries: list[dict]) -> dict[str, str]:
  """tool_use_id → flattened tool_result text."""
  out: dict[str, str] = {}
  for entry in entries:
    if entry.get('type') != 'user':
      continue
    content = entry.get('message', {}).get('content')
    if not isinstance(content, list):
      continue
    for block in content:
      if not isinstance(block, dict) or block.get('type') != 'tool_result':
        continue
      tool_use_id = block.get('tool_use_id')
      if not isinstance(tool_use_id, str):
        continue
      body: Any = block.get('content')
      if isinstance(body, list):
        body = '\n'.join(
          x.get('text', '') for x in body if isinstance(x, dict) and x.get('type') == 'text'
        )
      out[tool_use_id] = '' if body is None else str(body)
  return out


def _user_text(content: Any) -> Optional[str]:
  """text of a user turn that isn't a tool_result; None if there is none."""
  if isinstance(content, str):
    s = content.strip()
    return s if len(s) > 0 else None
  if not isinstance(content, list):
    return None
  parts = [c.get('text', '') for c in content if isinstance(c, dict) and c.get('type') == 'text']
  joined = '\n'.join(parts).strip()
  return joined if len(joined) > 0 else None


def _format_tool_use(block: dict) -> str:
  name = block.get('name', '?')
  arguments = block.get('input', {})
  return f'{name}({json.dumps(arguments, separators=(", ", ": "))})'


def _assistant_blocks(content: Any, tool_results: dict[str, str], colors: Colors) -> list[str]:
  if not isinstance(content, list):
    return []
  out: list[str] = []
  for block in content:
    if not isinstance(block, dict):
      continue
    block_type = block.get('type')
    if block_type == 'text':
      text = block.get('text', '').strip()
      if len(text) > 0:
        out.append(text)
    elif block_type == 'thinking':
      head = f'{colors.dim}[thinking]{colors.reset}'
      text = block.get('thinking', '').strip()
      if len(text) == 0:
        out.append(head)
      else:
        lines = [head]
        for line in text.splitlines():
          lines.append(f'{colors.dim}  {line}{colors.reset}')
        out.append('\n'.join(lines))
    elif block_type == 'tool_use':
      tool_use_id = block.get('id', '')
      head = f'{colors.cyan}→ {_format_tool_use(block)}{colors.reset}'
      result = tool_results.get(tool_use_id)
      if result is None:
        out.append(head)
      else:
        lines = [head]
        if len(result) == 0:
          lines.append(f'{colors.dim}  (empty){colors.reset}')
        else:
          for line in result.splitlines():
            lines.append(f'{colors.dim}  {line}{colors.reset}')
        out.append('\n'.join(lines))
  return out


def _format_claude_header(trail: dict, colors: Colors) -> str:
  native = trail.get('native', {})
  location = trail.get('location', {})
  started = trail.get('started_at', '')
  end = trail.get('end')
  duration = ''
  start_moment = _parse_iso(started) if len(started) > 0 else None
  end_moment = _parse_iso(end['at']) if end is not None and 'at' in end else None
  if start_moment is not None and end_moment is not None:
    duration = f', {_format_duration((end_moment - start_moment).total_seconds())}'
  status = 'live' if end is None else f'{end.get("reason")}{duration}'
  models = trail.get('models', [])
  model = ', '.join(models) if len(models) > 0 else native.get('llm', {}).get('model', '?')
  lines = [
    f'{colors.bold}trail    {colors.reset} {trail.get("id")}',
    f'{colors.dim}workspace{colors.reset} {location.get("workspace", "?")}'
    f'    {colors.dim}bro{colors.reset} {trail.get("bro")}'
    f'    {colors.dim}host{colors.reset} {location.get("host", "?")}',
    f'{colors.dim}started  {colors.reset} {_format_timestamp(started)}  ({status})',
    f'{colors.dim}model    {colors.reset} {model}'
    f'    {colors.dim}claude-code{colors.reset} {native.get("harness_version", "?")}'
    f'    {colors.dim}lines{colors.reset} {native.get("line_count", "?")}'
    f'    {colors.dim}turns{colors.reset} {trail.get("turn_count", "?")}',
    f'{colors.dim}segment  {colors.reset} {native.get("segment", "?")}',
  ]
  subject = trail.get('subject')
  if subject is not None:
    lines.append(f'{colors.dim}subject  {colors.reset} {subject}')
  forked_from = trail.get('forked_from')
  if forked_from is not None:
    lines.append(
      f'{colors.dim}forked   {colors.reset} '
      f'from {forked_from.get("trail_id")} @ line {forked_from.get("step_id")}'
    )
  cw_command = native.get('cw_command')
  if cw_command is not None:
    lines.append(f'{colors.dim}cw       {colors.reset} {cw_command}')
  lines.append(colors.dim + ('─' * 78) + colors.reset)
  return '\n'.join(lines)


def _format_context(records: Any, colors: Colors) -> Optional[str]:
  """render the SESSION CONTEXT preamble from the trail's stored launch-context
  records (cw/session_context.py: each has a `title` plus either a `content`
  text block or a `fields` key/value map)."""
  if not isinstance(records, list) or len(records) == 0:
    return None
  out = [f'{colors.bold}SESSION CONTEXT{colors.reset}']
  for record in records:
    if not isinstance(record, dict):
      continue
    title = record.get('title') or f'{record.get("kind", "?")}/{record.get("subtype", "?")}'
    out.append(f'{colors.yellow}▸ {title}{colors.reset}')
    content = record.get('content')
    fields = record.get('fields')
    if isinstance(content, str) and len(content) > 0:
      for line in content.splitlines():
        out.append(f'{colors.dim}  {line}{colors.reset}')
    elif isinstance(fields, dict):
      for key, value in fields.items():
        rendered = ', '.join(str(v) for v in value) if isinstance(value, list) else str(value)
        out.append(f'{colors.dim}  {key}{colors.reset} {rendered}')
  out.append(colors.dim + ('─' * 78) + colors.reset)
  return '\n'.join(out)


class _ClaudeTimeline:
  """renders parsed claude records into the human-readable timeline, keeping
  the turn counter and tool-result index across chain segments."""

  def __init__(self, colors: Colors, tool_results: dict[str, str]):
    self.colors = colors
    self.tool_results = tool_results
    self.turn = 0

  def render(self, entry: dict) -> list[str]:
    colors = self.colors
    entry_type = entry.get('type')
    if entry_type not in ('user', 'assistant'):
      return []
    content = entry.get('message', {}).get('content')

    if entry_type == 'user' and isinstance(content, list) and len(content) > 0:
      if all(isinstance(c, dict) and c.get('type') == 'tool_result' for c in content):
        return []

    timestamp = entry.get('timestamp', '')[:19].replace('T', ' ')
    markers = []
    if entry.get('isSidechain') is True:
      markers.append('sub')
    if entry.get('isMeta') is True:
      markers.append('meta')
    tag = ''.join(f'[{m}] ' for m in markers)

    if entry_type == 'user':
      text = _user_text(content)
      if text is None:
        return []
      self.turn += 1
      role = f'{colors.blue}{tag}USER{colors.reset}'
      head = (
        f'\n{colors.bold}#{self.turn}{colors.reset} {role} {colors.dim}{timestamp}{colors.reset}'
      )
      return [head, _indent(text)]
    blocks = _assistant_blocks(content, self.tool_results, colors)
    if len(blocks) == 0:
      return []
    self.turn += 1
    role = f'{colors.green}{tag}ASSISTANT{colors.reset}'
    head = f'\n{colors.bold}#{self.turn}{colors.reset} {role} {colors.dim}{timestamp}{colors.reset}'
    return [head, *(_indent(block) for block in blocks)]


def _chain(client: TrailsClient, trail: dict) -> list[tuple[dict, Optional[int]]]:
  """the fork ancestry of `trail`, root first: (header, bound) pairs where
  `bound` is the last step index the next trail's fork carries (inclusive);
  the target trail itself is unbounded."""
  segments: list[tuple[dict, Optional[int]]] = [(trail, None)]
  current = trail
  while True:
    forked_from = current.get('forked_from')
    if forked_from is None:
      break
    current = client.get_trail(forked_from['trail_id'])
    segments.append((current, int(forked_from['step_id'])))
  segments.reverse()
  return segments


def _segment_entries(client: TrailsClient, trail_id: str, bound: Optional[int]) -> list[dict]:
  entries: list[dict] = []
  for row in client.iter_steps(trail_id):
    if bound is not None and int(row['step_id']) > bound:
      break
    record = row.get('record')
    if isinstance(record, dict):
      entries.append(record)
  return entries


def _render_claude_trail(client: TrailsClient, trail: dict, colors: Colors) -> str:
  out = [_format_claude_header(trail, colors)]
  context = _format_context(client.get_launch_context(trail['id']), colors)
  if context is not None:
    out.append(context)
  segments = _chain(client, trail)
  entry_lists = [_segment_entries(client, header['id'], bound) for header, bound in segments]
  timeline = _ClaudeTimeline(
    colors, _index_tool_results([entry for entries in entry_lists for entry in entries])
  )
  for (header, _), entries in zip(segments, entry_lists, strict=True):
    if header['id'] != segments[0][0]['id']:
      segment_id = str(header.get('native', {}).get('segment', '?'))[:8]
      out.append(
        f'\n{colors.dim}── resumed as trail {header["id"]} '
        f'(segment {segment_id}) · {_format_timestamp(header.get("started_at"))} ──{colors.reset}'
      )
    for entry in entries:
      out.extend(timeline.render(entry))
  return '\n'.join(out) + '\n'


# --- show -------------------------------------------------------------------------


def _command_show(client: TrailsClient, args: dict, colors: Colors) -> int:
  trail_id = args['trail_id']
  header = client.get_trail(trail_id)
  if header.get('harness') == 'claude':
    return _show_claude(client, header, args, colors)
  return _show_bro(client, header, args, colors)


def _show_bro(client: TrailsClient, header: dict, args: dict, colors: Colors) -> int:
  trail_id = header['id']
  if bool(args.get('follow', False)):
    print(_format_bro_header(header, colors), flush=True)
    try:
      for row in _follow_steps(client, trail_id, interval=args['interval']):
        print(_format_step_summary(row, colors), flush=True)
    except KeyboardInterrupt:
      return 130
    return 0
  out: list[str] = [_format_bro_header(header, colors)]
  for row in client.iter_steps(trail_id):
    out.append(_format_step_summary(row, colors))
  _emit('\n'.join(out) + '\n', args)
  return 0


def _show_claude(client: TrailsClient, header: dict, args: dict, colors: Colors) -> int:
  trail_id = header['id']
  if not bool(args.get('follow', False)):
    _emit(_render_claude_trail(client, header, colors), args)
    return 0
  sys.stdout.write(_render_claude_trail(client, header, colors))
  sys.stdout.flush()
  # live tail: new records render as they land; tool results that arrive after
  # their call are shown as standalone dim blocks rather than inlined
  last = str(header.get('native', {}).get('line_count', 0) - 1)
  timeline = _ClaudeTimeline(colors, {})
  try:
    for row in _follow_steps(
      client, trail_id, interval=args['interval'], after=last if int(last) >= 0 else None
    ):
      record = row.get('record')
      if not isinstance(record, dict):
        continue
      for line in timeline.render(record):
        print(line, flush=True)
      for tool_use_id, result in _index_tool_results([record]).items():
        del tool_use_id
        body = result if len(result) > 0 else '(empty)'
        print(_indent(f'{colors.dim}← {_truncate_oneline(body)}{colors.reset}'), flush=True)
  except KeyboardInterrupt:
    return 130
  return 0


# --- grep -------------------------------------------------------------------------


def _grep_lines(
  name: str, text: str, regex: re.Pattern[str], colors: Colors, before: int = 0, after: int = 0
) -> list[str]:
  """matching lines of `text` in grep -n style: <name>:<line>:<text>, plus
  `before`/`after` context lines (<name>-<line>-<text>, groups separated by --)."""

  def highlight(match: re.Match[str]) -> str:
    return f'{colors.bold}{colors.red}{match.group(0)}{colors.reset}'

  lines = text.splitlines()
  match_indexes = {index for index, line in enumerate(lines) if regex.search(line) is not None}
  shown: set[int] = set()
  for index in match_indexes:
    shown.update(range(max(index - before, 0), min(index + after + 1, len(lines))))

  has_context = before > 0 or after > 0
  out: list[str] = []
  previous: Optional[int] = None
  for index in sorted(shown):
    if has_context and previous is not None and index > previous + 1:
      out.append(f'{colors.cyan}--{colors.reset}')
    previous = index
    line = lines[index]
    if index in match_indexes:
      separator = f'{colors.cyan}:{colors.reset}'
      if colors.enabled:
        line = regex.sub(highlight, line)
    else:
      separator = f'{colors.cyan}-{colors.reset}'
    out.append(
      f'{colors.magenta}{name}{colors.reset}{separator}'
      f'{colors.green}{index + 1}{colors.reset}{separator}{line}'
    )
  return out


def _render_own_timeline(client: TrailsClient, header: dict) -> str:
  """one trail's own rendered text (no chain walk, no launch context, no
  colors) — the haystack a grep searches."""
  plain = Colors(False)
  if header.get('harness') != 'claude':
    out = [_format_bro_header(header, plain)]
    out.extend(_format_step_summary(row, plain) for row in client.iter_steps(header['id']))
    return '\n'.join(out) + '\n'
  entries = _segment_entries(client, header['id'], None)
  timeline = _ClaudeTimeline(plain, _index_tool_results(entries))
  out = [_format_claude_header(header, plain)]
  for entry in entries:
    out.extend(timeline.render(entry))
  return '\n'.join(out) + '\n'


def _command_grep(client: TrailsClient, args: dict, colors: Colors) -> int:
  """exit 0 when at least one line matched, 1 otherwise — like grep."""
  try:
    regex = re.compile(args['pattern'], re.IGNORECASE if args.get('ignore_case', False) else 0)
  except re.error as e:
    raise SystemExit(f'invalid pattern {args["pattern"]!r}: {e}')

  context_value: Optional[int] = args.get('context')
  default_context: int = context_value if context_value is not None else 0
  after_value: Optional[int] = args.get('after_context')
  after: int = after_value if after_value is not None else default_context
  before_value: Optional[int] = args.get('before_context')
  before: int = before_value if before_value is not None else default_context
  if before < 0 or after < 0:
    raise SystemExit('context lengths must be non-negative')
  has_context = before > 0 or after > 0

  ids: list[str] = args.get('trails', [])
  if len(ids) > 0:
    headers = [client.get_trail(trail_id) for trail_id in ids]
  else:
    headers = list(client.iter_trails(harness=args.get('harness'), max_items=args.get('limit')))

  log.info('searching %d trails', len(headers))
  found = False
  for header in headers:
    matches = _grep_lines(
      header['id'],
      _render_own_timeline(client, header),
      regex,
      colors,
      before=before,
      after=after,
    )
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

  # walk upward to the root so the tree displays the full ancestry rather than
  # just the children of the named trail. cycles are not possible (forked_from
  # pointers are set only at trail creation), so plain ascent is safe.
  root = start
  while True:
    forked_from = root.get('forked_from')
    if forked_from is None:
      break
    root = client.get_trail(forked_from['trail_id'])

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
  if trail.get('harness') == 'claude':
    who = trail.get('location', {}).get('workspace', '?')
  else:
    who = trail.get('bro', '?')
  forked_from_step = ''
  forked_from = trail.get('forked_from')
  if forked_from is not None:
    step_id = forked_from['step_id'][:_STEP_ID_DISPLAY_CHARS]
    forked_from_step = f' {colors.dim}@step {step_id}{colors.reset}'
  lines.append(
    f'{prefix}{connector}{colors.yellow}{trail_id}{colors.reset}  '
    f'{colors.cyan}{who}{colors.reset}/{colors.dim}{model}{colors.reset}'
    f'{forked_from_step}{marker}'
  )
  children = list(client.iter_trails(forked_from=trail_id))
  # iter_trails returns newest-first; reverse so the tree reads oldest-first
  # under each node, matching how a reader would build it up mentally.
  children.reverse()
  child_prefix = prefix + ('    ' if is_last else '│   ')
  for i, child in enumerate(children):
    last = i == len(children) - 1
    _render_tree(
      client, child, child_prefix, is_last=last, lines=lines, colors=colors, highlight=highlight
    )


# --- CLI --------------------------------------------------------------------------

_COMMANDS = ('list', 'show', 'grep', 'tree')


def _with_default_command(argv: list[str]) -> list[str]:
  """default the subcommand to `show`, so `rewind <trail-id>` keeps working.

  the subcommand must be the first argument (trail and legacy session ids never
  collide with a command name); help flags pass through so bare `rewind --help`
  documents every command."""
  rest = argv[1:]
  if len(rest) == 0 or rest[0] in _COMMANDS or rest[0] in ('-h', '--help'):
    return argv
  return [argv[0], 'show', *rest]


def _add_color_argument(parser: base.args.Parser) -> None:
  parser.add_argument(
    '--color',
    default='auto',
    choices=['auto', 'always', 'never'],
    help='color output (default: auto = on if stdout is a TTY and NO_COLOR is unset)',
  )


def main(argv: list[str]) -> Optional[int]:
  parser = base.args.Parser(
    description='read recorded runs across harnesses; `rewind <trail-id>` means '
    '`rewind show <trail-id>`'
  )
  sub = parser.add_subparsers(dest='command')

  list_parser = sub.add_parser('list', help='list trail headers, newest first')
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

  show_parser = sub.add_parser('show', help='render one trail (the default command); harness-aware')
  show_parser.add_argument('trail_id', help='trail id (or a legacy claude session id)')
  show_parser.add_argument(
    '--no-pager', action='store_true', help='do not pipe output through a pager'
  )
  show_parser.add_argument(
    '-f',
    '--follow',
    action='store_true',
    help='keep polling and render new records as they arrive, like tail -f; '
    'exits once the trail ends (no pager)',
  )
  show_parser.add_argument(
    '--interval', type=float, default=2.0, help='seconds between polls with --follow'
  )
  _add_color_argument(show_parser)
  show_parser.set_handler(lambda **args: _dispatch(_command_show, args))

  grep_parser = sub.add_parser('grep', help='grep rendered trails as files: <id>:<line>:<text>')
  grep_parser.add_argument('pattern', help='Python regular expression to search for')
  grep_parser.add_argument(
    'trails',
    nargs='*',
    help='trail ids to search (default: every trail, newest first)',
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

  tree_parser = sub.add_parser(
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
