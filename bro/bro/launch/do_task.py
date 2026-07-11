from typing import Optional

from bro.bro import BaseBro
from do._cli import run
from do.do import do
from llm.observer import Observer

__cli_name__ = 'do-task'


async def do_task(bro: BaseBro, task: str, observer: Optional[Observer] = None) -> str:
  # `do-task <bro> <ref>` is shorthand for `ask <bro> /fix <ref>`. Pass an
  # already-slash-prefixed input straight through so users can override (e.g.
  # `do-task ppp-dev "/fix --focus"`); the check is on the first character, so
  # leading whitespace before `/` gets wrapped like any other ref. Whether the
  # bro actually has a `fix` skill is its own business — it raises if not.
  if not task.startswith('/'):
    task = f'/fix {task}'
  return await do(bro, task, observer=observer)


def main(argv: list[str]) -> Optional[int]:
  return run(
    cli_name='do-task',
    parser_description='run a bro on a flow task',
    arg_name='task',
    arg_help='flow task reference: id, dashed UUID, Notion URL, or description',
    run_function=do_task,
    argv=argv,
    export_task_id=True,
  )
