#!/usr/bin/env python
"""human-readable `trails` CLI — counterpart to `sessions` / `rewind` for the
bros recording pipeline.

four subcommands:
- `trails list` — filtered listing of trail headers, paged through `$PAGER`.
- `trails show <id>` — header + step listing for one trail; `-f` keeps polling
  and renders new steps as they land, like `tail -f`.
- `trails tree <id>` — render the parent/fork hierarchy reachable from a trail.
- `trails fork <id> <step_id>` — call `bro.fork.fork()` and drop the user into
  an interactive `.send()` loop, similar in spirit to `bro.launch.call.call_text`.

All commands talk to the deployed `trails-server` via `TrailsClient`; config
comes from the `trails` secret (the same one `HTTPTracker` reads).
"""

import asyncio
import http.client
import json
import os
import sys
import time
from collections.abc import Callable, Iterator
from typing import Any, Optional

import base.args
from base import log, pager
from bro.fork import fork
from llm.tracker import HTTPStatusError, RecordedTrail, Step, is_retryable_status
from trails.client import (
  TrailsClient,
  default_client,
  fetch_recorded_trail,
  step_from_row,
  trail_from_header,
)

__cli_name__ = 'trails'

_BODY_TRUNCATE_CHARS = 240
_SHORT_ID_CHARS = 10


class _Colors:
  def __init__(self, enabled: bool) -> None:
    self.enabled = enabled

  def _c(self, code: str) -> str:
    return f'\033[{code}m' if self.enabled else ''

  @property
  def reset(self) -> str:
    return self._c('0')

  @property
  def dim(self) -> str:
    return self._c('2')

  @property
  def bold(self) -> str:
    return self._c('1')

  @property
  def red(self) -> str:
    return self._c('31')

  @property
  def green(self) -> str:
    return self._c('32')

  @property
  def yellow(self) -> str:
    return self._c('33')

  @property
  def blue(self) -> str:
    return self._c('34')

  @property
  def cyan(self) -> str:
    return self._c('36')


def _should_color(mode: str, stream=sys.stdout) -> bool:
  if mode == 'always':
    return True
  if mode == 'never':
    return False
  if os.environ.get('NO_COLOR') is not None:
    return False
  return stream.isatty()


def _short(trail_id: str) -> str:
  return trail_id[:_SHORT_ID_CHARS]


def _format_timestamp(iso: Optional[str]) -> str:
  if iso is None:
    return '-'
  # 2026-06-07T22:14:03.123456Z -> 2026-06-07 22:14:03
  return iso.replace('T', ' ')[:19]


def _truncate_oneline(body: Any, limit: int = _BODY_TRUNCATE_CHARS) -> str:
  """render a step body to a single line, capped at `limit` chars with a
  `... <N more chars>` marker pointing at the part that got dropped.
  """
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


def _spilled_body(body: Any) -> Optional[dict]:
  """return the spillover descriptor `{s3, url, size}` if `body` is one; else
  None. the server inlines bodies <1MB and returns this shape above that — we
  surface it as a one-liner rather than fetching multi-MB blobs eagerly.
  """
  if not isinstance(body, dict):
    return None
  if 's3' not in body:
    return None
  return body


def _format_step_summary(step: dict, colors: _Colors) -> str:
  kind = step.get('kind', '?')
  # the full step id leads the line so the user can copy it straight into
  # `trails fork <trail_id> <step_id>` — the server doesn't accept prefixes.
  step_id = step.get('step_id', '?')
  timestamp = _format_timestamp(step.get('ts'))
  turn = step.get('turn_index')
  turn_str = f't{turn} ' if turn is not None else ''
  prefix = (
    f'{colors.yellow}{step_id}{colors.reset}  '
    f'{colors.dim}{timestamp}{colors.reset}  {colors.yellow}{turn_str}{kind:<14}{colors.reset}'
  )

  body = step.get('body')
  spilled = _spilled_body(body)
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
    'where',
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


