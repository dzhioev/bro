#!/usr/bin/env python
"""argument parsing built on argparse.

bro.base.args must stay importable in a stdlib-only environment (no venv): some
consumers run outside it — e.g. bro/workflow/commit_footer.py via the commit
git hooks. So it imports no third-party package at module load. icecream is the one
exception, treated as optional — the --ic debug flag is registered only when it is
installed.
"""

import argparse
import contextlib
import os
import sys
from collections.abc import Callable, Generator, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Optional, TypeVar, overload

from bro.base import log

try:
  from icecream import ic
except ImportError:  # icecream is a venv dep; bro.base.args must import without it
  ic = None

# re-exported so bro.base.args is the only module in the repo that imports argparse
REMAINDER = argparse.REMAINDER
SUPPRESS = argparse.SUPPRESS
ArgumentTypeError = argparse.ArgumentTypeError

if TYPE_CHECKING:
  from bro.base.time_util import Moment


def moment_parser(arg: str) -> 'Moment':
  # lazy: bro.base.time_util imports bro.base.args for its own CLI, so a top-level import
  # here would be a cycle.
  from bro.base.time_util import parse_moment

  try:
    return parse_moment(arg)
  except ValueError as e:
    raise argparse.ArgumentTypeError(f'can\'t parse: "{arg}" exception: "{str(e)}"')


def list_parser(arg: str) -> list[str]:
  return [item.strip() for item in arg.split(',')]


