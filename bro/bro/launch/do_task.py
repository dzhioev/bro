from typing import Optional

from bro.bro import BaseBro
from do._cli import run_main
from do.do import do
from llm.observer import Observer

__cli_name__ = 'do-task'


def fix_invocation(task: str) -> str:
  if task.startswith('/'):
    return task
  return f'/fix {task}'


async def do_task(bro: BaseBro, task: str, observer: Optional[Observer] = None) -> str:
  return await do(bro, fix_invocation(task), observer=observer)


def main(argv: list[str]) -> Optional[int]:
  return run_main(
    argv,
    program=['do-task'],
    input_transform=fix_invocation,
    export_task_id=True,
  )