def _format_trail_row(trail: dict, colors: _Colors) -> str:
  trail_id = trail.get('trail_id', '?')
  bro = trail.get('bro', '?')
  spec = trail.get('llm_spec', {})
  model = spec.get('model', '?')
  started = _format_timestamp(trail.get('started_at'))
  ended_raw = trail.get('ended_at')
  end_reason = trail.get('end_reason')
  if ended_raw is None:
    status = f'{colors.yellow}live{colors.reset}'
  elif end_reason == 'lost':
    # server-stamped by the sweep: recording went silent without a real end.
    status = f'{colors.red}lost{colors.reset}'
  else:
    status = f'{colors.green}done:{end_reason}{colors.reset}'
  parent = trail.get('parent')
  parent_tag = ''
  if parent is not None:
    parent_tag = f'  {colors.dim}fork-of {parent["trail_id"]}{colors.reset}'
  # the full trail id is on the line so the user can copy it straight into
  # `trails show / tree / fork` — the server doesn't accept prefixes.
  return (
    f'{colors.yellow}{trail_id}{colors.reset}  '
    f'{colors.dim}{started}{colors.reset}  '
    f'{colors.cyan}{bro:<10}{colors.reset}  '
    f'{colors.dim}{model:<10}{colors.reset}  '
    f'{status}{parent_tag}'
  )


def _format_trail_header(trail: dict, colors: _Colors) -> str:
  spec = trail.get('llm_spec', {})
  aggregates = trail.get('aggregates', {})
  parent = trail.get('parent')
  lines = [
    f'{colors.bold}trail     {colors.reset} {trail.get("trail_id")}',
    f'{colors.dim}bro       {colors.reset} {trail.get("bro")} (version {trail.get("bro_version")})',
    f'{colors.dim}llm_spec  {colors.reset} {json.dumps(spec, ensure_ascii=False)}',
    f'{colors.dim}started   {colors.reset} {_format_timestamp(trail.get("started_at"))}',
    f'{colors.dim}ended     {colors.reset} {_format_timestamp(trail.get("ended_at"))}  '
    f'({trail.get("end_reason")})',
    f'{colors.dim}interactive{colors.reset} {trail.get("interactive")}  '
    f'{colors.dim}entry_point{colors.reset} {trail.get("entry_point")}',
  ]
  if parent is not None:
    lines.append(
      f'{colors.dim}parent    {colors.reset} {parent.get("relationship")} '
      f'{parent.get("trail_id")} @ step {parent.get("step_id")}'
    )
  summoner = trail.get('summoner')
  if summoner is not None:
    lines.append(f'{colors.dim}summoner  {colors.reset} {json.dumps(summoner, ensure_ascii=False)}')
  continuation = trail.get('continuation')
  if continuation is not None:
    lines.append(f'{colors.dim}continuation{colors.reset} {json.dumps(continuation)}')
  lines.append(
    f'{colors.dim}aggregates{colors.reset} '
    f'turns={aggregates.get("turn_count")} '
    f'tools={aggregates.get("tool_call_count")} '
    f'tokens_in={aggregates.get("tokens_in")} '
    f'tokens_out={aggregates.get("tokens_out")} '
    f'tokens_reasoning={aggregates.get("tokens_reasoning")}'
  )
  counts = aggregates.get('step_counts_by_kind')
  if counts is not None:
    parts = ', '.join(f'{k}={v}' for k, v in counts.items() if v > 0)
    lines.append(f'{colors.dim}step kinds{colors.reset} {parts}')
  lines.append(colors.dim + ('─' * 78) + colors.reset)
  return '\n'.join(lines)


def _command_list(client: TrailsClient, args: dict, colors: _Colors) -> int:
  trails_iter = client.iter_trails(
    bro=args.get('bro'),
    parent=args.get('parent'),
    since=args.get('since'),
    until=args.get('until'),
    max_items=args.get('limit'),
  )
  trails_list = list(trails_iter)
  if len(trails_list) == 0:
    print('(no trails)', file=sys.stderr)
    return 0
  text = '\n'.join(_format_trail_row(t, colors) for t in trails_list) + '\n'
  will_page = sys.stdout.isatty() and not args.get('no_pager', False)
  if will_page:
    pager.page(text)
  else:
    sys.stdout.write(text)
  return 0