def trigger(function: Callable) -> type[argparse.Action]:
  class TriggerAction(argparse.Action):
    def __init__(self, option_strings, dest, **kwargs):
      kwargs.setdefault('nargs', 0)
      super().__init__(option_strings, dest, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
      del parser, namespace, values, option_string
      function()

  return TriggerAction


def enable_ic() -> None:
  if ic is not None:
    ic.enable()


_N = TypeVar('_N')

# subparser handler stashed via set_handler, popped by dispatch
_HANDLER_DEST = '_handler'


def _default_env_name(option_strings: Sequence[str]) -> Optional[str]:
  long_opts = [o for o in option_strings if o.startswith('--')]
  if len(long_opts) == 0:
    return None
  name = max(long_opts, key=len).lstrip('-')
  return name.replace('-', '_').upper()


def _is_unsupported_env_action(action: argparse.Action) -> bool:
  if isinstance(action, (argparse._AppendAction, argparse._CountAction)):
    return True
  if action.nargs in ('+', '*'):
    return True
  return False


def _is_nargs_zero(action: argparse.Action) -> bool:
  return action.nargs == 0


class _Formatter(argparse.HelpFormatter):
  _global_ids: frozenset[int] = frozenset()

  def _format_usage(self, usage, actions, groups, prefix):  # type: ignore[override]
    global_ids = self._global_ids
    script = [a for a in actions if id(a) not in global_ids]
    global_actions = [a for a in actions if id(a) in global_ids]
    result = super()._format_usage(usage, script, groups, prefix)
    if len(global_actions) == 0:
      return result
    global_usage = self._format_actions_usage(global_actions, []).strip()  # type: ignore[attr-defined]
    if global_usage == '':
      return result
    body = result.rstrip('\n')
    lines = body.split('\n')
    if len(lines) > 1:
      indent_len = len(lines[1]) - len(lines[1].lstrip())
    else:
      actual_prefix = prefix if prefix is not None else 'usage: '
      indent_len = len(actual_prefix) + len(self._prog) + 1
    indent = ' ' * indent_len
    return f'{body}\n{indent}({global_usage})\n'


class Parser(argparse.ArgumentParser):
  def __init__(self, *args, **kwargs):
    self._exclusive_groups: list[list[list[str]]] = []
    self._env_info: dict[str, dict] = {}
    self._last_argv: Optional[list[str]] = None
    kwargs.setdefault('formatter_class', _Formatter)
    super().__init__(*args, **kwargs)
    self._global_group = self.add_argument_group('global options')
    for action in self._actions:
      if isinstance(action, argparse._HelpAction):
        self._move_to_global(action)
        break
    self._global_group.add_argument(
      '--allow-env', action='store_true', help='honor env-var overrides for flags'
    )
    self._global_group.add_argument(
      '--print-env', action='store_true', help='print env-var summary and exit'
    )
    self._log_action = self._add_global_argument(
      '--log',
      env=False,
      choices=log.LEVEL_NAMES,
      help='log level threshold (default: info, or the inherited BRO_LOG_LEVEL)',
    )
    self._verbose_action = self._add_global_argument(
      '--verbose',
      action='store_const',
      const='verbose',
      dest='log',
      help='shorthand for --log verbose',
    )
    if ic is not None:
      self._add_global_argument('--ic', action=trigger(enable_ic), help='enable ic output')

  def _move_to_global(self, action: argparse.Action) -> None:
    for group in self._action_groups:
      if group is not self._global_group and action in group._group_actions:
        group._group_actions.remove(action)
    if action not in self._global_group._group_actions:
      self._global_group._group_actions.append(action)

  def _add_global_argument(self, *args, **kwargs):
    action = self.add_argument(*args, **kwargs)
    self._move_to_global(action)
    return action

  def add_argument(self, *args, env: bool = True, secret: bool = False, **kwargs):  # type: ignore[override]
    is_flag = any(isinstance(a, str) and a.startswith('-') for a in args)
    if not is_flag:
      return super().add_argument(*args, **kwargs)

    action = super().add_argument(*args, **kwargs)
    if not env or isinstance(action, argparse._HelpAction):
      return action

    env_name = _default_env_name(action.option_strings)
    if env_name is None:
      return action

    unsupported = _is_unsupported_env_action(action)
    self._env_info[action.dest] = {
      'env_name': env_name,
      'supported': not unsupported,
      'secret': secret,
      'action': action,
    }
    return action

  def add_exclusive_groups(self, *groups: list[str]) -> None:
    self._exclusive_groups.append(list(groups))

  def _format_group(self, group: list[str]) -> str:
    args = [f'--{a.replace("_", "-")}' for a in group]
    if len(args) == 1:
      return args[0]
    return '{' + ', '.join(args) + '}'

  def _get_formatter(self):  # type: ignore[override]
    formatter = super()._get_formatter()
    group = getattr(self, '_global_group', None)
    if isinstance(formatter, _Formatter) and group is not None:
      formatter._global_ids = frozenset(id(a) for a in group._group_actions)
    return formatter

  @contextlib.contextmanager
  def _env_help_suffixed(self) -> Generator[None]:
    """append `(env: NAME)` to each env-supported action's help for the block —
    only when the last parsed argv carried --allow-env — restoring the originals
    on exit."""
    argv = self._last_argv if self._last_argv is not None else []
    show_env = '--allow-env' in argv
    originals: dict[str, Optional[str]] = {}
    if show_env:
      for dest, info in self._env_info.items():
        if not info['supported']:
          continue
        action = info['action']
        if action.help is argparse.SUPPRESS:
          continue
        originals[dest] = action.help
        env_name = info['env_name']
        if action.help is not None:
          action.help = f'{action.help} (env: {env_name})'
        else:
          action.help = f'(env: {env_name})'
    try:
      yield
    finally:
      for dest, original in originals.items():
        self._env_info[dest]['action'].help = original

  def format_help(self) -> str:
    with self._env_help_suffixed():
      help_text = super().format_help()
    if len(self._exclusive_groups) == 0:
      return help_text
    lines = ['', 'Constraints:']
    for groups in self._exclusive_groups:
      formatted = ' | '.join(self._format_group(g) for g in groups)
      lines.append(f'  {formatted}  (mutually exclusive)')
    return help_text + '\n'.join(lines) + '\n'

  def _check_exclusive_groups(self, namespace: argparse.Namespace) -> None:
    for groups in self._exclusive_groups:
      set_groups: list[list[str]] = []
      for group in groups:
        set_args = [arg for arg in group if bool(getattr(namespace, arg, None))]
        if len(set_args) > 0:
          set_groups.append(set_args)
      if len(set_groups) > 1:
        formatted = ' and '.join(
          '/'.join(f'--{a.replace("_", "-")}' for a in g) for g in set_groups
        )
        self.error(f'arguments {formatted} are mutually exclusive')

  def _cli_provided(self, action: argparse.Action, argv: list[str]) -> bool:
    for opt in action.option_strings:
      if opt in argv:
        return True
      if any(a.startswith(opt + '=') for a in argv):
        return True
    return False

  def _parse_bool_env(self, env_name: str, value: str) -> bool:
    if value == '1':
      return True
    if value == '0':
      return False
    self.error(f'invalid value for env {env_name}: {value!r} (expected "1" or "0")')

  def _apply_env(self, argv: list[str]) -> list[str]:
    mutated = list(argv)
    for dest, info in self._env_info.items():
      action = info['action']
      env_name = info['env_name']
      env_value = os.environ.get(env_name)
      if env_value is None:
        continue
      if not info['supported']:
        raise NotImplementedError(
          f'env-var support not implemented for action on --{dest.replace("_", "-")} '
          f'(env {env_name} is set)'
        )
      # any CLI-provided action on the same dest wins over the env, not just
      # this one — --log must not be overridden by an injected --verbose
      if any(self._cli_provided(a, argv) for a in self._actions if a.dest == action.dest):
        continue
      if _is_nargs_zero(action):
        if self._parse_bool_env(env_name, env_value):
          mutated.append(action.option_strings[0])
      else:
        converter = action.type if action.type is not None else (lambda x: x)
        try:
          converted = converter(env_value)
        except (ValueError, argparse.ArgumentTypeError) as e:
          self.error(f'invalid value for env {env_name}: {env_value!r} ({e})')
        action.default = converted
        action.required = False
    return mutated

  def _format_value(self, value) -> str:
    if value is None:
      return '(none)'
    if isinstance(value, bool):
      return '1' if value else '0'
    if isinstance(value, list):
      return ','.join(str(v) for v in value)
    return str(value)

  def _print_env_table(self, namespace: argparse.Namespace, argv: list[str]) -> None:
    allow_env = getattr(namespace, 'allow_env', False)
    rows: list[tuple[str, str, str, str]] = []
    for dest, info in self._env_info.items():
      if not info['supported']:
        continue
      action = info['action']
      env_name = info['env_name']
      env_raw = os.environ.get(env_name)
      env_was_set = env_raw is not None
      cli_set = self._cli_provided(action, argv)
      if cli_set:
        src = 'A'
      elif allow_env and env_was_set:
        src = 'E'
      else:
        src = 'D'
      current = getattr(namespace, dest, None)
      current_str = self._format_value(current)
      env_str = env_raw if env_was_set else '(not set)'
      if info['secret']:
        current_str = '***' if current is not None else '(none)'
        env_str = '***' if env_was_set else '(not set)'
      rows.append((src, env_name, current_str, env_str))

    headers = ('SRC', 'ENV_NAME', 'CURRENT VALUE', 'ENV VALUE')
    all_rows = [headers] + rows
    widths = [max(len(r[i]) for r in all_rows) for i in range(4)]
    for r in all_rows:
      print('  '.join(r[i].ljust(widths[i]) for i in range(4)).rstrip())

  @overload
  def parse_args(self, args: Iterable[str], namespace: None = ...) -> argparse.Namespace: ...
  @overload
  def parse_args(self, args: Iterable[str], namespace: _N) -> _N: ...
  def parse_args(  # type: ignore[override]
    self, args: Iterable[str], namespace: Optional[_N] = None
  ) -> _N | argparse.Namespace:
    if ic is not None:
      ic.disable()
    argv_list = list(args)
    self._last_argv = argv_list
    # sharing the `log` dest keeps --verbose a pure alias, but it also means
    # _check_exclusive_groups cannot tell the two apart — enforce on the argv
    if self._cli_provided(self._log_action, argv_list) and self._cli_provided(
      self._verbose_action, argv_list
    ):
      self.error('arguments --log and --verbose are mutually exclusive')
    allow_env = '--allow-env' in argv_list
    print_env = '--print-env' in argv_list
    parse_argv = self._apply_env(argv_list) if allow_env else argv_list
    parsed = super().parse_args(parse_argv, namespace)  # pyright: ignore[reportArgumentType]
    assert isinstance(parsed, argparse.Namespace)
    level_name = getattr(parsed, 'log', None)
    if level_name is not None:
      log.set_level(log.level_number(level_name))
    if print_env:
      self._print_env_table(parsed, argv_list)
      sys.exit(0)
    self._check_exclusive_groups(parsed)
    if ic is not None:
      delattr(parsed, 'ic')
    delattr(parsed, 'log')
    delattr(parsed, 'print_env')
    delattr(parsed, 'allow_env')
    return parsed

  def reconstruct(self, namespace, *, prog=None, exclude=()):
    """Reconstruct canonical argv from a parsed namespace.

    Iterates over the parser's actions and builds the command line from the
    namespace values. Global flags (--log, --verbose, --ic, --allow-env,
    --print-env) and help are always excluded. Flags appear in definition order; positionals
    follow.
    """
    get = namespace.get if isinstance(namespace, dict) else lambda d: getattr(namespace, d, None)
    parts = [prog] if isinstance(prog, str) else list(prog if prog is not None else [self.prog])
    global_dests = {a.dest for a in self._global_group._group_actions}
    skip = global_dests | set(exclude)
    flags: list[argparse.Action] = []
    positionals: list[argparse.Action] = []
    for action in self._actions:
      if action.dest in skip or isinstance(action, argparse._SubParsersAction):
        continue
      if len(action.option_strings) > 0:
        flags.append(action)
      else:
        positionals.append(action)
    for action in flags:
      value = get(action.dest)
      if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
        if value != action.default:
          parts.append(action.option_strings[0])
      elif isinstance(action, argparse._AppendAction):
        for item in value if value is not None else []:
          parts.extend([action.option_strings[0], str(item)])
      elif value is not None and value != action.default:
        parts.extend([action.option_strings[0], str(value)])
    for action in positionals:
      value = get(action.dest)
      if isinstance(value, list):
        parts.extend(str(v) for v in value)
      elif value is not None:
        parts.append(str(value))
    return parts

  def set_handler(self, function: Callable) -> 'Parser':
    """register the handler dispatch() calls when this (sub)parser is selected; it
    is invoked with the parsed args as keyword arguments."""
    self.set_defaults(**{_HANDLER_DEST: function})
    return self

  def dispatch(self, argv: list[str]):
    """parse argv and invoke the selected subcommand's handler.

    each subparser registers a handler with set_handler(function); dispatch pops the
    subcommand dest and the handler and calls handler(**remaining_args), returning
    its value. with no subcommand given (optional subparsers) it prints help to
    stderr and returns 1."""
    args = self.parse(argv)
    subparsers_action = next(
      (a for a in self._actions if isinstance(a, argparse._SubParsersAction)), None
    )
    if subparsers_action is None:
      raise RuntimeError('dispatch() requires add_subparsers()')
    if subparsers_action.dest != argparse.SUPPRESS:
      args.pop(subparsers_action.dest, None)
    handler = args.pop(_HANDLER_DEST, None)
    if handler is None:
      self.print_help(sys.stderr)
      return 1
    return handler(**args)

  def parse(self, argv: list[str]) -> dict:
    return vars(self.parse_args(argv[1:]))


@dataclass(frozen=True)
class Argument:
  """one argument an installed command declares, described without argparse.

  `name` is the keyword the command's handler receives it under, `option` the
  option string to spell it with on a command line (None for a positional).
  `kind` is `flag` (present or absent, no value), `value` (one value), or `list`
  (any number of values).
  """

  name: str
  help: str
  required: bool
  kind: Literal['flag', 'value', 'list']
  option: Optional[str]
  choices: tuple[str, ...]
  value_type: Literal['string', 'integer', 'number']


@dataclass(frozen=True)
class CommandSignature:
  command: tuple[str, ...]
  description: str
  arguments: tuple[Argument, ...]


class _ParserBuilt(BaseException):
  """unwinds a `main` at the moment its parser is complete.

  BaseException so a `main` that guards its own body against Exception cannot
  swallow the capture and leave the introspection silently empty-handed.
  """

  def __init__(self, parser: Parser):
    super().__init__('parser captured')
    self.parser = parser


@contextlib.contextmanager
def _parse_intercepted() -> Generator[None]:
  original = Parser.parse

  def intercept(self: Parser, argv: list[str]) -> dict:
    del argv
    raise _ParserBuilt(self)

  Parser.parse = intercept  # type: ignore[method-assign]
  try:
    yield
  finally:
    Parser.parse = original  # type: ignore[method-assign]


def _cli_module(program: str) -> str:
  """the module whose `main` the installed console script `program` runs.

  `sync-scripts` publishes every CLI under two script names — its import path
  with the underscores dashed, and the bare `__cli_name__` alias — both pointing
  at a bridge attribute that is the import path with the dots underscored. The
  attribute therefore picks the path-shaped name out of the pair, and that name
  spells the module.
  """
  import importlib.metadata

  entry_points = list(importlib.metadata.entry_points(group='console_scripts'))
  targets = {entry_point.value for entry_point in entry_points if entry_point.name == program}
  if len(targets) == 0:
    raise ValueError(f'no installed command named {program!r}')
  if len(targets) > 1:
    raise ValueError(
      f'command {program!r} is installed by several distributions: {sorted(targets)}'
    )
  target = next(iter(targets))
  attribute = target.partition(':')[2]
  paths = [
    entry_point.name
    for entry_point in entry_points
    if entry_point.value == target
    and entry_point.name.replace('-', '_').replace('.', '_') == attribute
  ]
  if len(paths) != 1:
    raise ValueError(f'command {program!r} ({target}) is not published as an import path')
  return paths[0].replace('-', '_')


def _built_parser(program: str) -> Parser:
  import importlib

  module_name = _cli_module(program)
  module = importlib.import_module(module_name)
  main = getattr(module, 'main', None)
  if main is None:
    raise ValueError(f'{module_name} has no main to read {program!r} from')
  with _parse_intercepted():
    try:
      main([program])
    except _ParserBuilt as built:
      return built.parser
  raise ValueError(f'{module_name}.main returned without building a parser')


def _subcommands(parser: Parser) -> Optional[argparse._SubParsersAction]:
  return next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)


