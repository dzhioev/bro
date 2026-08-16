#!/usr/bin/env python
"""`rewind` — the single reader over recorded runs, every harness."""

import re
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from typing import Any, Optional

import bro.base.args as base_args
from bro.base import log, pager
from bro.base.ansi import Colors, should_color
from bro.base.text_window import DEFAULT_LIMIT, window
from bro.trails.display import (
  ColorMode,
  DisplayConfig,
  DisplayRecord,
  DisplaySession,
  PresetName,
  RecordedAdapter,
  RecordedSource,
  RetainedRenderer,
  StreamRenderer,
  preset,
)
from bro.trails.store import TrailsStore, TransientUnavailable, default_store

__cli_name__ = 'rewind'


def _configuration(args: dict[str, Any], name: PresetName) -> DisplayConfig:
  return preset(name, color=ColorMode(args.get('color', ColorMode.AUTO)))


def _retained_document(records: Iterable[DisplayRecord], configuration: DisplayConfig) -> str:
  renderer = RetainedRenderer(target=sys.stdout)
  with DisplaySession(configuration, renderer) as session:
    session.consume(records)
  return renderer.document()


def _window_output(text: str, args: dict[str, Any]) -> str:
  offset = args.get('output_offset')
  limit = args.get('output_limit')
  if offset is None and limit is None:
    return text
  if offset is not None and offset < 0:
    raise SystemExit('output offset must be non-negative')
  return window(text, offset or 0, DEFAULT_LIMIT if limit is None else limit)


def _emit_document(text: str, args: dict[str, Any], configuration: DisplayConfig) -> None:
  text = _window_output(text, args)
  if (
    configuration.paging
    and sys.stdout.isatty()
    and not bool(args.get('no_pager', False))
    and not bool(args.get('follow', False))
  ):
    pager.page(text)
  else:
    sys.stdout.write(text)


def _command_list(client: TrailsStore, args: dict[str, Any]) -> int:
  adapter = RecordedAdapter(client)
  records = [
    adapter.trail_list_row(header)
    for header in client.iter_trails(
      harness=args.get('harness'),
      bro=args.get('bro'),
      forked_from=args.get('forked_from'),
      since=args.get('since'),
      until=args.get('until'),
      max_items=args.get('limit'),
    )
  ]
  if len(records) == 0:
    print('(no trails)', file=sys.stderr)
    return 0
  configuration = _configuration(args, PresetName.REWIND_LIST)
  _emit_document(_retained_document(records, configuration), args, configuration)
  return 0