def _follow_steps(
  client: TrailsClient,
  trail_id: str,
  *,
  interval: float,
  sleep: Callable[[float], None] = time.sleep,
) -> Iterator[dict]:
  """yield the trail's existing steps, then keep polling for new ones every
  `interval` seconds — `tail -f` over a trail's step stream.

  terminates once the `end` step arrives or — for a trail that never got one
  (`end_trail` is best-effort on the write side) — once an idle poll finds
  `ended_at` set on the header; a still-live trail is followed until
  interrupted. transient failures (network blips, 5xx / 429) are logged and
  retried on the next tick; a deterministic 4xx propagates.
  """
  after: Optional[str] = None
  while True:
    try:
      for row in client.iter_steps(trail_id, after=after):
        after = row['step_id']
        yield row
        if row.get('kind') == 'end':
          return
      if client.get_trail(trail_id).get('ended_at') is not None:
        # the end transaction may have committed between the steps poll and
        # this header read — drain once more so its steps are not dropped.
        yield from client.iter_steps(trail_id, after=after)
        return
    except HTTPStatusError as exception:
      if not is_retryable_status(exception.status):
        raise
      log.warning('transient trails-server error, retrying: %s', exception)
    except (OSError, http.client.HTTPException) as exception:
      log.warning('transient trails-server error, retrying: %s', exception)
    sleep(interval)


def _command_show(client: TrailsClient, args: dict, colors: _Colors) -> int:
  trail_id = args['trail_id']
  header = client.get_trail(trail_id)
  if bool(args.get('follow', False)):
    print(_format_trail_header(header, colors), flush=True)
    try:
      for row in _follow_steps(client, trail_id, interval=args['interval']):
        print(_format_step_summary(row, colors), flush=True)
    except KeyboardInterrupt:
      return 130
    return 0
  out: list[str] = [_format_trail_header(header, colors)]
  for row in client.iter_steps(trail_id):
    out.append(_format_step_summary(row, colors))
  text = '\n'.join(out) + '\n'
  will_page = sys.stdout.isatty() and not args.get('no_pager', False)
  if will_page:
    pager.page(text)
  else:
    sys.stdout.write(text)
  return 0


def _command_tree(client: TrailsClient, args: dict, colors: _Colors) -> int:
  trail_id = args['trail_id']
  start = client.get_trail(trail_id)

  # walk upward to the root so the tree displays the full ancestry rather than
  # just the children of the named trail. cycles are not possible (parent
  # pointers are append-only at trail-creation time), so plain ascent is safe.
  root = start
  while True:
    parent = root.get('parent')
    if parent is None:
      break
    root = client.get_trail(parent['trail_id'])

  highlight = trail_id
  lines: list[str] = []
  _render_tree(client, root, '', is_last=True, lines=lines, colors=colors, highlight=highlight)
  sys.stdout.write('\n'.join(lines) + '\n')
  return 0


def _render_tree(
  client: TrailsClient,
  trail: dict,
  prefix: str,
  *,
  is_last: bool,
  lines: list[str],
  colors: _Colors,
  highlight: str,
) -> None:
  trail_id = trail['trail_id']
  connector = '└── ' if is_last else '├── '
  marker = f' {colors.bold}<-- here{colors.reset}' if trail_id == highlight else ''
  spec = trail.get('llm_spec', {})
  model = spec.get('model', '?')
  bro = trail.get('bro', '?')
  parent_step = ''
  parent = trail.get('parent')
  if parent is not None:
    parent_step = f' {colors.dim}@step {_short(parent["step_id"])}{colors.reset}'
  lines.append(
    f'{prefix}{connector}{colors.yellow}{_short(trail_id)}{colors.reset}  '
    f'{colors.cyan}{bro}{colors.reset}/{colors.dim}{model}{colors.reset}'
    f'{parent_step}{marker}'
  )
  children = list(client.iter_trails(parent=trail_id))
  # iter_trails returns newest-first; reverse so the tree reads oldest-first
  # under each node, matching how a reader would build it up mentally.
  children.reverse()
  child_prefix = prefix + ('    ' if is_last else '│   ')
  for i, child in enumerate(children):
    last = i == len(children) - 1
    _render_tree(
      client, child, child_prefix, is_last=last, lines=lines, colors=colors, highlight=highlight
    )


