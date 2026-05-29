import re
import sys

from bro.bro import BaseBro
from do._cli import run
from llm.tracer import Tracer

__cli_name__ = 'ask'

_SKILL_INVOCATION = re.compile(r'^/([a-zA-Z][\w-]*)(?:\s+(.*))?\Z', re.DOTALL)


def _expand_skill_invocation(bro: BaseBro, what: str) -> str:
  # `/<skill-name> <args>` in input → swap in the skill's markdown body and
  # surface the rest as `ARGUMENTS:`. Same shape Claude Code uses for slash
  # commands, so a body authored for either surface works on both. Unknown
  # `/<name>` raises KeyError (with the available-skill list) from
  # bro.get_skill_body — the CLI catches it.
  match = _SKILL_INVOCATION.match(what)
  if match is None:
    return what
  name, args = match.group(1), match.group(2)
  body = bro.get_skill_body(name)
  if args is None or args.strip() == '':
    return body
  return f'{body}\n\nARGUMENTS: {args.strip()}'


async def do(bro: BaseBro, what: str, tracer: Tracer | None = None) -> str:
  return await bro.run(_expand_skill_invocation(bro, what), tracer=tracer)


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
