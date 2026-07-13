from typing import Optional

from bro.bro import BaseBro
from do._cli import run
from do.do import do
from llm.observer import Observer

__cli_name__ = 'do-task'


def fix_invocation(task: str) -> str:
  # the `/fix` wrapping that makes `do-task <bro> <ref>` shorthand for
  # `ask <bro> /fix <ref>`. An already-slash-prefixed input passes straight
  # through so users can override (e.g. `do-task ppp-dev "/fix --focus"`); the
  # check is on the first character, so leading whitespace before `/` gets
  # wrapped like any other ref.
  if task.startswith('/'):
    return task
  return f'/fix {task}'


async def do_task(bro: BaseBro, task: str, observer: Optional[Observer] = None) -> str:
  # whether the bro actually has a `fix` skill is its own business — it raises
  # if not.
  return await do(bro, fix_invocation(task), observer=observer)


def main(argv: list[str]) -> Optional[int]:
  return run(
    cli_name='do-task',
    parser_description='run a bro on a flow task',
    arg_name='task',
    arg_help='flow task reference: id, dashed UUID, Notion URL, or description',
    run_function=do_task,
    argv=argv,
    export_task_id=True,
    # a relayed child runs plain `ask`, so the wrapping happens before the send
    relay_prompt=fix_invocation,
  )
