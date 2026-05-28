import sys

from bro.bro import BaseBro
from do._cli import run
from do.do import do
from llm.tracer import Tracer

__cli_name__ = 'do-task'


async def do_task(bro: BaseBro, task: str, tracer: Tracer | None = None) -> str:
  # the task argument is opaque to us — id, dashed UUID, Notion URL, or free text
  # describing the task. the bro's system prompt is responsible for normalising it
  return await do(bro, f'fix the flow task: {task}', tracer=tracer)


def main(argv=None) -> int | None:
  return run(
    cli_name='do-task',
    parser_desc='run a bro on a flow task',
    arg_name='task',
    arg_help='flow task reference: id, dashed UUID, Notion URL, or description',
    run_fn=do_task,
    argv=argv,
  )


if __name__ == '__main__':
  sys.exit(main(sys.argv))
