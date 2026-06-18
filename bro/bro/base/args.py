#!/usr/bin/env python

import argparse
import os
import sys
from base.time_util import parse_moment, Moment
from icecream import ic
from collections.abc import Iterable, Sequence
from typing import Callable, Type, TypeVar, overload
import logging

from base import log


def moment_parser(arg: str) -> Moment:
  try:
    return parse_moment(arg)
  except ValueError as e:
    raise argparse.ArgumentTypeError(f'can\'t parse: "{arg}" exception: "{str(e)}"')


def list_parser(arg: str) -> list[str]:
  return [item.strip() for item in arg.split(',')]


def trigger(fn: Callable) -> Type[argparse.Action]:
  class TriggerAction(argparse.Action):
    def __init__(self, option_strings, dest, **kwargs):
      kwargs.setdefault('nargs', 0)
      super().__init__(option_strings, dest, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
      del parser, namespace, values, option_string
      fn()

  return TriggerAction


def set_log_level(level: int) -> Callable:
  def enable() -> None:
    log.set_level(level)

  return enable


def enable_ic() -> None:
  ic.enable()


_N = TypeVar('_N')


def _default_env_name(option_strings: Sequence[str]) -> str | None:
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
  _global_ids: set[int] = set()

  def _format_usage(self, usage, actions, groups, prefix):  # type: ignore[override]
    global_ids = self._global_ids
    script = [a for a in actions if id(a) not in global_ids]
    glob = [a for a in actions if id(a) in global_ids]
    result = super()._format_usage(usage, script, groups, prefix)
    if len(glob) == 0:
      return result
    glob_str = self._format_actions_usage(glob, []).strip()  # type: ignore[attr-defined]
    if glob_str == '':
      return result
    body = result.rstrip('\n')
    lines = body.split('\n')
    if len(lines) > 1:
      indent_len = len(lines[1]) - len(lines[1].lstrip())
    else:
      actual_prefix = prefix if prefix is not None else 'usage: '
      indent_len = len(actual_prefix) + len(self._prog) + 1
    indent = ' ' * indent_len
    return f'{body}\n{indent}({glob_str})\n'


class Parser(argparse.ArgumentParser):
  def __init__(self, *args, **kwargs):
    self._exclusive_groups: list[list[list[str]]] = []
    self._env_info: dict[str, dict] = {}
    self._last_argv: list[str] | None = None
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
    self._add_global_argument(
      '--verbose', action=trigger(set_log_level(logging.DEBUG)), help='enable verbose logging'
    )
    self._add_global_argument('--ic', action=trigger(enable_ic), help='enable ic ouptput')

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
      formatter._global_ids = {id(a) for a in group._group_actions}
    return formatter

  def format_help(self) -> str:
    argv = self._last_argv if self._last_argv is not None else sys.argv[1:]
    show_env = '--allow-env' in argv
    originals: dict[str, str | None] = {}
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
      help_text = super().format_help()
    finally:
      for dest, original in originals.items():
        self._env_info[dest]['action'].help = original
    if len(self._exclusive_groups) == 0:
      return help_text
    lines = ['', 'Constraints:']
    for groups in self._exclusive_groups:
      formatted = ' | '.join(self._format_group(g) for g in groups)
      lines.append(f'  {formatted}  (mutually exclusive)')
    return help_text + '\n'.join(lines) + '\n'

  def _check_exclusive_groups(self, ns: argparse.Namespace) -> None:
    for groups in self._exclusive_groups:
      set_groups: list[list[str]] = []
      for group in groups:
        set_args = [arg for arg in group if bool(getattr(ns, arg, None))]
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
      env_val = os.environ.get(env_name)
      if env_val is None:
        continue
      if not info['supported']:
        raise NotImplementedError(
          f'env-var support not implemented for action on --{dest.replace("_", "-")} '
          f'(env {env_name} is set)'
        )
      if self._cli_provided(action, argv):
        continue
      if _is_nargs_zero(action):
        if self._parse_bool_env(env_name, env_val):
          mutated.append(action.option_strings[0])
      else:
        converter = action.type if action.type is not None else (lambda x: x)
        try:
          converted = converter(env_val)
        except (ValueError, argparse.ArgumentTypeError) as e:
          self.error(f'invalid value for env {env_name}: {env_val!r} ({e})')
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

  def _print_env_table(self, ns: argparse.Namespace, argv: list[str]) -> None:
    allow_env = getattr(ns, 'allow_env', False)
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
      current = getattr(ns, dest, None)
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
  def parse_args(
    self, args: Iterable[str] | None = ..., namespace: None = ...
  ) -> argparse.Namespace: ...
  @overload
  def parse_args(self, args: Iterable[str] | None, namespace: _N) -> _N: ...
  @overload
  def parse_args(self, *, namespace: _N) -> _N: ...
  def parse_args(
    self, args: Iterable[str] | None = None, namespace: _N | None = None
  ) -> _N | argparse.Namespace:
    ic.disable()
    argv_list = list(args) if args is not None else list(sys.argv[1:])
    self._last_argv = argv_list
    allow_env = '--allow-env' in argv_list
    print_env = '--print-env' in argv_list
    parse_argv = self._apply_env(argv_list) if allow_env else argv_list
    ns = super().parse_args(parse_argv, namespace)  # pyright: ignore[reportArgumentType]
    assert isinstance(ns, argparse.Namespace)
    if print_env:
      self._print_env_table(ns, argv_list)
      sys.exit(0)
    self._check_exclusive_groups(ns)
    delattr(ns, 'ic')
    delattr(ns, 'verbose')
    delattr(ns, 'print_env')
    delattr(ns, 'allow_env')
    return ns

  def reconstruct(self, ns, *, prog=None, exclude=()):
    """Reconstruct canonical argv from a parsed namespace.

    Iterates over the parser's actions and builds the command line from the
    namespace values. Global flags (--verbose, --ic, --allow-env, --print-env)
    and help are always excluded. Flags appear in definition order; positionals
    follow.
    """
    get = ns.get if isinstance(ns, dict) else lambda d: getattr(ns, d, None)
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
      val = get(action.dest)
      if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
        if val != action.default:
          parts.append(action.option_strings[0])
      elif isinstance(action, argparse._AppendAction):
        for item in val if val is not None else []:
          parts.extend([action.option_strings[0], str(item)])
      elif val is not None and val != action.default:
        parts.extend([action.option_strings[0], str(val)])
    for action in positionals:
      val = get(action.dest)
      if isinstance(val, list):
        parts.extend(str(v) for v in val)
      elif val is not None:
        parts.append(str(val))
    return parts

  def parse(self, argv=None):
    if argv is None:
      argv = sys.argv
    return vars(self.parse_args(argv[1:]))
