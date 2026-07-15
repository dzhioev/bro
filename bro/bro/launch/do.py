from typing import Optional

from bro.bro import BaseBro
from do._cli import run_main
from llm.observer import Observer

__cli_name__ = 'ask'


async def do(bro: BaseBro, what: str, observer: Optional[Observer] = None) -> str:
  return await bro.run(what, observer=observer)


def main(argv: list[str]) -> Optional[int]:
  return run_main(argv, program=['ask'])
