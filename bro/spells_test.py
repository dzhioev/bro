import json
import sys
import types
from pathlib import Path
from typing import ClassVar, Optional, get_args

import pytest

import bro.llm.mcp as llm_mcp
from bro import bro as bro_module, spells as spell_store
from bro.base.condition import SetVariable
from bro.bro import BaseBro
from bro.llm.mcp import InProcessMCPServer, MCPServerSpec, ToolRegistry, creds
from bro.prompts import get_prompt
from bro.spells import CAST_SECRET, NAMESPACE, load_spell
from bros.dev import Dev


class _TrackerDev(Dev):
  name = 'tracker-dev'
  features: ClassVar = {'brog': True}


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
def fake_packages(tmp_path):
  added: list[str] = []

  def make(name: str, spells: Optional[dict[str, str]] = None) -> str:
    package_dir = tmp_path / name
    package_dir.mkdir()
    init_path = package_dir / '__init__.py'
    init_path.write_text('')
    if spells is not None:
      spells_dir = package_dir / 'spells'
      spells_dir.mkdir()
      for spell_name, content in spells.items():
        (spells_dir / f'{spell_name}.md').write_text(content)
    module = types.ModuleType(name)
    module.__file__ = str(init_path)
    sys.modules[name] = module
    added.append(name)
    return name

  yield make

  for name in added:
    sys.modules.pop(name, None)


def _bro_class(package: str, parent: type[BaseBro] = BaseBro) -> type[BaseBro]:
  return type(
    f'BroFor{package}',
    (parent,),
    {'__module__': package, 'name': package.removeprefix('_'), 'description': 'test bro'},
  )


def _servers(bro: BaseBro, wire: llm_mcp.Wire) -> list[llm_mcp.MCPServer]:
  return bro._mcp_servers_for(hold='unattended') if wire == 'bare' else bro.claude_bro_mcp_servers()


def _spell_server(bro: BaseBro, *, wire: llm_mcp.Wire = 'bare') -> llm_mcp.MCPServer:
  return next(server for server in _servers(bro, wire) if server.namespace == NAMESPACE)


def _service_server(bro: BaseBro, *, wire: llm_mcp.Wire = 'bare') -> llm_mcp.MCPServer:
  return next(server for server in _servers(bro, wire) if server.namespace == 'bro')


