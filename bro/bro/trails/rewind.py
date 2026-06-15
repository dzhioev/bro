#!/usr/bin/env python
"""human-readable `trails` CLI — counterpart to `sessions` / `rewind` for the
bros recording pipeline.

four subcommands:
- `trails list` — filtered listing of trail headers, paged through `$PAGER`.
- `trails show <id>` — header + step listing for one trail.
- `trails tree <id>` — render the parent/fork hierarchy reachable from a trail.
- `trails fork <id> <step_id>` — call `bro.fork.fork()` and drop the user into
  an interactive `.send()` loop, similar in spirit to `do.call.call_text`.

All commands talk to the deployed `trails-server` via `TrailsClient`; config
comes from the `trails` secret (the same one `HTTPTracker` reads).
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from typing import Any, Callable

import base.args
from base import log
from bro.fork import fork
from llm.tracker import RecordedTrail, Step
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


def _page(text: str) -> None:
  pager_cmd = os.environ.get('PAGER')
  if pager_cmd is None or len(pager_cmd.strip()) == 0:
    pager_cmd = 'less -FRX' if shutil.which('less') is not None else None
  if pager_cmd is None:
    sys.stdout.write(text)
    return
  try:
    p = subprocess.Popen(pager_cmd, shell=True, stdin=subprocess.PIPE)
  except FileNotFoundError:
    sys.stdout.write(text)
    return
  assert p.stdin is not None
  try:
    p.stdin.write(text.encode('utf-8'))
  except BrokenPipeError:
    pass
  finally:
    try:
      p.stdin.close()
    except BrokenPipeError:
      pass
  p.wait()


def _short(trail_id: str) -> str:
  return trail_id[:_SHORT_ID_CHARS]


def _format_ts(iso: str | None) -> str:
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


def _spilled_body(body: Any) -> dict | None:
  """return the spillover descriptor `{s3, url, size}` if `body` is one; else
  None. the server inlines bodies <1MB and returns this shape above that — we
  surface it as a one-liner rather than fetching multi-MB blobs eagerly.
  """
  if not isinstance(body, dict):
    return None
  if 's3' not in body:
    return None
  return body


def _format_step_summary(step: dict, col: _Colors) -> str:
  kind = step.get('kind', '?')
  # the full step ULID leads the line so the user can copy it straight into
  # `trails fork <trail_id> <step_id>` — the server doesn't accept prefixes.
  step_id = step.get('step_id', '?')
  ts = _format_ts(step.get('ts'))
  turn = step.get('turn_index')
  turn_str = f't{turn} ' if turn is not None else ''
  prefix = (
    f'{col.yellow}{step_id}{col.reset}  '
    f'{col.dim}{ts}{col.reset}  {col.yellow}{turn_str}{kind:<14}{col.reset}'
  )

  body = step.get('body')
  spilled = _spilled_body(body)
  if spilled is not None:
    size = spilled.get('size', '?')
    url = spilled.get('url', '-')
    summary = f'{col.dim}<{size} bytes spilled>{col.reset} {url}'
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
    'where',
  ):
    if key in step:
      extras_parts.append(f'{col.cyan}{key}{col.reset}={step[key]}')
  arguments = step.get('arguments')
  if arguments is not None:
    extras_parts.append(f'{col.cyan}args{col.reset}={_truncate_oneline(arguments, 80)}')

  parts = [prefix]
  if len(summary) > 0:
    parts.append(summary)
  if len(extras_parts) > 0:
    parts.append(f'[{" ".join(extras_parts)}]')
  return '  '.join(parts)


def _format_trail_row(trail: dict, col: _Colors) -> str:
  tid = trail.get('trail_id', '?')
  bro = trail.get('bro', '?')
  spec = trail.get('llm_spec') or {}
  model = spec.get('model', '?')
  started = _format_ts(trail.get('started_at'))
  ended_raw = trail.get('ended_at')
  end_reason = trail.get('end_reason')
  status = (
    f'{col.green}done:{end_reason}{col.reset}'
    if ended_raw is not None
    else f'{col.yellow}live{col.reset}'
  )
  parent = trail.get('parent')
  parent_tag = ''
  if parent is not None:
    parent_tag = f'  {col.dim}fork-of {parent["trail_id"]}{col.reset}'
  # the full ULID is on the line so the user can copy it straight into
  # `trails show / tree / fork` — the server doesn't accept prefixes.
  return (
    f'{col.yellow}{tid}{col.reset}  '
    f'{col.dim}{started}{col.reset}  '
    f'{col.cyan}{bro:<10}{col.reset}  '
    f'{col.dim}{model:<10}{col.reset}  '
    f'{status}{parent_tag}'
  )


def _format_trail_header(trail: dict, col: _Colors) -> str:
  spec = trail.get('llm_spec') or {}
  aggregates = trail.get('aggregates') or {}
  parent = trail.get('parent')
  lines = [
    f'{col.bold}trail     {col.reset} {trail.get("trail_id")}',
    f'{col.dim}bro       {col.reset} {trail.get("bro")} (version {trail.get("bro_version")})',
    f'{col.dim}llm_spec  {col.reset} {json.dumps(spec, ensure_ascii=False)}',
    f'{col.dim}started   {col.reset} {_format_ts(trail.get("started_at"))}',
    f'{col.dim}ended     {col.reset} {_format_ts(trail.get("ended_at"))}  '
    f'({trail.get("end_reason")})',
    f'{col.dim}interactive{col.reset} {trail.get("interactive")}  '
    f'{col.dim}entry_point{col.reset} {trail.get("entry_point")}',
  ]
  if parent is not None:
    lines.append(
      f'{col.dim}parent    {col.reset} {parent.get("relationship")} '
      f'{parent.get("trail_id")} @ step {parent.get("step_id")}'
    )
  continuation = trail.get('continuation')
  if continuation is not None:
    lines.append(f'{col.dim}continuation{col.reset} {json.dumps(continuation)}')
  lines.append(
    f'{col.dim}aggregates{col.reset} '
    f'turns={aggregates.get("turn_count")} '
    f'tools={aggregates.get("tool_call_count")} '
    f'tokens_in={aggregates.get("tokens_in")} '
    f'tokens_out={aggregates.get("tokens_out")} '
    f'tokens_reasoning={aggregates.get("tokens_reasoning")}'
  )
  counts = aggregates.get('step_counts_by_kind')
  if counts is not None:
    parts = ', '.join(f'{k}={v}' for k, v in counts.items() if v > 0)
    lines.append(f'{col.dim}step kinds{col.reset} {parts}')
  lines.append(col.dim + ('─' * 78) + col.reset)
  return '\n'.join(lines)


def _cmd_list(client: TrailsClient, args: dict, col: _Colors) -> int:
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
  text = '\n'.join(_format_trail_row(t, col) for t in trails_list) + '\n'
  will_page = sys.stdout.isatty() and not args.get('no_pager', False)
  if will_page:
    _page(text)
  else:
    sys.stdout.write(text)
  return 0


def _cmd_show(client: TrailsClient, args: dict, col: _Colors) -> int:
  trail_id = args['trail_id']
  header = client.get_trail(trail_id)
  out: list[str] = [_format_trail_header(header, col)]
  for row in client.iter_steps(trail_id):
    out.append(_format_step_summary(row, col))
  text = '\n'.join(out) + '\n'
  will_page = sys.stdout.isatty() and not args.get('no_pager', False)
  if will_page:
    _page(text)
  else:
    sys.stdout.write(text)
  return 0


def _cmd_tree(client: TrailsClient, args: dict, col: _Colors) -> int:
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
  _render_tree(client, root, '', is_last=True, lines=lines, col=col, highlight=highlight)
  sys.stdout.write('\n'.join(lines) + '\n')
  return 0


def _render_tree(
  client: TrailsClient,
  trail: dict,
  prefix: str,
  *,
  is_last: bool,
  lines: list[str],
  col: _Colors,
  highlight: str,
) -> None:
  tid = trail['trail_id']
  connector = '└── ' if is_last else '├── '
  marker = f' {col.bold}<-- here{col.reset}' if tid == highlight else ''
  spec = trail.get('llm_spec') or {}
  model = spec.get('model', '?')
  bro = trail.get('bro', '?')
  parent_step = ''
  parent = trail.get('parent')
  if parent is not None:
    parent_step = f' {col.dim}@step {_short(parent["step_id"])}{col.reset}'
  lines.append(
    f'{prefix}{connector}{col.yellow}{_short(tid)}{col.reset}  '
    f'{col.cyan}{bro}{col.reset}/{col.dim}{model}{col.reset}'
    f'{parent_step}{marker}'
  )
  children = list(client.iter_trails(parent=tid))
  # iter_trails returns newest-first; reverse so the tree reads oldest-first
  # under each node, matching how a reader would build it up mentally.
  children.reverse()
  child_prefix = prefix + ('    ' if is_last else '│   ')
  for i, child in enumerate(children):
    last = i == len(children) - 1
    _render_tree(
      client, child, child_prefix, is_last=last, lines=lines, col=col, highlight=highlight
    )


def _cmd_fork(client: TrailsClient, args: dict, col: _Colors) -> int:
  trail_id = args['trail_id']
  step_id = args['step_id']
  initial = args.get('initial')
  no_record = bool(args.get('no_record', False))

  log.info('fetching parent trail %s', trail_id)
  parent_trail = fetch_recorded_trail(client, trail_id)
  log.info('forking at step %s (%d steps in prefix)', step_id, len(parent_trail.steps))

  bro = fork(parent_trail, step_id, record=not no_record)
  new_trail_id = getattr(bro._tracker, '_trail_id', None)
  fork_banner = f'forked {_short(trail_id)}@{_short(step_id)} as {col.yellow}{bro.name}{col.reset}'
  if new_trail_id is not None and len(new_trail_id) > 0:
    fork_banner += f'  new trail: {col.yellow}{new_trail_id}{col.reset}'
  print(fork_banner, file=sys.stderr)
  if no_record:
    print(f'{col.dim}(recording disabled){col.reset}', file=sys.stderr)

  try:
    asyncio.run(_fork_repl(bro, initial))
  except KeyboardInterrupt:
    return 130
  return 0


async def _fork_repl(
  bro,
  initial: str | None,
  *,
  read_line: Callable[[], str] | None = None,
  emit: Callable[[str], None] | None = None,
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


def main(argv=None) -> int | None:
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

  col = _Colors(_should_color(args['color']))
  client = default_client()
  try:
    if command == 'list':
      return _cmd_list(client, args, col)
    if command == 'show':
      return _cmd_show(client, args, col)
    if command == 'tree':
      return _cmd_tree(client, args, col)
    if command == 'fork':
      return _cmd_fork(client, args, col)
  finally:
    client.close()
  parser.print_help(sys.stderr)
  return 1


# re-exported so tests / external callers can rehydrate steps without reaching
# into trails.client themselves.
__all__ = ['RecordedTrail', 'Step', 'main', 'step_from_row', 'trail_from_header']


if __name__ == '__main__':
  sys.exit(main(sys.argv))
