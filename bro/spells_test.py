import json
import sys
import types
from pathlib import Path
from typing import Optional, cast

import pytest

import bro.llm.mcp as llm_mcp
import bro.mcp as mcp
from bro import bro as bro_module, spells as spell_store
from bro.base.condition import SetVariable
from bro.base.text_window import window
from bro.bro import BaseBro
from bro.harness import get_harness, installed_harness_names
from bro.llm.mcp import InProcessMCPServer, ToolRegistry
from bro.mcp import MCPServerSpec, creds
from bro.prompts import get_prompt
from bro.registry import create_bro, declared_specs
from bro.spells import CAST_SECRET, NAMESPACE, load_spell
from bro.spells_test_helper import SpellPackage, spell_package


def _spell(
  description: str = 'run the procedure',
  body: str = 'procedure body',
  parameters: Optional[dict[str, str]] = None,
) -> str:
  lines = ['---', f'description: {description}']
  if parameters is not None:
    lines.append(f'parameters: {json.dumps(parameters)}')
  lines.extend(['---', '', body])
  return '\n'.join(lines)


@pytest.fixture(autouse=True)
def _cast_secret_unavailable(monkeypatch):
  monkeypatch.setattr(spell_store.credentials, 'available', lambda name: False)


@pytest.fixture
def fake_packages(tmp_path, monkeypatch):
  def make(name: str, spells: Optional[dict[str, str]] = None) -> SpellPackage:
    return spell_package(monkeypatch, tmp_path, name, spells)

  return make


class _NoRun:
  """the `LiveRun` a bro assembled outside a run has: no trail, no tool position."""

  trail_id = None
  current_tool_step_id = None


def _servers(bro: BaseBro, *, hold: str = 'unattended') -> list[llm_mcp.MCPServer]:
  return bro.assemble(harness='bro', hold=hold, live_run=_NoRun())


def _persona_servers(bro: BaseBro) -> list[llm_mcp.MCPServer]:
  return bro.assemble(harness='claude', hold='unattended')


def _spell_server(bro: BaseBro, *, hold: str = 'unattended') -> llm_mcp.MCPServer:
  return next(server for server in _servers(bro, hold=hold) if server.namespace == NAMESPACE)


def _service_server(bro: BaseBro, *, harness: mcp.HarnessLike = 'bro') -> llm_mcp.MCPServer:
  # built on its own rather than picked out of a full assembly: these tests read
  # service tools only, and materializing a bro's declared servers would demand
  # the credentials they hold.
  return bro_module._build_service_server(bro, hold='unattended', harness=harness)


class TestSpellDeclaration:
  def test_declarations_merge_along_the_mro_and_derived_overrides_parent(self, fake_packages):
    parent_package = fake_packages(
      '_spell_parent',
      {'shared': _spell(body='parent'), 'parent-only': _spell(body='parent only')},
    )
    child_package = fake_packages(
      '_spell_child',
      {'shared': _spell(body='child'), 'child-only': _spell(body='child only')},
    )
    parent = parent_package.bro_class()
    child = child_package.bro_class(parent)

    spells = child().spell_paths
    assert set(spells) == {'shared', 'parent-only', 'child-only'}
    assert spells['shared'].read_text().endswith('child')

  def test_the_resolved_roster_is_read_only(self, fake_packages):
    package = fake_packages('_spell_read_only', {'do-work': _spell()})
    bro = package.bro_class()()
    with pytest.raises(TypeError):
      cast(dict[str, Path], bro.spell_paths)['stray'] = Path('stray.md')
    assert list(bro.spell_paths) == ['do-work']

  def test_only_declared_files_are_spells(self, fake_packages):
    package = fake_packages('_spell_declared', {'named': _spell(), 'stray': _spell()})
    assert list(package.bro_class(spells=('named.md',))().spell_paths) == ['named']

  def test_a_nested_path_names_the_spell_by_its_stem(self, fake_packages):
    package = fake_packages('_spell_nested', {'review/pr': _spell(body='nested')})
    assert package.bro_class()().get_spell_body('pr', harness='bro', hold='unattended') == 'nested'

  @pytest.mark.parametrize(
    ('entry', 'match'),
    [
      ('missing.md', 'names no file'),
      ('/absolute.md', 'inside'),
      ('../outside.md', 'inside'),
      ('known.txt', 'not a markdown file'),
    ],
  )
  def test_an_invalid_entry_fails_construction(self, fake_packages, entry, match):
    package = fake_packages(f'_spell_entry_{len(sys.modules)}', {'known': _spell()})
    with pytest.raises(ValueError, match=match):
      package.bro_class(spells=(entry,))()

  def test_one_declaration_cannot_repeat_a_spell_name(self, fake_packages):
    package = fake_packages('_spell_repeat', {'a/fix': _spell(), 'b/fix': _spell()})
    with pytest.raises(ValueError, match="repeats spell 'fix'"):
      package.bro_class()()

  def test_a_module_without_a_file_cannot_declare_spells(self, monkeypatch):
    monkeypatch.setitem(sys.modules, '_spell_fileless', types.ModuleType('_spell_fileless'))
    fileless = type(
      'Fileless',
      (BaseBro,),
      {
        '__module__': '_spell_fileless',
        'name': 'fileless',
        'description': 'test bro',
        'spells': ('x.md',),
      },
    )
    with pytest.raises(ValueError, match='no file'):
      fileless()


