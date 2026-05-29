import sys

from bro.bro import BaseBro
from do._cli import run
from do.do import do
from llm.tracer import Tracer

__cli_name__ = 'do-task'


async def do_task(bro: BaseBro, task: str, tracer: Tracer | None = None) -> str:
  # `do-task <bro> <ref>` is shorthand for `ask <bro> /fix <ref>`. Pass an
  # already-slash-prefixed input straight through so users can override (e.g.
  # `do-task ppp-dev "/fix --focus"`).
  what = task if task.lstrip().startswith('/') else f'/fix {task}'
  return await do(bro, what, tracer=tracer)


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
