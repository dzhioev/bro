#!/usr/bin/env python
from typing import Optional

from base.args import Parser


def template() -> None:
  pass


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description='')
  return template(**parser.parse(argv))