class TestSpellStore:
  def test_body_and_descriptions_strip_flat_frontmatter(self, fake_packages):
    package = fake_packages(
      '_spell_body',
      {'do-work': _spell('First sentence. Full detail follows.', '# Procedure\n\nwork')},
    )
    bro = package.bro_class()()

    assert bro.spell_descriptions() == [('do-work', 'First sentence. Full detail follows.')]
    assert bro.get_spell_body('do-work', harness='bro', hold='unattended') == '# Procedure\n\nwork'

  def test_unknown_spell_names_available_spells(self, fake_packages):
    package = fake_packages('_spell_unknown', {'known': _spell()})
    with pytest.raises(KeyError, match='available: known'):
      package.bro_class()().get_spell_body('missing', harness='bro', hold='unattended')

  def test_spell_body_renders_against_the_bro_features(self, fake_packages, monkeypatch):
    package = fake_packages(
      '_spell_features',
      {'gated': _spell(body='{{iff #features contains x}}on-branch{{else}}off-branch{{end}}')},
    )
    cls = package.bro_class()
    cls.features = {'x': creds.contains('xkey')}
    monkeypatch.setattr(spell_store.credentials, 'known_names', lambda: frozenset({'xkey'}))
    bro = cls()

    # the probe is live: one instance renders both states as availability moves
    monkeypatch.setattr(spell_store.credentials, 'available', lambda name: name == 'xkey')
    assert bro.get_spell_body('gated', harness='bro', hold='unattended') == 'on-branch'
    monkeypatch.setattr(spell_store.credentials, 'available', lambda name: False)
    assert bro.get_spell_body('gated', harness='bro', hold='unattended') == 'off-branch'

  @pytest.mark.asyncio
  async def test_spell_tool_renders_under_the_assembled_hold(self, fake_packages):
    package = fake_packages(
      '_spell_hold',
      {'held': _spell(body='{{iff #hold = unattended}}alone{{else}}watched{{end}}')},
    )
    bro = package.bro_class()()

    for hold, body in (('unattended', 'alone'), ('attended', 'watched')):
      tools = await _spell_server(bro, hold=hold).list_tools()
      assert await next(tool for tool in tools if tool.name == 'held').call({}) == body

  def test_checked_in_store_has_no_legacy_skill_directories(self):
    skill_directories = list((Path(spell_store.__file__).parent.parent / 'bros').glob('*/skills'))
    assert skill_directories == []


