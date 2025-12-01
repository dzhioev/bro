#!/usr/bin/env python
from base.args import Parser
import sys


def template() -> None:
  pass


def main(argv):
  parser = Parser(description='')
  return template(**parser.parse(argv))


if __name__ == '__main__':
  sys.exit(main(sys.argv))