class TestSpellStore:
  def test_collects_spells_and_derived_overrides_parent(self, fake_packages):
    parent_package = fake_packages(
      '_spell_parent',
      {'shared': _spell(body='parent'), 'parent-only': _spell(body='parent only')},
    )
    child_package = fake_packages(
      '_spell_child',
      {'shared': _spell(body='child'), 'child-only': _spell(body='child only')},
    )
    parent = _bro_class(parent_package)
    child = _bro_class(child_package, parent)

    spells = child().spells
    assert set(spells) == {'shared', 'parent-only', 'child-only'}
    assert spells['shared'].read_text().endswith('child')

  def test_body_and_descriptions_strip_flat_frontmatter(self, fake_packages):
    package = fake_packages(
      '_spell_body',
      {'do-work': _spell('First sentence. Full detail follows.', '# Procedure\n\nwork')},
    )
    bro = _bro_class(package)()

    assert bro.spell_descriptions() == [('do-work', 'First sentence. Full detail follows.')]
    assert bro.get_spell_body('do-work', harness='bro', wire='bare') == '# Procedure\n\nwork'

  def test_unknown_spell_names_available_spells(self, fake_packages):
    package = fake_packages('_spell_unknown', {'known': _spell()})
    with pytest.raises(KeyError, match='available: known'):
      _bro_class(package)().get_spell_body('missing', harness='bro', wire='bare')

  def test_checked_in_spells_render_for_every_surface(self):
    spell_files = sorted((Path(spell_store.__file__).parent.parent / 'bros').glob('*/spells/*.md'))
    assert len(spell_files) > 0
    # the closed universe of feature names checked-in spells may condition on;
    # grow it when a bro declares a new feature
    feature_names = frozenset({'brog'})
    for path in spell_files:
      spell = load_spell(path.stem, path)
      for harness in get_args(llm_mcp.Harness):
        for wire in get_args(llm_mcp.Wire):
          for enabled in (True, False):
            llm_mcp.render_text(
              spell.body,
              harness=harness,
              wire=wire,
              creds=spell_store.credentials.known_names(),
              extra={'features': SetVariable(lambda name, on=enabled: on, universe=feature_names)},
            )

  def test_spell_body_renders_against_the_bro_features(self, fake_packages, monkeypatch):
    package = fake_packages(
      '_spell_features',
      {'gated': _spell(body='{{iff #features contains x}}on-branch{{else}}off-branch{{end}}')},
    )
    cls = _bro_class(package)
    cls.features = {'x': creds.contains('xkey')}
    bro = cls()

    # the probe is live: one instance renders both states as availability moves
    monkeypatch.setattr(spell_store.credentials, 'available', lambda name: name == 'xkey')
    assert bro.get_spell_body('gated', harness='bro', wire='bare') == 'on-branch'
    monkeypatch.setattr(spell_store.credentials, 'available', lambda name: False)
    assert bro.get_spell_body('gated', harness='bro', wire='bare') == 'off-branch'

  def test_checked_in_store_has_no_legacy_skill_directories(self):
    skill_directories = list((Path(spell_store.__file__).parent.parent / 'bros').glob('*/skills'))
    assert skill_directories == []

  def test_tracker_dev_inherits_shared_and_dev_spells(self):
    bro = _TrackerDev()
    assert set(bro.spells) == {'ask', 'audit', 'fix', 'land', 'reflect', 'run-pr', 'wire'}
    assert '## Spells' in bro.system_prompt
    assert '## Available skills' not in bro.system_prompt

  def test_fix_declares_optional_task_and_new_arguments_for_bro(self):
    bro = _TrackerDev()
    spell = load_spell('fix', bro.spells['fix'])
    assert [(parameter.name, parameter.required) for parameter in spell.parameters] == [
      ('task', False),
      ('new', False),
    ]

    bro_body = bro.get_spell_body('fix', harness='bro', wire='bare')
    claude_body = bro.get_spell_body('fix', harness='claude', wire='mcp')
    for body in (bro_body, claude_body):
      assert '`task` — operate on the existing task' in body
      assert '`new` — create a task from this seed' in body
      assert '/fix' not in body

  def test_run_pr_declares_optional_base_and_reentry_arguments(self):
    bro = _TrackerDev()
    spell = load_spell('run-pr', bro.spells['run-pr'])
    assert [(parameter.name, parameter.required) for parameter in spell.parameters] == [
      ('base', False),
      ('pr', False),
    ]
    bodies = (
      bro.get_spell_body('run-pr', harness='bro', wire='bare'),
      bro.get_spell_body('run-pr', harness='claude', wire='mcp'),
    )
    for body in bodies:
      assert '`base` — base the PR' in body
      assert '`pr` — re-entry mode' in body
      assert '/run-pr' not in body

  @pytest.mark.asyncio
  async def test_skill_loader_is_a_framework_service_tool(self):
    bro = _TrackerDev()
    assert not hasattr(bro, 'skills')
    assert not hasattr(bro, 'get_skill_body')
    assert not hasattr(bro, 'skill_descriptions')
    assert 'skill' in bro_module._SERVICE_TOOL_NAMES
    assert 'skill' in {tool.name for tool in await bro._service_server.list_tools()}


