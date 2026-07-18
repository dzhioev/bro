from typing import Optional

from bro.launch._cli import run_main

__cli_name__ = 'ask'


def main(argv: list[str]) -> Optional[int]:
  return run_main(argv, program=['ask'], implied_fast=True)
