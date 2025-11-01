#!/usr/bin/env python
from base.args import Parser
import sys


def template() -> None:
  pass


def main(argv):
  parser = Parser(description='')
  kwargs = parser.parse(argv)
  return template(**kwargs)


if __name__ == '__main__':
  sys.exit(main(sys.argv))