class TestSpellValidation:
  @pytest.mark.parametrize(
    ('name', 'content', 'match'),
    [
      ('bad.name', _spell(), 'ASCII letters'),
      ('real', '---\nname: wrong\n---\nbody', 'disagrees'),
      ('real', _spell(parameters={'bad.name': 'bad'}), 'parameter name'),
      ('real', _spell(parameters={'offset': 'bad'}), 'output paging'),
      ('real', _spell(parameters={'?': 'bad'}), 'parameter name'),
      ('real', _spell(parameters={'same': 'one', 'same?': 'two'}), 'duplicate parameter'),
      ('real', '---\nparameters: []\n---\nbody', 'JSON object'),
      ('real', '---\nparameters: {broken\n---\nbody', 'one-line JSON object'),
      ('real', '---\nparameters: {"arg": 1}\n---\nbody', 'must be strings'),
    ],
  )
  def test_invalid_spells_fail_at_bro_load(self, fake_packages, name, content, match):
    package = fake_packages(f'_invalid_{len(sys.modules)}', {name: content})
    with pytest.raises(ValueError, match=match):
      _bro_class(package)()

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
  async def test_mounts_on_native_and_both_claude_surfaces(self, fake_packages):
    package = fake_packages('_spell_mount', {'do-work': _spell()})
    bro = _bro_class(package)()

    assert NAMESPACE in {server.namespace for server in bro._mcp_servers_for(hold='unattended')}
    assert NAMESPACE in {server.namespace for server in bro.claude_bro_mcp_servers()}
    assert NAMESPACE in {server.namespace for server in bro.claude_persona_mcp_servers()}
    registry = ToolRegistry([_spell_server(bro)])
    assert {tool.name for tool in await registry.resolve()} == {'spell__do-work'}

  def test_empty_store_mounts_no_spell_server(self, fake_packages):
    package = fake_packages('_spell_empty')
    bro = _bro_class(package)()
    assert NAMESPACE not in {server.namespace for server in bro._mcp_servers_for(hold='unattended')}
    assert NAMESPACE not in {server.namespace for server in bro.claude_bro_mcp_servers()}
    assert NAMESPACE not in {server.namespace for server in bro.claude_persona_mcp_servers()}

  def test_declared_server_cannot_claim_reserved_namespace(self, fake_packages):
    package = fake_packages('_spell_reserved')
    server_spec = MCPServerSpec(build=lambda: InProcessMCPServer(NAMESPACE, []))
    bro_class = type(
      'ReservedBro',
      (BaseBro,),
      {
        '__module__': package,
        'name': 'reserved',
        'description': 'test bro',
        'tools': [llm_mcp.ToolLayer(server_specs=(server_spec,))],
      },
    )
    with pytest.raises(ValueError, match='reserved for bro framework tools'):
      bro_class()._mcp_servers_for(hold='unattended')

  @pytest.mark.asyncio
  async def test_skill_loader_is_empty_and_excluded_from_claude_persona(self, fake_packages):
    package = fake_packages('_skill_loader_empty')
    bro = _bro_class(package)()
    native_tools = await _service_server(bro).list_tools()
    claude_bro_tools = await _service_server(bro, wire='mcp').list_tools()
    persona_tool_names = {
      tool.name for server in bro.claude_persona_mcp_servers() for tool in await server.list_tools()
    }

    native_skill = next(tool for tool in native_tools if tool.name == 'skill')
    assert 'skill' in {tool.name for tool in claude_bro_tools}
    assert 'skill' not in persona_tool_names
    assert '## Skills' in bro.system_prompt
    assert '## Skills' in bro.claude_system_prompt
    assert native_skill.parameters['required'] == ['name']
    assert await native_skill.call({'name': 'third-party'}) == ''

    with pytest.raises(ValueError, match='exactly one'):
      await native_skill.call({})
    with pytest.raises(ValueError, match='non-empty'):
      await native_skill.call({'name': ''})

  @pytest.mark.asyncio
  async def test_schema_marks_required_and_optional_strings(self, fake_packages):
    package = fake_packages(
      '_spell_schema',
      {'do-work': _spell(parameters={'task': 'task ref', 'notes?': 'extra context'})},
    )
    tool = (await _spell_server(_bro_class(package)()).list_tools())[0]

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
    body = (
      '{{iff #harness = bro}}BRO{{else}}CLAUDE{{end}} {{iff #wire = bare}}BARE{{else}}MCP{{end}}'
    )
    package = fake_packages(
      '_spell_render',
      {'do-work': _spell(body=body, parameters={'task': 'task ref', 'notes?': 'context'})},
    )
    bro = _bro_class(package)()
    bare_tool = (await _spell_server(bro).list_tools())[0]
    mcp_tool = (await _spell_server(bro, wire='mcp').list_tools())[0]
    persona_server = next(
      server for server in bro.claude_persona_mcp_servers() if server.namespace == NAMESPACE
    )
    persona_tool = (await persona_server.list_tools())[0]

    assert await bare_tool.call({'task': 'T-1'}) == 'BRO BARE\n\n# Arguments\n\ntask: T-1'
    assert await mcp_tool.call({'task': 'T-2', 'notes': 'urgent'}) == (
      'BRO MCP\n\n# Arguments\n\ntask: T-2\nnotes: urgent'
    )
    assert await persona_tool.call({'task': 'T-3'}) == ('CLAUDE MCP\n\n# Arguments\n\ntask: T-3')

  @pytest.mark.asyncio
  async def test_no_arguments_section_when_none_are_declared_or_passed(self, fake_packages):
    package = fake_packages('_spell_no_args', {'do-work': _spell(body='body')})
    tool = (await _spell_server(_bro_class(package)()).list_tools())[0]
    assert await tool.call({}) == 'body'

  @pytest.mark.asyncio
  async def test_required_and_typed_argument_validation(self, fake_packages):
    package = fake_packages(
      '_spell_call_validation',
      {'do-work': _spell(parameters={'task': 'task ref', 'notes?': 'context'})},
    )
    tool = (await _spell_server(_bro_class(package)()).list_tools())[0]

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
    tool = (await _spell_server(_bro_class(package)()).list_tools())[0]

    result = await tool.call({'offset': 1000})
    assert isinstance(result, str)
    assert result.startswith('[...skipped before: 1,000 lines')
    assert 'line 999' not in result
    assert 'line 1000\nline 1001' in result
    assert 'line 1004' in result
    assert '\t' not in result