def _command_fork(client: TrailsClient, args: dict, colors: _Colors) -> int:
  trail_id = args['trail_id']
  step_id = args['step_id']
  initial = args.get('initial')
  no_record = bool(args.get('no_record', False))

  log.info('fetching parent trail %s', trail_id)
  parent_trail = fetch_recorded_trail(client, trail_id)
  log.info('forking at step %s (%d steps in prefix)', step_id, len(parent_trail.steps))

  bro = fork(
    parent_trail,
    step_id,
    record=not no_record,
    # a fork of a fork replays its ancestor prefix through the same reader
    fetch_parent=lambda parent_id: fetch_recorded_trail(client, parent_id),
  )
  new_trail_id = bro.trail_id
  fork_banner = (
    f'forked {_short(trail_id)}@{_short(step_id)} as {colors.yellow}{bro.name}{colors.reset}'
  )
  if new_trail_id is not None:
    fork_banner += f'  new trail: {colors.yellow}{new_trail_id}{colors.reset}'
  print(fork_banner, file=sys.stderr)
  if no_record:
    print(f'{colors.dim}(recording disabled){colors.reset}', file=sys.stderr)

  try:
    asyncio.run(_fork_repl(bro, initial))
  except KeyboardInterrupt:
    return 130
  return 0


async def _fork_repl(
  bro,
  initial: Optional[str],
  *,
  read_line: Optional[Callable[[], str]] = None,
  emit: Optional[Callable[[str], None]] = None,
) -> None:
  read = read_line if read_line is not None else (lambda: input('> '))
  emit_reply = emit if emit is not None else (lambda r: print(f'{bro.name}: {r}'))

  if initial is not None and len(initial) > 0:
    reply = await bro.send(initial)
    emit_reply(reply)
  while True:
    try:
      message = read()
    except EOFError:
      return
    if len(message) == 0:
      continue
    reply = await bro.send(message)
    emit_reply(reply)


def main(argv: list[str]) -> Optional[int]:
  parser = base.args.Parser(description='inspect and fork recorded bro trails')
  parser.add_argument(
    '--color',
    default='auto',
    choices=['auto', 'always', 'never'],
    help='color output (default: auto = on if stdout is a TTY and NO_COLOR is unset)',
  )
  sub = parser.add_subparsers(dest='command')

  list_p = sub.add_parser('list', help='list trail headers, newest first')
  list_p.add_argument('--bro', help='filter by bro name')
  list_p.add_argument('--since', help='ISO timestamp lower bound on started_at')
  list_p.add_argument('--until', help='ISO timestamp upper bound on started_at')
  list_p.add_argument('--parent', help='list forks of this trail id')
  list_p.add_argument('--limit', type=int, default=50, help='max trails to list')
  list_p.add_argument('--no-pager', action='store_true', help='do not pipe output through a pager')

  show_p = sub.add_parser('show', help='show header + step listing for one trail')
  show_p.add_argument('trail_id')
  show_p.add_argument('--no-pager', action='store_true', help='do not pipe output through a pager')
  show_p.add_argument(
    '-f',
    '--follow',
    action='store_true',
    help='keep polling for new steps and render them as they arrive, like tail -f; '
    'exits once the trail ends (no pager)',
  )
  show_p.add_argument(
    '--interval', type=float, default=2.0, help='seconds between polls with --follow'
  )

  tree_p = sub.add_parser('tree', help='render the parent/fork hierarchy reachable from a trail')
  tree_p.add_argument('trail_id')

  fork_p = sub.add_parser(
    'fork',
    help='fork a recorded trail at <step_id> and drop into an interactive .send() loop',
  )
  fork_p.add_argument('trail_id')
  fork_p.add_argument('step_id')
  fork_p.add_argument('--initial', help='send this as the first user message before the prompt')
  fork_p.add_argument(
    '--no-record',
    action='store_true',
    help='pin the fork to a NullTracker so the conversation is not saved as a new trail',
  )

  args = parser.parse(argv)
  command = args.get('command')
  if command is None:
    parser.print_help(sys.stderr)
    return 1

  colors = _Colors(_should_color(args['color']))
  with default_client() as client:
    if command == 'list':
      return _command_list(client, args, colors)
    if command == 'show':
      return _command_show(client, args, colors)
    if command == 'tree':
      return _command_tree(client, args, colors)
    if command == 'fork':
      return _command_fork(client, args, colors)
  parser.print_help(sys.stderr)
  return 1


# re-exported so tests / external callers can rehydrate steps without reaching
# into trails.client themselves.
__all__ = ['RecordedTrail', 'Step', 'main', 'step_from_row', 'trail_from_header']
