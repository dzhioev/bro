#!/usr/bin/env python

import argparse
from base.time_util import parse_moment, datetime
from icecream import ic
from typing import Callable, Type
import logging


def moment_parser(arg: str) -> datetime:
  try:
    return parse_moment(arg)
  except ValueError:
    raise argparse.ArgumentTypeError(f'invalid date format: {arg}')


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


class Parser(argparse.ArgumentParser):
  def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)

    self.add_argument(
      '--verbose', action=trigger(enable_verbose_logging), help='enable verbose logging'
    )

  def parse_args(self, args=None, namespace=None):
    ns = super().parse_args(args, namespace)
    delattr(ns, 'verbose')
    return ns

  def parse(self, argv):
    return vars(self.parse_args(argv[1:]))