class TestSpellValidation:
  @pytest.mark.parametrize(
    ('name', 'content', 'match'),
    [
      ('bad.name', _spell(), 'ASCII letters'),
      ('real', '---\nname: wrong\ndescription: real\n---\nbody', 'disagrees'),
      ('real', '---\nname: real\n---\nbody', 'no description'),
      ('real', _spell(parameters={'bad.name': 'bad'}), 'parameter name'),
      ('real', _spell(parameters={'offset': 'bad'}), 'output paging'),
      ('real', _spell(parameters={'?': 'bad'}), 'parameter name'),
      ('real', _spell(parameters={'same': 'one', 'same?': 'two'}), 'duplicate parameter'),
      ('real', '---\ndescription: real\nparameters: []\n---\nbody', 'JSON object'),
      ('real', '---\ndescription: real\nparameters: {broken\n---\nbody', 'one-line JSON object'),
      ('real', '---\ndescription: real\nparameters: {"arg": 1}\n---\nbody', 'must be strings'),
    ],
  )
  def test_invalid_spells_fail_at_bro_load(self, fake_packages, name, content, match):
    package = fake_packages(f'_invalid_{len(sys.modules)}', {name: content})
    with pytest.raises(ValueError, match=match):
      package.bro_class()()

  @pytest.mark.parametrize('name', ['at', 'cast', 'skill', 'spell'])
  def test_service_and_namespace_names_are_valid_spell_stems(self, tmp_path, name):
    path = tmp_path / f'{name}.md'
    path.write_text(_spell())
    assert load_spell(name, path).name == name

  def test_required_and_optional_parameters_parse(self, tmp_path):
    path = tmp_path / 'do-work.md'
    path.write_text(_spell(parameters={'task': 'task ref', 'notes?': 'extra context'}))
    spell = load_spell('do-work', path)
    assert [(item.name, item.required) for item in spell.parameters] == [
      ('task', True),
      ('notes', False),
    ]


