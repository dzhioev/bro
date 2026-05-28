import sys

from bro.bro import BaseBro
from do._cli import run
from llm.tracer import Tracer

__cli_name__ = 'ask'


async def do(bro: BaseBro, what: str, tracer: Tracer | None = None) -> str:
  return await bro.run(what, tracer=tracer)


def main(argv=None) -> int | None:
  return run(
    cli_name='ask',
    parser_desc='run a bro on the given input',
    arg_name='what',
    arg_help='input to send to the bro',
    run_fn=do,
    argv=argv,
  )


if __name__ == '__main__':
  sys.exit(main(sys.argv))
