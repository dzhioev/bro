"""The complete recipe flag shared by launch surfaces."""

from bro.base.args import Parser

LLM_HELP = (
  'complete recipe text: provider:model[+passthrough][,field=value]...; '
  'replaces the configured recipe whole'
)


def add_llm_flags(parser: Parser, *, required: bool = False) -> None:
  parser.add_argument('--llm', default=None, required=required, metavar='RECIPE', help=LLM_HELP)