class TestSpellServer:
  @pytest.mark.asyncio
  async def test_mounts_on_both_harnesses(self, fake_packages):
    package = fake_packages('_spell_mount', {'do-work': _spell()})
    bro = package.bro_class()()

    assert NAMESPACE in {server.namespace for server in _servers(bro)}
    assert NAMESPACE in {server.namespace for server in _persona_servers(bro)}
    registry = ToolRegistry([_spell_server(bro)])
    assert {tool.name for tool in await registry.resolve()} == {'spell__do-work'}

  def test_empty_store_mounts_no_spell_server(self, fake_packages):
    package = fake_packages('_spell_empty')
    bro = package.bro_class()()
    assert NAMESPACE not in {server.namespace for server in _servers(bro)}
    assert NAMESPACE not in {server.namespace for server in _persona_servers(bro)}

  def test_declared_server_cannot_claim_reserved_namespace(self, fake_packages):
    package = fake_packages('_spell_reserved')
    server_spec = MCPServerSpec(
      namespace=NAMESPACE, build=lambda: InProcessMCPServer(NAMESPACE, [])
    )
    bro_class = type(
      'ReservedBro',
      (BaseBro,),
      {
        '__module__': package,
        'name': 'reserved',
        'description': 'test bro',
        'tools': [mcp.ToolLayer(server_specs=(server_spec,))],
      },
    )
    with pytest.raises(ValueError, match='reserved for bro framework tools'):
      _servers(bro_class())

  @pytest.mark.asyncio
  async def test_skill_loader_contract_and_prompt(self, fake_packages):
    package = fake_packages('_skill_loader_empty')
    bro = package.bro_class()()
    persona_tool_names = {
      tool.name for server in _persona_servers(bro) for tool in await server.list_tools()
    }
    skill = spell_store.build_skill_tool()

    assert not hasattr(bro, 'skills')
    assert not hasattr(bro, 'get_skill_body')
    assert not hasattr(bro, 'skill_descriptions')
    assert 'skill' not in persona_tool_names
    assert '## Skills' in bro.system_prompt_for(hold='unattended')
    assert skill.parameters['required'] == ['name']
    assert await skill.call({'name': 'third-party'}) == ''

    with pytest.raises(ValueError, match='exactly one'):
      await skill.call({})
    with pytest.raises(ValueError, match='non-empty'):
      await skill.call({'name': ''})

  @pytest.mark.asyncio
  async def test_schema_marks_required_and_optional_strings(self, fake_packages):
    package = fake_packages(
      '_spell_schema',
      {'do-work': _spell(parameters={'task': 'task ref', 'notes?': 'extra context'})},
    )
    tool = (await _spell_server(package.bro_class()()).list_tools())[0]

    assert tool.description == 'run the procedure'
    assert tool.parameters['required'] == ['task']
    assert tool.parameters['properties']['task'] == {'type': 'string', 'description': 'task ref'}
    assert tool.parameters['properties']['notes'] == {
      'type': 'string',
      'description': 'extra context',
    }
    assert tool.parameters['properties']['offset']['type'] == 'integer'
    assert tool.parameters['additionalProperties'] is False

  @pytest.mark.asyncio
  async def test_renders_body_and_appends_passed_arguments(self, fake_packages):
    body = '{{iff #harness = bro}}BRO{{else}}CLAUDE{{end}}'
    package = fake_packages(
      '_spell_render',
      {'do-work': _spell(body=body, parameters={'task': 'task ref', 'notes?': 'context'})},
    )
    bro = package.bro_class()()
    native_tool = (await _spell_server(bro).list_tools())[0]
    persona_server = next(
      server for server in _persona_servers(bro) if server.namespace == NAMESPACE
    )
    persona_tool = (await persona_server.list_tools())[0]

    assert await native_tool.call({'task': 'T-1'}) == 'BRO\n\n# Arguments\n\ntask: T-1'
    assert await persona_tool.call({'task': 'T-2', 'notes': 'urgent'}) == (
      'CLAUDE\n\n# Arguments\n\ntask: T-2\nnotes: urgent'
    )

  @pytest.mark.asyncio
  async def test_no_arguments_section_when_none_are_declared_or_passed(self, fake_packages):
    package = fake_packages('_spell_no_args', {'do-work': _spell(body='body')})
    tool = (await _spell_server(package.bro_class()()).list_tools())[0]
    assert await tool.call({}) == 'body'

  @pytest.mark.asyncio
  async def test_required_and_typed_argument_validation(self, fake_packages):
    package = fake_packages(
      '_spell_call_validation',
      {'do-work': _spell(parameters={'task': 'task ref', 'notes?': 'context'})},
    )
    tool = (await _spell_server(package.bro_class()()).list_tools())[0]

    with pytest.raises(ValueError, match='missing required'):
      await tool.call({})
    with pytest.raises(ValueError, match='must be a string'):
      await tool.call({'task': 1})
    with pytest.raises(ValueError, match='unknown arguments'):
      await tool.call({'task': 'T-1', 'extra': 'no'})
    with pytest.raises(ValueError, match='must be an integer'):
      await tool.call({'task': 'T-1', 'offset': True})

  @pytest.mark.asyncio
  async def test_pages_plain_output_with_generous_window(self, fake_packages):
    body = '\n'.join(f'line {index}' for index in range(1005))
    package = fake_packages('_spell_window', {'do-work': _spell(body=body)})
    tool = (await _spell_server(package.bro_class()()).list_tools())[0]

    result = await tool.call({'offset': 1000})
    assert isinstance(result, str)
    assert result.startswith('[...skipped before: 1,000 lines')
    assert 'line 999' not in result
    assert 'line 1000\nline 1001' in result
    assert 'line 1004' in result
    assert '\t' not in result


