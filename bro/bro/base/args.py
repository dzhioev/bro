#!/usr/bin/env python

import argparse
from base.time_util import parse_moment, datetime
from icecream import ic
from typing import Callable, Type, Sequence
import logging


def moment_parser(arg: str) -> datetime:
  try:
    return parse_moment(arg)
  except ValueError as e:
    raise argparse.ArgumentTypeError(f'can\'t parse: "{arg}" exception: "{str(e)}"')


def trigger(fn: Callable) -> Type[argparse.Action]:
  class TriggerAction(argparse.Action):
    def __init__(self, option_strings, dest, **kwargs):
      kwargs.setdefault('nargs', 0)
      super().__init__(option_strings, dest, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
      fn()

  return TriggerAction


def enable_verbose_logging() -> None:
  logging.basicConfig(level=logging.DEBUG)


def enable_ic() -> None:
  ic.enable()


class Parser(argparse.ArgumentParser):
  def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)

    self.add_argument(
      '--verbose', action=trigger(enable_verbose_logging), help='enable verbose logging'
    )
    self.add_argument('--ic', action=trigger(enable_ic), help='enable ic ouptput')

  def parse_args(
    self, args: Sequence[str] | None = None, namespace: None = None
  ) -> argparse.Namespace:
    ic.disable()
    ns = super().parse_args(args, namespace)
    delattr(ns, 'ic')
    delattr(ns, 'verbose')
    return ns

  def parse(self, argv):
    return vars(self.parse_args(argv[1:]))
