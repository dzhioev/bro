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
    self.add_argument(
      '--verbose', action=trigger(enable_logging(logging.DEBUG)), help='enable verbose logging'
    )
    self.add_argument(
      '--info', action=trigger(enable_logging(logging.INFO)), help='enable info logging'
    )
    self.add_argument('--ic', action=trigger(enable_ic), help='enable ic ouptput')

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
    delattr(ns, 'ic')
    delattr(ns, 'info')
    delattr(ns, 'verbose')
    return ns

  def parse(self, argv):
    return vars(self.parse_args(argv[1:]))
