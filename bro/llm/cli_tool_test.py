import asyncio

import pytest

from bro.base.args import Argument, CommandSignature
from bro.llm.cli_tool import _CommandTool, build_server
from bro.llm.mcp import sh


def _tool(command: str, *arguments: str) -> _CommandTool:
  server = sh(command, *arguments).server_specs[0].build()
  tool = asyncio.run(server.list_tools())[0]
  assert isinstance(tool, _CommandTool)
  return tool


def _argument(name: str, **overrides) -> Argument:
  fields: dict = {
    'name': name,
    'help': f'{name} help',
    'required': False,
    'kind': 'value',
    'option': f'--{name.replace("_", "-")}',
    'choices': (),
    'value_type': 'string',
  }
  fields.update(overrides)
  return Argument(**fields)


def _synthetic(*arguments: Argument) -> _CommandTool:
  signature = CommandSignature(command=('tool', 'sub'), description='does a thing', arguments=())
  return _CommandTool('tool_sub', signature, arguments)


class TestDeclaration:
  def test_words_become_the_tool_name(self):
    assert _tool('bro list').name == 'bro_list'

  def test_dotted_program_stays_a_valid_segment(self):
    assert _tool('bro.run list').name == 'bro_run_list'

  @pytest.mark.parametrize('command', ['bro list; rm -rf /', 'bro | tee out', '$(bro) list'])
  def test_shell_syntax_is_not_a_command(self, command):
    with pytest.raises(ValueError, match='not a command word'):
      sh(command)

  def test_empty_command_raises(self):
    with pytest.raises(ValueError, match='needs a command'):
      sh('   ')

  def test_declaration_neither_reads_nor_runs_the_command(self):
    sh('definitely-not-installed subcommand')


class TestGeneratedSurface:
  def test_globals_are_not_arguments(self):
    tool = _tool('bro list')
    assert tool.parameters == {'type': 'object', 'properties': {}, 'required': []}

  def test_description_carries_the_command_summary(self):
    assert _tool('bro list').description.startswith('list registered bros')

  def test_schema_follows_the_declared_arguments(self):
    parameters = _tool('bro show').parameters
    assert parameters['required'] == ['name']
    assert parameters['properties']['name'] == {'type': 'string', 'description': 'bro name'}
    assert parameters['properties']['system_prompt']['type'] == 'boolean'

  def test_choices_become_an_enum_and_int_type_carries(self):
    properties = _tool('rewind grep').parameters['properties']
    assert properties['color']['enum'] == ['auto', 'always', 'never']
    assert properties['after_context']['type'] == 'integer'
    assert properties['trails'] == {
      'type': 'array',
      'items': {'type': 'string'},
      'description': 'trail ids to search (default: every trail, newest first)',
    }

  def test_exposure_narrows_to_the_named_arguments(self):
    assert set(_tool('bro show', 'name').parameters['properties']) == {'name'}

  def test_exposure_keeps_the_commands_own_argument_order(self):
    tool = _tool('rewind grep', 'trails', 'color', 'pattern')
    assert tool._argv({'pattern': 'p', 'trails': ['a'], 'color': 'never'}) == [
      'rewind',
      'grep',
      '--color=never',
      '--',
      'p',
      'a',
    ]

  def test_exposure_cannot_omit_a_required_argument(self):
    with pytest.raises(ValueError, match='requires name'):
      _tool('bro show', 'system_prompt')

  def test_unknown_exposed_argument_raises(self):
    with pytest.raises(ValueError, match='no arguments'):
      _tool('bro show', 'nope')

  def test_unreadable_command_raises_at_build(self):
    with pytest.raises(ValueError, match='no installed command'):
      build_server(('definitely-not-installed',), (), 'x')


class TestArgv:
  def test_long_options_attach_their_value(self):
    tool = _synthetic(_argument('limit'))
    assert tool._argv({'limit': 5}) == ['tool', 'sub', '--limit=5']

  def test_short_only_option_passes_its_value_separately(self):
    tool = _synthetic(_argument('depth', option='-d'))
    assert tool._argv({'depth': '2'}) == ['tool', 'sub', '-d', '2']

  def test_flags_appear_only_when_true(self):
    tool = _synthetic(_argument('quiet', kind='flag'))
    assert tool._argv({'quiet': True}) == ['tool', 'sub', '--quiet']
    assert tool._argv({'quiet': False}) == ['tool', 'sub']

  def test_omitted_optionals_are_omitted(self):
    tool = _synthetic(_argument('limit'), _argument('color'))
    assert tool._argv({'color': 'never'}) == ['tool', 'sub', '--color=never']

  def test_positionals_follow_a_separator(self):
    tool = _synthetic(
      _argument('pattern', option=None, required=True), _argument('quiet', kind='flag')
    )
    assert tool._argv({'pattern': '-x', 'quiet': True}) == ['tool', 'sub', '--quiet', '--', '-x']

  def test_list_arguments_repeat(self):
    tool = _synthetic(_argument('trails', kind='list', option=None))
    assert tool._argv({'trails': ['a', 'b']}) == ['tool', 'sub', '--', 'a', 'b']

  def test_unknown_argument_raises(self):
    with pytest.raises(ValueError, match='unknown arguments: nope'):
      _synthetic(_argument('limit'))._argv({'nope': 1})

  def test_missing_required_argument_raises(self):
    with pytest.raises(ValueError, match='missing required'):
      _synthetic(_argument('pattern', option=None, required=True))._argv({})

  def test_value_outside_the_choices_raises(self):
    tool = _synthetic(_argument('color', choices=('auto', 'never')))
    with pytest.raises(ValueError, match='must be one of'):
      tool._argv({'color': 'pink'})

  def test_non_boolean_flag_raises(self):
    tool = _synthetic(_argument('quiet', kind='flag'))
    with pytest.raises(ValueError, match='pass a boolean'):
      tool._argv({'quiet': 'yes'})

  def test_list_for_a_single_valued_argument_raises(self):
    with pytest.raises(ValueError, match='takes one value'):
      _synthetic(_argument('limit'))._argv({'limit': [1, 2]})


class TestCall:
  def test_running_the_command_returns_its_exit_code_and_output(self):
    result = asyncio.run(_tool('bro list').call({}))
    assert result.startswith('exit_code: 0\n')
    assert 'lead:' in result

  def test_arguments_reach_the_command(self):
    result = asyncio.run(_tool('bro show').call({'name': 'lead'}))
    assert result.startswith('exit_code: 0\n# lead')
