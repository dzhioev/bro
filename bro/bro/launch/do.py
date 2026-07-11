from typing import Optional

from bro.bro import BaseBro
from do._cli import run
from llm.observer import Observer

__cli_name__ = 'ask'


async def do(bro: BaseBro, what: str, observer: Optional[Observer] = None) -> str:
  # input passes through verbatim, `/skill` invocations included: the bro's
  # system prompt describes the /-syntax and the model loads the body itself
  # via the `bro::skill` tool (an unknown name fails there and the bro raises).
  return await bro.run(what, observer=observer)


def main(argv: list[str]) -> Optional[int]:
  return run(
    cli_name='ask',
    parser_description='run a bro on the given input',
    arg_name='what',
    arg_help='input to send to the bro',
    run_function=do,
    argv=argv,
  )