def _descend(parser: Parser, command: Sequence[str]) -> tuple[Parser, str]:
  """the parser the command's last word selects, plus the summary its parent
  lists it under."""
  summary = ''
  for index, word in enumerate(command[1:], start=1):
    selected = ' '.join(command[:index])
    action = _subcommands(parser)
    if action is None:
      raise ValueError(f'{selected!r} takes no subcommand, so {word!r} names nothing')
    if word not in action.choices:
      raise ValueError(
        f'{selected!r} has no subcommand {word!r}; available: {", ".join(action.choices)}'
      )
    summary = next(
      (choice.help or '' for choice in action._choices_actions if choice.dest == word), ''
    )
    parser = action.choices[word]
    if not isinstance(parser, Parser):
      raise ValueError(f'subcommand {word!r} of {selected!r} is not built on this parser')
  return parser, summary


def _argument(action: argparse.Action) -> Argument:
  if isinstance(action, (argparse._AppendAction, argparse._CountAction)):
    raise ValueError(f'argument {action.dest!r} accumulates across repetitions')
  if action.nargs == 0:
    kind: Literal['flag', 'value', 'list'] = 'flag'
  elif isinstance(action.nargs, int):
    raise ValueError(f'argument {action.dest!r} takes exactly {action.nargs} values')
  elif action.nargs in ('*', '+'):
    kind = 'list'
  elif action.nargs in (None, '?'):
    kind = 'value'
  else:
    raise ValueError(f'argument {action.dest!r} takes {action.nargs!r} values')
  long_options = [option for option in action.option_strings if option.startswith('--')]
  option = None
  if len(action.option_strings) > 0:
    option = long_options[0] if len(long_options) > 0 else action.option_strings[0]
  if action.type is int:
    value_type: Literal['string', 'integer', 'number'] = 'integer'
  elif action.type is float:
    value_type = 'number'
  else:
    value_type = 'string'
  return Argument(
    name=action.dest,
    help=action.help if action.help is not None else '',
    required=bool(action.required),
    kind=kind,
    option=option,
    choices=() if action.choices is None else tuple(str(c) for c in action.choices),
    value_type=value_type,
  )


