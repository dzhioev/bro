#!/usr/bin/env python

import argparse
from base.time_util import parse_moment, Moment
from icecream import ic
from typing import Callable, Type, Sequence, TypeVar, overload
import logging


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


def enable_logging(level: int) -> Callable:
  def enable() -> None:
    logging.basicConfig(level=level, force=True)

  return enable


def enable_ic() -> None:
  ic.enable()


_N = TypeVar('_N')


class Parser(argparse.ArgumentParser):
  def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)
    self._exclusive_groups: list[list[list[str]]] = []
    self.add_argument(
      '--verbose', action=trigger(enable_logging(logging.DEBUG)), help='enable verbose logging'
    )
    self.add_argument(
      '--info', action=trigger(enable_logging(logging.INFO)), help='enable info logging'
    )
    self.add_argument('--ic', action=trigger(enable_ic), help='enable ic ouptput')

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

  def _check_exclusive_groups(self, ns: _N | argparse.Namespace) -> None:
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
    ns = super().parse_args(args, namespace)
    self._check_exclusive_groups(ns)
    delattr(ns, 'ic')
    delattr(ns, 'info')
    delattr(ns, 'verbose')
    return ns

  def parse(self, argv):
    return vars(self.parse_args(argv[1:]))
