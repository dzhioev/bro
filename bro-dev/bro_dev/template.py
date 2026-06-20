#!/usr/bin/env python
from base.args import Parser


def template() -> None:
  pass


def main(argv: list[str]) -> int | None:
  parser = Parser(description='')
  return template(**parser.parse(argv))