def _follow_batches(
  client: TrailsStore,
  trail_id: str,
  *,
  iterator: Callable[[str, Optional[int]], Iterator[dict]],
  cursor: Callable[[dict], int],
  terminal: Callable[[dict], bool],
  interval: float,
  after: Optional[int] = None,
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
    except TransientUnavailable as exception:
      log.warning('trails store temporarily unavailable, retrying: %s', exception)
    sleep(interval)


def _stream_follow(
  client: TrailsStore,
  args: dict[str, Any],
  configuration: DisplayConfig,
  initial_records: Iterable[DisplayRecord],
  after: int | None,
  *,
  iterator: Callable[[str, Optional[int]], Iterator[dict]],
  cursor: Callable[[dict], int],
  terminal: Callable[[dict], bool],
  adapt_batch: Callable[[list[dict]], Iterable[DisplayRecord]],
) -> int:
  renderer = StreamRenderer(sys.stdout)
  try:
    with DisplaySession(configuration, renderer) as session:
      session.consume(initial_records)
      if renderer.consumer_closed:
        return 0
      for rows in _follow_batches(
        client,
        args['trail_id'],
        iterator=iterator,
        cursor=cursor,
        terminal=terminal,
        interval=args['interval'],
        after=after,
      ):
        session.consume(adapt_batch(rows))
        if renderer.consumer_closed:
          return 0
  except KeyboardInterrupt:
    return 130
  return 0


def _last_target_step_id(records: Iterable[DisplayRecord], trail_id: str) -> int | None:
  return max(
    (
      record.source.step_id
      for record in records
      if isinstance(record.source, RecordedSource) and record.source.trail_id == trail_id
    ),
    default=None,
  )


def _command_show(client: TrailsStore, args: dict[str, Any]) -> int:
  adapter = RecordedAdapter(client)
  trail_id = args['trail_id']
  header = client.get_trail(trail_id)
  records = adapter.conversation_records(header)
  after = _last_target_step_id(records, trail_id)
  configuration = _configuration(args, PresetName.REWIND_SHOW)
  if bool(args.get('follow', False)):
    return _stream_follow(
      client,
      args,
      configuration,
      records,
      after,
      iterator=lambda selected_id, cursor: client.iter_messages(selected_id, after=cursor),
      cursor=lambda message: message['source']['step_id'],
      terminal=lambda message: False,
      adapt_batch=lambda messages: adapter.message_records(trail_id, messages),
    )
  _emit_document(_retained_document(records, configuration), args, configuration)
  return 0


def _command_steps(client: TrailsStore, args: dict[str, Any]) -> int:
  adapter = RecordedAdapter(client)
  trail_id = args['trail_id']
  header = client.get_trail(trail_id)
  steps = list(client.iter_steps(trail_id))
  records: list[DisplayRecord] = [
    adapter.trail_metadata(header),
    *adapter.native_step_records(trail_id, steps),
  ]
  after = steps[-1]['step_id'] if len(steps) > 0 else None
  configuration = _configuration(args, PresetName.REWIND_STEPS)
  if bool(args.get('follow', False)):
    return _stream_follow(
      client,
      args,
      configuration,
      records,
      after,
      iterator=lambda selected_id, cursor: client.iter_steps(selected_id, after=cursor),
      cursor=lambda step: step['step_id'],
      terminal=lambda step: step.get('kind') == 'end',
      adapt_batch=lambda batch: adapter.native_step_records(trail_id, batch),
    )
  _emit_document(_retained_document(records, configuration), args, configuration)
  return 0


def _grep_lines(
  name: str,
  text: str,
  regex: re.Pattern[str],
  colors: Colors,
  before: int = 0,
  after: int = 0,
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


def _command_grep(client: TrailsStore, args: dict[str, Any]) -> int:
  """Exit 0 when at least one line matched, 1 otherwise — like grep."""
  try:
    regex = re.compile(args['pattern'], re.IGNORECASE if args.get('ignore_case', False) else 0)
  except re.error as exception:
    raise SystemExit(f'invalid pattern {args["pattern"]!r}: {exception}') from exception

  context_value: Optional[int] = args.get('context')
  default_context = context_value if context_value is not None else 0
  after_value: Optional[int] = args.get('after_context')
  after = after_value if after_value is not None else default_context
  before_value: Optional[int] = args.get('before_context')
  before = before_value if before_value is not None else default_context
  if before < 0 or after < 0:
    raise SystemExit('context lengths must be non-negative')
  has_context = before > 0 or after > 0

  trail_ids: list[str] = args.get('trails', [])
  if len(trail_ids) > 0:
    headers = [client.get_trail(trail_id) for trail_id in trail_ids]
  else:
    headers = list(client.iter_trails(harness=args.get('harness'), max_items=args.get('limit')))

  log.info('searching %d trails', len(headers))
  colors = Colors(should_color(args['color']))
  configuration = _configuration(args, PresetName.REWIND_GREP)
  groups: list[str] = []
  for header in headers:
    adapter = RecordedAdapter(client)
    records = adapter.conversation_records(header)
    rendered = _retained_document(records, configuration)
    matches = _grep_lines(header['id'], rendered, regex, colors, before=before, after=after)
    if len(matches) > 0:
      groups.append('\n'.join(matches))
  if len(groups) == 0:
    return 1
  separator = f'\n{colors.cyan}--{colors.reset}\n' if has_context else '\n'
  sys.stdout.write(_window_output(separator.join(groups) + '\n', args))
  sys.stdout.flush()
  return 0


def _command_tree(client: TrailsStore, args: dict[str, Any]) -> int:
  adapter = RecordedAdapter(client)
  records = adapter.lineage_records(args['trail_id'])
  configuration = _configuration(args, PresetName.REWIND_TREE)
  _emit_document(_retained_document(records, configuration), args, configuration)
  return 0


_COMMANDS = ('list', 'show', 'steps', 'grep', 'tree')


def _with_default_command(argv: list[str]) -> list[str]:
  """Default the subcommand to `show`, so `rewind <trail-id>` keeps working."""
  remaining = argv[1:]
  if len(remaining) == 0 or remaining[0] in _COMMANDS or remaining[0] in ('-h', '--help'):
    return argv
  return [argv[0], 'show', *remaining]


def _add_color_argument(parser: base_args.Parser) -> None:
  parser.add_argument(
    '--color',
    default='auto',
    choices=['auto', 'always', 'never'],
    help='color output (default: auto = on if stdout is a TTY and NO_COLOR is unset)',
  )


def _add_output_window_arguments(parser: base_args.Parser) -> None:
  parser.add_argument(
    '--output-offset',
    type=int,
    help='skip this many rendered output lines before printing',
  )
  parser.add_argument(
    '--output-limit',
    type=int,
    help=f'max rendered output lines (default with an offset: {DEFAULT_LIMIT})',
  )


def _add_view_arguments(parser: base_args.Parser) -> None:
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
  _add_output_window_arguments(parser)
  _add_color_argument(parser)


def main(argv: list[str]) -> Optional[int]:
  parser = base_args.Parser(
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
  _add_output_window_arguments(grep_parser)
  _add_color_argument(grep_parser)
  grep_parser.set_handler(lambda **args: _dispatch(_command_grep, args))

  tree_parser = subparsers.add_parser(
    'tree', help='render the forked_from/fork hierarchy reachable from a trail'
  )
  tree_parser.add_argument('trail_id')
  _add_output_window_arguments(tree_parser)
  _add_color_argument(tree_parser)
  tree_parser.set_handler(lambda **args: _dispatch(_command_tree, args))

  return parser.dispatch(_with_default_command(argv))


def _dispatch(command: Callable[[TrailsStore, dict[str, Any]], int], args: dict[str, Any]) -> int:
  with default_store() as client:
    return command(client, args)
