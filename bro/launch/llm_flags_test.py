import json

import pytest

from bro.base import host_config
from bro.base.args import Parser
from bro.launch import llm_flags
from bro.llm.llms import claude_code, openai as openai_llm
from bro.llm.providers import LLMSelection, LLMSelectionError


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


class TestPresets:
  @pytest.fixture(autouse=True)
  def _stores(self, monkeypatch, tmp_path):
    monkeypatch.setattr(host_config, 'HOST_CONFIG_FILE', tmp_path / 'bro.json')
    monkeypatch.setattr(
      'bro.workspace.project.project_config',
      lambda: _FakeProjectConfig({'sharp': 'openai:sol:max', 'cheap': ':terra'}),
    )
    self.host_file = tmp_path / 'bro.json'

  def test_a_project_preset_expands(self):
    args = _args(['--llm', 'sharp'])
    assert llm_flags.selection_from_args(args) == LLMSelection('openai', 'sol', 'max')

  def test_the_host_table_overrides_the_project_per_name(self):
    self.host_file.write_text(json.dumps({'llm': {'sharp': ':fable5'}}))
    args = _args(['--llm', 'sharp'])
    assert llm_flags.selection_from_args(args) == LLMSelection(model='fable5')
    # the name the host doesn't restate still comes from the project
    assert llm_flags.selection_from_args(_args(['--llm', 'cheap'])) == LLMSelection(model='terra')

  def test_a_name_no_table_carries_is_read_as_a_recipe(self):
    assert llm_flags.selection_from_args(_args(['--llm', ':terra'])) == LLMSelection(model='terra')

  def test_a_recipe_is_read_without_a_project_around_it(self, monkeypatch):
    def no_project():
      raise FileNotFoundError('git')

    monkeypatch.setattr('bro.workspace.project.project_config', no_project)

    assert llm_flags.selection_from_args(_args(['--llm', ':terra'])) == LLMSelection(model='terra')

  def test_a_malformed_preset_names_itself_in_the_error(self):
    self.host_file.write_text(json.dumps({'llm': {'broken': '::ludicrous'}}))
    with pytest.raises(LLMSelectionError, match="preset 'broken'"):
      llm_flags.selection_from_args(_args(['--llm', 'broken']))


class _FakeProjectConfig:
  def __init__(self, presets: dict):
    self.sections = {'llm': presets}


class TestSurfaceGuards:
  def test_a_native_launcher_refuses_a_self_driving_harness(self):
    with pytest.raises(LLMSelectionError, match='cw ss --raw'):
      llm_flags.resolve_native(openai_llm.LLMSpec(), LLMSelection(model='fable5'))

  def test_a_claude_session_refuses_an_api_provider(self):
    with pytest.raises(LLMSelectionError, match='bro run'):
      llm_flags.resolve_claude(LLMSelection(provider='openai'))

  def test_a_claude_session_defaults_to_claude_codes_own_recipe(self):
    assert llm_flags.resolve_claude(LLMSelection()) == claude_code.LLMSpec()
