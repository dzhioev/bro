#!/usr/bin/env python

import argparse
import os
import sys
from base.time_util import parse_moment, Moment
from icecream import ic
from typing import Callable, Type, Sequence, TypeVar, overload
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
  if not long_opts:
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


class Parser(argparse.ArgumentParser):
  def __init__(self, *args, **kwargs):
    self._exclusive_groups: list[list[list[str]]] = []
    self._env_info: dict[str, dict] = {}
    super().__init__(*args, **kwargs)
    super().add_argument(
      '--allow-env', action='store_true', help='honor env-var overrides for flags'
    )
    super().add_argument('--print-env', action='store_true', help='print env-var summary and exit')
    self.add_argument(
      '--verbose', action=trigger(set_log_level(logging.DEBUG)), help='enable verbose logging'
    )
    self.add_argument('--ic', action=trigger(enable_ic), help='enable ic ouptput')

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
    if not unsupported:
      current_help = action.help
      if current_help is argparse.SUPPRESS:
        pass
      elif current_help:
        action.help = f'{current_help} (env: {env_name})'
      else:
        action.help = f'(env: {env_name})'

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

  def format_help(self) -> str:
    help_text = super().format_help()
    if not self._exclusive_groups:
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
        set_args = [arg for arg in group if getattr(ns, arg, None)]
        if set_args:
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
    self, args: Sequence[str] | None = ..., namespace: None = ...
  ) -> argparse.Namespace: ...
  @overload
  def parse_args(self, args: Sequence[str] | None, namespace: _N) -> _N: ...
  @overload
  def parse_args(self, *, namespace: _N) -> _N: ...
  def parse_args(
    self, args: Sequence[str] | None = None, namespace: _N | None = None
  ) -> _N | argparse.Namespace:
    ic.disable()
    argv_list = list(args) if args is not None else list(sys.argv[1:])
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

  def parse(self, argv=None):
    if argv is None:
      argv = sys.argv
    return vars(self.parse_args(argv[1:]))