class TestCast:
  @staticmethod
  async def _tool(bro: BaseBro, *, harness: mcp.HarnessLike = 'bro'):
    tools = await _service_server(bro, harness=harness).list_tools()
    return next(tool for tool in tools if tool.name == 'cast')

  @pytest.mark.asyncio
  async def test_mounts_only_when_secret_resolves_and_keeps_direct_tools(
    self, fake_packages, monkeypatch
  ):
    package = fake_packages('_cast_mount', {'do-work': _spell()})
    bro_class = package.bro_class()

    unavailable_spells = {tool.name for tool in await _spell_server(bro_class()).list_tools()}
    unavailable_services = {tool.name for tool in await _service_server(bro_class()).list_tools()}
    assert unavailable_spells == {'do-work'}
    assert 'cast' not in unavailable_services

    monkeypatch.setattr(spell_store.credentials, 'available', lambda name: name == CAST_SECRET)
    available_bro = bro_class()
    native_spells = {tool.name for tool in await _spell_server(available_bro).list_tools()}
    native_services = {tool.name for tool in await _service_server(available_bro).list_tools()}
    persona_service = next(
      server for server in _persona_servers(available_bro) if server.namespace == 'bro'
    )
    persona_services = {tool.name for tool in await persona_service.list_tools()}
    assert native_spells == {'do-work'}
    assert 'cast' in native_services
    assert 'cast' in persona_services

  @pytest.mark.asyncio
  async def test_success_converts_argument_pairs_and_passes_roster_to_mu(
    self, fake_packages, monkeypatch
  ):
    import bro.llm.mu as mu_module

    package = fake_packages(
      '_cast_success',
      {
        'do-work': _spell(
          description='develop the named task',
          parameters={'task': 'task ref', 'notes?': 'extra context'},
        )
      },
    )
    monkeypatch.setattr(spell_store.credentials, 'available', lambda name: True)
    captured = {}

    async def fake_mu(prompt, result_class, *contents, model=None, reasoning_effort=None):
      captured['prompt'] = prompt
      captured['request'] = contents[0].json
      return result_class.model_validate(
        {
          'spell': 'spell::do-work',
          'arguments': [
            {'name': 'task', 'value': 'T-1'},
            {'name': 'notes', 'value': 'keep the merge'},
          ],
          'error': None,
        }
      )

    monkeypatch.setattr(mu_module, 'mu', types.SimpleNamespace(aio=fake_mu))
    result = await (await self._tool(package.bro_class()())).call({'command': 'work on T-1'})

    assert result == (
      'spell: spell::do-work\n\nprocedure body\n\n# Arguments\n\ntask: T-1\nnotes: keep the merge'
    )
    assert 'unambiguously applies' in captured['prompt']
    assert captured['request']['command'] == 'work on T-1'
    assert captured['request']['spells'] == [
      {
        'spell': 'spell::do-work',
        'description': 'develop the named task',
        'parameters': {
          'type': 'object',
          'properties': {
            'task': {'type': 'string', 'description': 'task ref'},
            'notes': {'type': 'string', 'description': 'extra context'},
          },
          'required': ['task'],
          'additionalProperties': False,
        },
      }
    ]

  @pytest.mark.asyncio
  async def test_renders_instructions_for_the_serving_surface(self, fake_packages, monkeypatch):
    import bro.llm.mu as mu_module

    package = fake_packages(
      '_cast_render',
      {'do-work': _spell(body='{{iff #harness = bro}}NATIVE{{else}}CLAUDE{{end}}')},
    )
    monkeypatch.setattr(spell_store.credentials, 'available', lambda name: True)

    async def fake_mu(prompt, result_class, *contents, model=None, reasoning_effort=None):
      return result_class.model_validate(
        {'spell': 'spell::do-work', 'arguments': [], 'error': None}
      )

    monkeypatch.setattr(mu_module, 'mu', types.SimpleNamespace(aio=fake_mu))
    bro = package.bro_class()()

    native_result = await (await self._tool(bro)).call({'command': 'do the work'})
    claude_result = await (await self._tool(bro, harness='claude')).call({'command': 'do the work'})
    assert native_result == 'spell: spell::do-work\n\nNATIVE'
    assert claude_result == 'spell: spell::do-work\n\nCLAUDE'

  @pytest.mark.asyncio
  async def test_success_with_null_arguments_is_a_call_without_arguments(
    self, fake_packages, monkeypatch
  ):
    import bro.llm.mu as mu_module

    package = fake_packages('_cast_null_arguments', {'do-work': _spell()})
    monkeypatch.setattr(spell_store.credentials, 'available', lambda name: True)

    async def fake_mu(prompt, result_class, *contents, model=None, reasoning_effort=None):
      return result_class.model_validate(
        {'spell': 'spell::do-work', 'arguments': None, 'error': None}
      )

    monkeypatch.setattr(mu_module, 'mu', types.SimpleNamespace(aio=fake_mu))
    result = await (await self._tool(package.bro_class()())).call({'command': 'do the work'})

    assert result == 'spell: spell::do-work\n\nprocedure body'

  @pytest.mark.asyncio
  async def test_expected_error_passes_through(self, fake_packages, monkeypatch):
    import bro.llm.mu as mu_module

    package = fake_packages('_cast_error', {'do-work': _spell()})
    monkeypatch.setattr(spell_store.credentials, 'available', lambda name: True)

    async def fake_mu(prompt, result_class, *contents, model=None, reasoning_effort=None):
      return result_class.model_validate(
        {'spell': None, 'arguments': None, 'error': 'the command matches no spell'}
      )

    monkeypatch.setattr(mu_module, 'mu', types.SimpleNamespace(aio=fake_mu))
    result = await (await self._tool(package.bro_class()())).call({'command': 'unknown action'})
    assert result == {'error': 'the command matches no spell'}

  @pytest.mark.asyncio
  @pytest.mark.parametrize(
    ('interpretation', 'match'),
    [
      (
        {'spell': 'spell::missing', 'arguments': [{'name': 'task', 'value': 'T-1'}]},
        'unknown spell',
      ),
      (
        {
          'spell': 'spell::do-work',
          'arguments': [
            {'name': 'task', 'value': 'T-1'},
            {'name': 'task', 'value': 'T-2'},
          ],
        },
        'duplicate argument',
      ),
      (
        {
          'spell': 'spell::do-work',
          'arguments': [
            {'name': 'task', 'value': 'T-1'},
            {'name': 'extra', 'value': 'no'},
          ],
        },
        'unknown argument',
      ),
      ({'spell': 'spell::do-work', 'arguments': []}, 'omitted required'),
      ({'spell': 'spell::do-work', 'arguments': None}, 'omitted required'),
    ],
  )
  async def test_invalid_model_selection_fails_validation(
    self, fake_packages, monkeypatch, interpretation, match
  ):
    import bro.llm.mu as mu_module

    package = fake_packages(
      f'_cast_invalid_{len(sys.modules)}',
      {'do-work': _spell(parameters={'task': 'task ref'})},
    )
    monkeypatch.setattr(spell_store.credentials, 'available', lambda name: True)

    async def fake_mu(prompt, result_class, *contents, model=None, reasoning_effort=None):
      return result_class.model_validate({**interpretation, 'error': None})

    monkeypatch.setattr(mu_module, 'mu', types.SimpleNamespace(aio=fake_mu))
    with pytest.raises(ValueError, match=match):
      await (await self._tool(package.bro_class()())).call({'command': 'do something'})

  @pytest.mark.asyncio
  async def test_rejects_empty_expected_error(self, fake_packages, monkeypatch):
    import bro.llm.mu as mu_module

    package = fake_packages('_cast_empty_error', {'do-work': _spell()})
    monkeypatch.setattr(spell_store.credentials, 'available', lambda name: True)

    async def fake_mu(prompt, result_class, *contents, model=None, reasoning_effort=None):
      return result_class.model_validate({'spell': None, 'arguments': None, 'error': '   '})

    monkeypatch.setattr(mu_module, 'mu', types.SimpleNamespace(aio=fake_mu))
    with pytest.raises(ValueError, match='empty error'):
      await (await self._tool(package.bro_class()())).call({'command': 'do something'})