def command_signature(command: Sequence[str]) -> CommandSignature:
  """the signature of an installed command — its summary and the arguments it
  declares — read from the parser its `main` builds rather than from its help text.

  `command` is the console-script name followed by any subcommands (`('bro',
  'list')`). Only the command's own arguments are described: the repo-wide global
  flags and anything hidden with a SUPPRESS help are left out. Raises when the
  command is not an installed CLI built on this module, when it dispatches
  subcommands of its own rather than doing the work, or when it declares an
  argument shape that cannot be described.
  """
  words = tuple(command)
  if len(words) == 0:
    raise ValueError('a command needs at least a program name')
  parser, summary = _descend(_built_parser(words[0]), words)
  spelled = ' '.join(words)
  action = _subcommands(parser)
  if action is not None:
    raise ValueError(
      f'{spelled!r} dispatches subcommands ({", ".join(action.choices)}); name one of them'
    )
  if len(words) > 1 and _HANDLER_DEST not in parser._defaults:
    # a subcommand that registers no handler is a placeholder its main routes
    # elsewhere before parsing, so the arguments it really takes are declared
    # nowhere this walk can reach.
    raise ValueError(f'{spelled!r} registers no handler; its arguments are declared elsewhere')
  description = parser.description if parser.description is not None else summary
  if len(description) == 0:
    raise ValueError(f'{spelled!r} describes itself nowhere; give it a description or a help line')
  global_actions = {id(a) for a in parser._global_group._group_actions}
  arguments = tuple(
    _argument(action)
    for action in parser._actions
    if id(action) not in global_actions and action.help is not SUPPRESS
  )
  return CommandSignature(command=words, description=description, arguments=arguments)