class TestCast:
  @staticmethod
  async def _tool(bro: BaseBro, *, wire: llm_mcp.Wire = 'bare'):
    tools = await _service_server(bro, wire=wire).list_tools()
    return next(tool for tool in tools if tool.name == 'cast')

  @pytest.mark.asyncio
  async def test_mounts_only_when_secret_resolves_and_keeps_direct_tools(
    self, fake_packages, monkeypatch
  ):
    package = fake_packages('_cast_mount', {'do-work': _spell()})
    bro_class = _bro_class(package)

    unavailable_spells = {tool.name for tool in await _spell_server(bro_class()).list_tools()}
    unavailable_services = {tool.name for tool in await _service_server(bro_class()).list_tools()}
    assert unavailable_spells == {'do-work'}
    assert 'cast' not in unavailable_services

    monkeypatch.setattr(spell_store.credentials, 'available', lambda name: name == CAST_SECRET)
    available_bro = bro_class()
    native_spells = {tool.name for tool in await _spell_server(available_bro).list_tools()}
    native_services = {tool.name for tool in await _service_server(available_bro).list_tools()}
    claude_services = {
      tool.name for tool in await _service_server(available_bro, wire='mcp').list_tools()
    }
    persona_service = next(
      server for server in available_bro.claude_persona_mcp_servers() if server.namespace == 'bro'
    )
    persona_services = {tool.name for tool in await persona_service.list_tools()}
    assert native_spells == {'do-work'}
    assert 'cast' in native_services
    assert 'cast' in claude_services
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
      captured['model'] = model
      captured['reasoning_effort'] = reasoning_effort
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
    result = await (await self._tool(_bro_class(package)())).call({'command': 'work on T-1'})

    assert result == (
      'spell: spell::do-work\n\nprocedure body\n\n# Arguments\n\ntask: T-1\nnotes: keep the merge'
    )
    assert 'unambiguously applies' in captured['prompt']
    assert captured['model'] == 'gpt-5.6-luna'
    assert captured['reasoning_effort'] == 'low'
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
      {'do-work': _spell(body='{{iff #wire = bare}}BARE{{else}}MCP{{end}}')},
    )
    monkeypatch.setattr(spell_store.credentials, 'available', lambda name: True)

    async def fake_mu(prompt, result_class, *contents, model=None, reasoning_effort=None):
      return result_class.model_validate(
        {'spell': 'spell::do-work', 'arguments': [], 'error': None}
      )

    monkeypatch.setattr(mu_module, 'mu', types.SimpleNamespace(aio=fake_mu))
    bro = _bro_class(package)()

    bare_result = await (await self._tool(bro)).call({'command': 'do the work'})
    mcp_result = await (await self._tool(bro, wire='mcp')).call({'command': 'do the work'})
    assert bare_result == 'spell: spell::do-work\n\nBARE'
    assert mcp_result == 'spell: spell::do-work\n\nMCP'

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
    result = await (await self._tool(_bro_class(package)())).call({'command': 'unknown action'})
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
      await (await self._tool(_bro_class(package)())).call({'command': 'do something'})

  @pytest.mark.asyncio
  async def test_rejects_empty_expected_error(self, fake_packages, monkeypatch):
    import bro.llm.mu as mu_module

    package = fake_packages('_cast_empty_error', {'do-work': _spell()})
    monkeypatch.setattr(spell_store.credentials, 'available', lambda name: True)

    async def fake_mu(prompt, result_class, *contents, model=None, reasoning_effort=None):
      return result_class.model_validate({'spell': None, 'arguments': None, 'error': '   '})

    monkeypatch.setattr(mu_module, 'mu', types.SimpleNamespace(aio=fake_mu))
    with pytest.raises(ValueError, match='empty error'):
      await (await self._tool(_bro_class(package)())).call({'command': 'do something'})