class TestSpellsPrompt:
  def test_direct_contract_is_present_without_cast(self, fake_packages):
    description = 'Full first sentence. Detail that belongs only on the tool.'
    package = fake_packages('_spell_prompt_direct', {'do-work': _spell(description)})
    bro = package.bro_class()()

    spells_section = (
      bro.system_prompt_for(hold='unattended').split('## Spells', 1)[1].split('## Skills', 1)[0]
    )
    assert '`/<name>`' not in spells_section
    assert '`bro::cast`' not in spells_section
    assert "call the named spell's own tool" in spells_section
    assert description not in bro.system_prompt_for(hold='unattended')

  def test_dispatch_contract_is_present_when_secret_resolves(self, fake_packages, monkeypatch):
    package = fake_packages('_spell_prompt_dispatch', {'do-work': _spell()})
    monkeypatch.setattr(spell_store.credentials, 'available', lambda name: name == CAST_SECRET)
    bro = package.bro_class()()

    assert '## Spells' in bro.system_prompt_for(hold='unattended')
    assert '`bro::cast`' in bro.system_prompt_for(hold='unattended')
    assert 'follow the returned instructions' in bro.system_prompt_for(hold='unattended')

  def test_section_is_absent_without_spells(self, fake_packages):
    package = fake_packages('_spell_prompt_empty')
    assert '## Spells' not in package.bro_class()().system_prompt_for(hold='unattended')


