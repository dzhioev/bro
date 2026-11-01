import pytest

from bro.base.args import Parser
from bro.launch.llm_flags import add_llm_flags


def parser() -> Parser:
  result = Parser(add_help=False)
  add_llm_flags(result)
  return result


def test_recipe_text_is_forwarded_whole():
  text = 'provider:model+passthrough,field=value'
  arguments = vars(parser().parse_args(['--llm', text]))
  assert arguments == {'llm': text}
  assert parser().reconstruct(arguments, prog=[]) == ['--llm', text]


def test_omitted_recipe_forwards_nothing():
  arguments = vars(parser().parse_args([]))
  assert arguments == {'llm': None}
  assert parser().reconstruct(arguments, prog=[]) == []


@pytest.mark.parametrize('flag', ['--provider', '--model', '--effort', '--fast', '--passthrough'])
def test_piece_flags_are_not_launch_options(flag):
  with pytest.raises(SystemExit):
    parser().parse_args([flag, 'value'])