class TestSpellsPrompt:
  def test_direct_contract_is_present_without_cast(self, fake_packages):
    description = 'Full first sentence. Detail that belongs only on the tool.'
    package = fake_packages('_spell_prompt_direct', {'do-work': _spell(description)})
    bro = _bro_class(package)()

    spells_section = bro.system_prompt.split('## Spells', 1)[1].split('## Skills', 1)[0]
    assert 'canonical `spell::` tools' in spells_section
    assert '`/<name>`' not in spells_section
    assert '`bro::cast`' not in spells_section
    assert '[[…]]' in spells_section
    assert "call the named spell's own tool" in spells_section
    assert description not in bro.system_prompt

  def test_dispatch_contract_is_present_when_secret_resolves(self, fake_packages, monkeypatch):
    package = fake_packages('_spell_prompt_dispatch', {'do-work': _spell()})
    monkeypatch.setattr(spell_store.credentials, 'available', lambda name: name == CAST_SECRET)
    bro = _bro_class(package)()

    for prompt in (bro.system_prompt, bro.claude_system_prompt):
      assert '## Spells' in prompt
      assert '[[…]]' in prompt
      assert '`bro::cast`' in prompt
      assert 'follow the returned instructions' in prompt

  def test_section_is_absent_without_spells(self, fake_packages):
    package = fake_packages('_spell_prompt_empty')
    assert '## Spells' not in _bro_class(package)().system_prompt


class TestSpellOptionalSecret:
  def test_nonempty_roster_declares_cast_secret_for_each_harness(self, fake_packages):
    package = fake_packages('_spell_optional_secret', {'do-work': _spell()})
    bro = _bro_class(package)()
    assert bro.optional_secrets(harness='bro') == (CAST_SECRET,)
    assert bro.optional_secrets(harness='claude') == (CAST_SECRET,)

  def test_empty_roster_does_not_declare_cast_secret(self, fake_packages):
    package = fake_packages('_spell_no_optional_secret')
    assert CAST_SECRET not in _bro_class(package)().optional_secrets()


class TestSpellToolNames:
  def test_tool_name_prompt_uses_the_ordinary_namespace_rule(self):
    text = get_prompt('tool_names.md')
    bare = llm_mcp.render_text(text, wire='bare')
    mcp = llm_mcp.render_text(text, wire='mcp')
    assert 'replace `::` with `__` and call that wire name directly' in bare
    assert 'prepend `mcp__`' in mcp
    assert 'spells `at` on the wire' not in bare
    assert 'No canonical namespace may be named `spell`' in bare
