import pytest

from bro.base.args import Parser
from bro.launch import llm_flags
from bro.llm.providers import LLMSelection


def _parser() -> Parser:
  parser = Parser(add_help=False)
  llm_flags.add_llm_flags(parser, effort_help='effort', fast_help='fast')
  return parser


def _args(argv: list[str]) -> dict:
  return vars(_parser().parse_args(argv))


class TestFlagSet:
  def test_llm_excludes_the_flags_it_speaks_for(self):
    for conflicting in (
      ['--provider', 'openai'],
      ['--model', 'sol'],
      ['--effort', 'high'],
      ['--fast'],
    ):
      with pytest.raises(SystemExit):
        _parser().parse_args(['--llm', ':sol', *conflicting])

  def test_the_pieces_combine_with_each_other(self):
    args = _args(['--provider', 'openai', '--model', 'sol', '--effort', 'max', '--fast'])
    assert llm_flags.selection_from_args(args) == LLMSelection('openai', 'sol', 'max', True)

  def test_everything_defaults_to_an_empty_selection(self):
    assert llm_flags.selection_from_args(_args([])).is_empty()


class TestCanonicalize:
  def test_the_pieces_collapse_into_one_forwarded_flag(self):
    args = _args(['--provider', 'openai', '--effort', 'max', '--fast'])
    assert llm_flags.canonicalize(args, llm_flags.selection_from_args(args)) == 'openai::max+fast'
    # the pieces go back to their parser defaults, so an argv rebuilt from the
    # namespace carries the canonical value alone
    assert _parser().reconstruct(args, prog=[]) == ['--llm', 'openai::max+fast']

  def test_an_empty_selection_forwards_nothing(self):
    args = _args([])
    assert llm_flags.canonicalize(args, llm_flags.selection_from_args(args)) is None
    assert _parser().reconstruct(args, prog=[]) == []

  def test_dropping_the_pieces_leaves_only_the_canonical_value(self):
    args = _args(['--fast'])
    llm_flags.canonicalize(args, llm_flags.selection_from_args(args))
    llm_flags.drop_piece_flags(args)
    assert args == {'llm': '+fast'}


def test_passthrough_piece_flag_canonicalizes_and_excludes_whole_recipe():
  args = _args(['--provider', 'openai', '--model', 'gpt-7', '--effort', 'high', '--passthrough'])
  selection = llm_flags.selection_from_args(args)
  assert selection.passthrough
  assert llm_flags.canonicalize(args, selection) == 'openai:gpt-7:high+passthrough'
  assert _parser().reconstruct(args, prog=[]) == ['--llm', selection.format()]
  with pytest.raises(SystemExit):
    _args(['--llm', 'openai', '--passthrough'])