class TestSpellOptionalSecret:
  def test_nonempty_roster_declares_cast_secret_for_each_harness(self, fake_packages):
    package = fake_packages('_spell_optional_secret', {'do-work': _spell()})
    bro = package.bro_class()()
    assert bro.optional_secrets(harness='bro') == (CAST_SECRET,)
    assert bro.optional_secrets(harness='claude') == (CAST_SECRET,)

  def test_empty_roster_does_not_declare_cast_secret(self, fake_packages):
    package = fake_packages('_spell_no_optional_secret')
    assert CAST_SECRET not in package.bro_class()().optional_secrets()


class TestSpellToolNames:
  def test_tool_name_prompt_uses_the_ordinary_namespace_rule(self):
    text = get_prompt('tool_names.md')
    native = mcp.render_text(text, harness=get_harness('bro'))
    claude = mcp.render_text(text, harness=get_harness('claude'))
    assert 'replace `::` with `__` and call that wire name directly' in native
    assert 'prepend `mcp__`' in claude
    assert 'spells `at` on the wire' not in native


# room left for each declared parameter's value in a call's `# Arguments`
# section — a URL, a branch, a short phrase; a spell whose call cannot carry
# that much fails rather than truncating at cast time
_ARGUMENT_ROOM = 200


@pytest.mark.parametrize('name', sorted(declared_specs()))
def test_every_spell_call_fits_one_window(name, monkeypatch):
  # every credential resolves, so `#creds`-gated passages all render and the
  # widest rendering is the one measured
  monkeypatch.setattr(spell_store.credentials, 'available', lambda credential: True)
  bro = create_bro(name)
  features = bro.vocabulary()['features']
  assert isinstance(features, SetVariable) and features.universe is not None
  every_persona = tuple(declared_specs())
  for spell_name, path in bro.spell_paths.items():
    spell = load_spell(spell_name, path)
    arguments = {parameter.name: 'x' * _ARGUMENT_ROOM for parameter in spell.parameters}
    for harness_name in installed_harness_names():
      for hold in mcp.HOLDS:
        for granted in (every_persona, ()):
          for enabled in (features.universe, frozenset()):
            body = mcp.render_text(
              spell.body,
              harness=get_harness(harness_name),
              creds=spell_store.credentials.known_names(),
              may_summon=granted,
              hold=hold,
              extra={'features': SetVariable(enabled, universe=features.universe)},
            ).strip()
            text = spell_store.call_text(spell, body, arguments)
            assert window(text, limit=spell_store.WINDOW_LIMIT) == text, (
              f'spell::{spell_name} of {name} overflows one window on {harness_name} '
              f'at {hold} ({len(text):,} characters)'
            )
