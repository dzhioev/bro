from typing import Optional

from bro.launch._cli import run_main

__cli_name__ = 'do-task'


def fix_invocation(task: str) -> str:
  if task.startswith('/'):
    return task
  return f'/fix {task}'


def main(argv: list[str]) -> Optional[int]:
  return run_main(
    argv,
    program=['do-task'],
    input_transform=fix_invocation,
    export_task_id=True,
  )
