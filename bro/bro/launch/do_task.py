from bro.bro import BaseBro
from do._cli import run
from do.do import do
from llm.observer import Observer

__cli_name__ = 'do-task'


async def do_task(bro: BaseBro, task: str, observer: Observer | None = None) -> str:
  # `do-task <bro> <ref>` is shorthand for `ask <bro> /fix <ref>`. Pass an
  # already-slash-prefixed input straight through so users can override (e.g.
  # `do-task ppp-dev "/fix --focus"`). Matches `do.py`'s `_SKILL_INVOCATION`
  # regex — leading whitespace before `/` is not a slash invocation in either
  # surface.
  if not task.startswith('/'):
    if 'fix' not in bro.skills:
      raise KeyError(f"bro {bro.name!r} has no 'fix' skill — use 'ask' instead")
    task = f'/fix {task}'
  return await do(bro, task, observer=observer)


def main(argv: list[str]) -> int | None:
  return run(
    cli_name='do-task',
    parser_desc='run a bro on a flow task',
    arg_name='task',
    arg_help='flow task reference: id, dashed UUID, Notion URL, or description',
    run_fn=do_task,
    argv=argv,
  )
