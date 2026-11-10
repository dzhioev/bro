import json
import sys
import types
from itertools import product
from pathlib import Path
from typing import Any, Optional, cast

import pytest

import bro.llm.mcp as llm_mcp
import bro.mcp as mcp
from bro import bro as bro_module, results, spells as spell_store
from bro.base.condition import SetVariable, when
from bro.base.template import TemplateError
from bro.base.text_window import BYTE_LIMIT
from bro.bro import BaseBro, feature
from bro.harness import Harness, get_harness, installed_harness_names
from bro.llm.mcp import InProcessMCPServer, ToolRegistry
from bro.mcp import MCPServerSpec, creds
from bro.monitor import SESSION_DIR_ENV
from bro.prompts import get_prompt
from bro.registry import create_bro, declared_specs
from bro.spells import CAST_SECRET, NAMESPACE, SpellFile, load_spell
from bro.spells_test_helper import SpellPackage, spell_package


def _spell(
  description: str = 'run the procedure',
  body: str = 'procedure body',
  parameters: Optional[dict[str, Any]] = None,
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


@pytest.fixture(autouse=True)
def session_state(tmp_path, monkeypatch):
  # a spell call's result is kept in the session's store
  monkeypatch.setenv(SESSION_DIR_ENV, str(tmp_path / 'session'))


class _NoRun:
  """the `LiveRun` a bro assembled outside a run has: no trail, no tool position,
  and the session's result store."""

  resolved_recipe = None
  trail_id = None
  current_tool_step_id = None

  @property
  def result_store(self) -> results.Store:
    return results.session_store()


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
  return bro_module._build_service_server(
    bro, hold='unattended', harness=harness, result_store=results.session_store
  )


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
    assert list(package.bro_class(spells=(package.files['named'],))().spell_paths) == ['named']

  def test_a_nested_path_names_the_spell_by_its_stem(self, fake_packages):
    package = fake_packages('_spell_nested', {'review/pr': _spell(body='nested')})
    assert package.bro_class()().get_spell_body('pr', harness='bro', hold='unattended') == 'nested'

  def test_a_missing_or_non_markdown_file_cannot_be_a_spell_file(self, tmp_path):
    (tmp_path / 'known.txt').write_text('text')
    with pytest.raises(ValueError, match='does not exist'):
      SpellFile(tmp_path / 'missing.md')
    with pytest.raises(ValueError, match='not a markdown file'):
      SpellFile(tmp_path / 'known.txt')

  def test_a_declaration_entry_must_be_a_spell_file(self, fake_packages):
    package = fake_packages('_spell_entry', {'known': _spell()})
    with pytest.raises(TypeError, match='not a SpellFile'):
      package.bro_class(spells=('known.md',))()

  def test_a_gated_spell_is_offered_only_where_its_condition_holds(self, fake_packages):
    package = fake_packages('_spell_gate', {'always': _spell(), 'gated': _spell()})
    declaration = (package.files['always'], when(feature('x'), package.files['gated']))

    on = package.bro_class(spells=declaration, features={'x': True})()
    off = package.bro_class(spells=declaration, features={'x': False})()

    assert list(on.spell_paths) == ['always', 'gated']
    assert list(off.spell_paths) == ['always']

  def test_an_offered_spell_needs_the_spells_it_requires(self, fake_packages, tmp_path):
    package = fake_packages('_spell_requires', {'needs': _spell(), 'needed': _spell()})
    needed = package.files['needed']
    needs = SpellFile(package.files['needs'].path, requires=('needed',))

    with pytest.raises(ValueError, match="spell 'needs' requires spell 'needed'"):
      package.bro_class(spells=(needs,))()
    assert list(package.bro_class(spells=(needs, needed))().spell_paths) == ['needs', 'needed']

  def test_a_gated_requirement_binds_only_where_it_resolves(self, fake_packages):
    package = fake_packages('_spell_gated_requires', {'needs': _spell(), 'needed': _spell()})
    needs = SpellFile(package.files['needs'].path, requires=(when(feature('x'), 'needed'),))

    assert list(package.bro_class(spells=(needs,), features={'x': False})().spell_paths) == [
      'needs'
    ]
    with pytest.raises(ValueError, match='requires spell'):
      package.bro_class(spells=(needs,), features={'x': True})()

  def test_spells_may_require_each_other(self, fake_packages):
    package = fake_packages('_spell_cycle', {'one': _spell(), 'two': _spell()})
    one = SpellFile(package.files['one'].path, requires=('two',))
    two = SpellFile(package.files['two'].path, requires=('one',))

    assert list(package.bro_class(spells=(one, two))().spell_paths) == ['one', 'two']
    with pytest.raises(ValueError, match="requires spell 'two'"):
      package.bro_class(spells=(one,))()

  def test_a_derived_declaration_satisfies_a_requirement_by_name(self, fake_packages):
    parent_package = fake_packages('_spell_requires_parent', {'needed': _spell(body='parent')})
    child_package = fake_packages('_spell_requires_child', {'needs': _spell(), 'needed': _spell()})
    needs = SpellFile(child_package.files['needs'].path, requires=('needed',))
    child = child_package.bro_class(
      parent_package.bro_class(), spells=(needs, child_package.files['needed'])
    )
    assert set(child().spell_paths) == {'needs', 'needed'}

  def test_one_declaration_cannot_repeat_a_spell_name(self, fake_packages):
    package = fake_packages('_spell_repeat', {'a/fix': _spell(), 'b/fix': _spell()})
    with pytest.raises(ValueError, match="repeats spell 'fix'"):
      package.bro_class()()


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


class TestChoiceParameters:
  @pytest.mark.parametrize(
    ('declaration', 'match'),
    [
      ({'description': 'mode', 'choices': []}, 'nonempty literals'),
      ({'description': 'mode', 'choices': ['']}, 'nonempty literals'),
      ({'description': 'mode', 'choices': ['one', 'one']}, 'repeated choice'),
      *[
        ({'description': 'mode', 'choices': [value]}, 'nonempty literals')
        for value in ('two words', 'a/b', 'a:b', 'é', '#one')
      ],
      ({'description': 'mode', 'choices': [1]}, 'valid string'),
      ({'description': 'mode', 'choices': 'one'}, 'valid list'),
      ({'description': 'mode'}, 'Field required'),
      ({'choices': ['one']}, 'Field required'),
      ({'description': 1, 'choices': ['one']}, 'valid string'),
      ({'description': 'mode', 'choices': ['one'], 'extra': True}, 'Extra inputs'),
      ({'description': 'mode', 'choices': ['one'], 'default': 'one'}, 'must not have a default'),
    ],
  )
  def test_invalid_choice_declarations_fail_at_load(self, fake_packages, declaration, match):
    package = fake_packages('_choice_invalid', {'choose': _spell(parameters={'mode': declaration})})
    with pytest.raises(ValueError, match=match):
      package.bro_class()()

  @pytest.mark.parametrize('default', ['missing', None, 1])
  def test_optional_default_must_be_in_choices(self, fake_packages, default):
    package = fake_packages(
      '_choice_default_invalid',
      {
        'choose': _spell(
          parameters={
            'mode?': {'description': 'mode', 'choices': ['one'], 'default': default},
          }
        )
      },
    )
    with pytest.raises(ValueError, match='outside its choices|valid string'):
      package.bro_class()()

  def test_optional_choice_requires_a_default(self, fake_packages):
    package = fake_packages(
      '_choice_default_missing',
      {
        'choose': _spell(
          parameters={
            'mode?': {'description': 'mode', 'choices': ['one']},
          }
        )
      },
    )
    with pytest.raises(ValueError, match='requires a default'):
      package.bro_class()()

  @pytest.mark.parametrize(
    'name',
    [
      'harness',
      'creds',
      'may_summon',
      'talk',
      'hold',
      'features',
      'true',
      'false',
    ],
  )
  def test_choice_cannot_shadow_a_rendering_fact(self, fake_packages, name):
    package = fake_packages(
      '_choice_reserved',
      {
        'choose': _spell(
          parameters={
            name: {'description': 'mode', 'choices': ['one']},
          }
        )
      },
    )
    with pytest.raises(ValueError, match='conflicts with a rendering fact'):
      package.bro_class()()

  def test_harness_fact_collision_fails_at_render_not_construction(self, fake_packages):
    class ModeHarness(Harness):
      def facts(self):
        return {'mode': True}

    package = fake_packages(
      '_choice_harness_collision',
      {
        'choose': _spell(
          parameters={
            'mode?': {'description': 'mode', 'choices': ['one'], 'default': 'one'},
          }
        )
      },
    )
    bro = package.bro_class()()
    with pytest.raises(ValueError, match='conflict with harness facts: mode'):
      bro.get_spell_body('choose', harness=ModeHarness('test'), hold='unattended')

  def test_template_literal_punctuation_is_a_valid_choice(self, fake_packages):
    package = fake_packages(
      '_choice_literal',
      {
        'choose': _spell(
          parameters={'mode': {'description': 'mode', 'choices': ['A.z_9-']}},
          body='{{iff #mode = A.z_9-}}matched{{end}}',
        )
      },
    )
    assert (
      package.bro_class()().get_spell_body(
        'choose',
        harness='bro',
        hold='unattended',
        choices={'mode': 'A.z_9-'},
      )
      == 'matched'
    )

  @pytest.mark.parametrize(
    ('choices', 'match'),
    [
      ({}, 'missing required'),
      ({'format': 'xml'}, 'outside choices'),
      ({'format': 'text', 'notes': 'context'}, 'unknown choices'),
    ],
  )
  def test_direct_body_render_validates_choice_values(self, choice_package, choices, match):
    with pytest.raises(ValueError, match=match):
      choice_package.bro_class()().get_spell_body(
        'choose',
        harness='bro',
        hold='unattended',
        choices=choices,
      )

  @pytest.fixture
  def choice_package(self, fake_packages):
    return fake_packages(
      '_choice_calls',
      {
        'choose': _spell(
          parameters={
            'mode?': {
              'description': 'output mode',
              'choices': ['short', 'full'],
              'default': 'short',
            },
            'format': {'description': 'output format', 'choices': ['text', 'json']},
            'notes?': 'context',
          },
          body='{{iff #mode = short}}BRIEF{{eliff #mode = full}}COMPLETE{{end}} '
          '{{iff #format = text}}TEXT{{eliff #format = json}}JSON{{end}}',
        )
      },
    )

  @pytest.mark.asyncio
  async def test_both_schemas_expose_choices_and_default(self, choice_package):
    bro = choice_package.bro_class()()
    tool = (await _spell_server(bro).list_tools())[0]
    spell = load_spell('choose', choice_package.files['choose'].path)
    roster_schema = spell_store._cast_roster([spell])[0]['parameters']
    for schema in (tool.parameters, roster_schema):
      assert schema['required'] == ['format']
      for parameter in spell.parameters:
        exposed = schema['properties'][parameter.name]
        assert exposed['description'] == parameter.description
        if parameter.choices:
          assert exposed['enum'] == list(parameter.choices)
        else:
          assert 'enum' not in exposed
        if parameter.default is not None:
          assert exposed['default'] == parameter.default
        else:
          assert 'default' not in exposed

  @pytest.mark.asyncio
  @pytest.mark.parametrize('route', ['tool', 'cast'])
  @pytest.mark.parametrize('harness', ['bro', 'claude'])
  @pytest.mark.parametrize(
    ('arguments', 'expected'),
    [
      ({'format': 'text'}, 'BRIEF TEXT\n\n# Arguments\n\nmode: short (default)\nformat: text'),
      (
        {'mode': 'short', 'format': 'json'},
        'BRIEF JSON\n\n# Arguments\n\nmode: short\nformat: json',
      ),
      (
        {'mode': 'full', 'format': 'text', 'notes': 'detail'},
        'COMPLETE TEXT\n\n# Arguments\n\nmode: full\nformat: text\nnotes: detail',
      ),
    ],
  )
  async def test_binding_and_rendering_on_every_route(
    self,
    choice_package,
    monkeypatch,
    route,
    harness,
    arguments,
    expected,
  ):
    bro = choice_package.bro_class()()
    if route == 'tool':
      server = spell_store.build_spell_server(bro, harness=harness, hold='unattended')
      tool = (await server.list_tools())[0]
      assert await tool.call(arguments) == expected
    else:
      import bro.llm.mu as mu_module

      async def fake_mu(prompt, result_class, *contents, role=None):
        return result_class.model_validate(
          {
            'spell': 'spell::choose',
            'arguments': [{'name': name, 'value': value} for name, value in arguments.items()],
            'error': None,
          }
        )

      monkeypatch.setattr(mu_module, 'mu', types.SimpleNamespace(aio=fake_mu))
      tool = spell_store.build_cast_tool(bro, harness=harness, hold='unattended')
      assert await tool.call({'command': 'choose'}) == f'spell: spell::choose\n\n{expected}'

  @pytest.mark.asyncio
  async def test_native_registry_refuses_invalid_choice_before_rendering(
    self, choice_package, monkeypatch
  ):
    bro = choice_package.bro_class()()

    def fail_render(*args, **kwargs):
      pytest.fail('invalid call reached rendering')

    monkeypatch.setattr(bro, 'get_spell_body', fail_render)
    tool = (await ToolRegistry([_spell_server(bro)]).resolve())[0]
    for arguments in ({'format': 'xml'}, {'format': 'text', 'mode': 'other'}):
      with pytest.raises(ValueError, match='outside choices'):
        await tool.call(arguments)

  @pytest.mark.asyncio
  async def test_cast_refuses_invalid_choice_before_rendering(self, choice_package, monkeypatch):
    import bro.llm.mu as mu_module

    bro = choice_package.bro_class()()

    def fail_render(*args, **kwargs):
      pytest.fail('invalid call reached rendering')

    async def fake_mu(prompt, result_class, *contents, role=None):
      return result_class.model_validate(
        {
          'spell': 'spell::choose',
          'arguments': [{'name': 'format', 'value': 'xml'}],
          'error': None,
        }
      )

    monkeypatch.setattr(bro, 'get_spell_body', fail_render)
    monkeypatch.setattr(mu_module, 'mu', types.SimpleNamespace(aio=fake_mu))
    tool = spell_store.build_cast_tool(bro, harness='bro', hold='unattended')
    with pytest.raises(ValueError, match='outside choices'):
      await tool.call({'command': 'choose xml'})

  def test_claude_server_validates_enum_before_calling_tool(self, choice_package, monkeypatch):
    from starlette.testclient import TestClient

    from bro.runtime.mcp_server import create_http_app

    bro = choice_package.bro_class()()

    def fail_call(*args, **kwargs):
      pytest.fail('invalid call reached spell tool')

    monkeypatch.setattr(spell_store.SpellTool, 'call', fail_call)
    server = spell_store.build_spell_server(bro, harness='claude', hold='unattended')
    with TestClient(create_http_app([server], 'test-token')) as client:
      response = client.post(
        '/spell',
        headers={
          'Authorization': 'Bearer test-token',
          'Accept': 'application/json, text/event-stream',
        },
        json={
          'jsonrpc': '2.0',
          'id': 1,
          'method': 'tools/call',
          'params': {'name': 'choose', 'arguments': {'format': 'xml'}},
        },
      )
    result = response.json()['result']
    assert result['isError'] is True
    assert result['content'][0]['text'].startswith('Input validation error:')
    assert 'xml' in result['content'][0]['text']

  @pytest.mark.parametrize(
    ('body', 'choices', 'match'),
    [
      ('{{iff #mode = shrot}}typo{{else}}other{{end}}', {}, 'shrot'),
      ('{{iff #mode = short}}brief{{end}}', {'mode': 'full'}, 'no branch'),
    ],
  )
  def test_choice_conditions_are_domain_checked_and_exhaustive(
    self, fake_packages, body, choices, match
  ):
    package = fake_packages(
      '_choice_condition',
      {
        'choose': _spell(
          parameters={
            'mode?': {'description': 'mode', 'choices': ['short', 'full'], 'default': 'short'},
          },
          body=body,
        )
      },
    )
    with pytest.raises(TemplateError, match=match):
      package.bro_class()().get_spell_body(
        'choose', harness='bro', hold='unattended', choices=choices
      )

  def test_included_fragment_receives_choice_variables(self, fake_packages, monkeypatch):
    import bro.prompts

    package = fake_packages(
      '_choice_include',
      {
        'choose': _spell(
          parameters={
            'mode?': {'description': 'mode', 'choices': ['short', 'full'], 'default': 'short'},
          },
          body='{{include fragment.md}}',
        )
      },
    )
    monkeypatch.setattr(bro.prompts, 'get_prompt', lambda name: '{{insert #mode}}')
    assert (
      package.bro_class()().get_spell_body('choose', harness='bro', hold='unattended') == 'short'
    )


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
    assert '## Skills' in bro.composed_prompt('bro', hold='unattended')
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
    assert set(tool.parameters['properties']) == {'task', 'notes'}
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

  @pytest.mark.asyncio
  async def test_a_long_body_is_kept_whole_and_paged(self, fake_packages):
    body = '\n'.join(f'line {index}' for index in range(BYTE_LIMIT // 5))
    package = fake_packages('_spell_long', {'do-work': _spell(body=body)})
    tool = (await _spell_server(package.bro_class()()).list_tools())[0]
    reply = await tool.call({})
    (entry,) = results.session_store().entries()
    assert results.session_store().read(entry.id)[1] == body
    assert reply == results.window(entry.id, body, 0, BYTE_LIMIT)


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

    monkeypatch.setattr(spell_store.credentials, 'available', lambda name: name == 'openai')
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

    async def fake_mu(prompt, result_class, *contents, role=None):
      captured['role'] = role
      captured['prompt'] = prompt
      captured['request'] = json.loads(contents[0].text)
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
    assert captured['role'] == 'dispatch'
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

    async def fake_mu(prompt, result_class, *contents, role=None):
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

    async def fake_mu(prompt, result_class, *contents, role=None):
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

    async def fake_mu(prompt, result_class, *contents, role=None):
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
        'unknown arguments',
      ),
      ({'spell': 'spell::do-work', 'arguments': []}, 'missing required'),
      ({'spell': 'spell::do-work', 'arguments': None}, 'missing required'),
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

    async def fake_mu(prompt, result_class, *contents, role=None):
      return result_class.model_validate({**interpretation, 'error': None})

    monkeypatch.setattr(mu_module, 'mu', types.SimpleNamespace(aio=fake_mu))
    with pytest.raises(ValueError, match=match):
      await (await self._tool(package.bro_class()())).call({'command': 'do something'})

  @pytest.mark.asyncio
  async def test_rejects_empty_expected_error(self, fake_packages, monkeypatch):
    import bro.llm.mu as mu_module

    package = fake_packages('_cast_empty_error', {'do-work': _spell()})
    monkeypatch.setattr(spell_store.credentials, 'available', lambda name: True)

    async def fake_mu(prompt, result_class, *contents, role=None):
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
      bro.composed_prompt('bro', hold='unattended')
      .split('## Spells', 1)[1]
      .split('## Skills', 1)[0]
    )
    assert '`/<name>`' not in spells_section
    assert '`bro::cast`' not in spells_section
    assert "call the named spell's own tool" in spells_section
    assert description not in bro.composed_prompt('bro', hold='unattended')

  def test_dispatch_contract_is_present_when_secret_resolves(self, fake_packages, monkeypatch):
    package = fake_packages('_spell_prompt_dispatch', {'do-work': _spell()})
    monkeypatch.setattr(spell_store.credentials, 'available', lambda name: name == CAST_SECRET)
    bro = package.bro_class()()

    assert '## Spells' in bro.composed_prompt('bro', hold='unattended')
    assert '`bro::cast`' in bro.composed_prompt('bro', hold='unattended')
    assert 'follow the returned instructions' in bro.composed_prompt('bro', hold='unattended')

  def test_section_is_absent_without_spells(self, fake_packages):
    package = fake_packages('_spell_prompt_empty')
    assert '## Spells' not in package.bro_class()().composed_prompt('bro', hold='unattended')


class TestSpellOptionalSecret:
  def test_nonempty_roster_declares_cast_secret_for_each_harness(self, fake_packages):
    package = fake_packages('_spell_optional_secret', {'do-work': _spell()})
    bro = package.bro_class()()
    assert bro.optional_secrets(harness='bro') == (CAST_SECRET,)
    assert bro.optional_secrets(harness='claude') == (CAST_SECRET,)

  def test_empty_roster_does_not_declare_cast_secret(self, fake_packages):
    package = fake_packages('_spell_no_optional_secret')
    assert CAST_SECRET not in package.bro_class()().optional_secrets('bro')


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
# that much in one reply fails rather than costing every run a page
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
    for choices in _choice_combinations(spell):
      arguments = {
        parameter.name: choices.get(parameter.name, 'x' * _ARGUMENT_ROOM)
        for parameter in spell.parameters
      }
      defaulted = frozenset(
        parameter.name
        for parameter in spell.parameters
        if parameter.default is not None and choices[parameter.name] == parameter.default
      )
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
                extra={
                  'features': SetVariable(enabled, universe=features.universe),
                  **spell_store.choice_variables(spell, choices),
                },
              ).strip()
              text = f'spell: spell::{spell_name}\n\n' + spell_store.call_text(
                spell, body, arguments, defaulted=defaulted
              )
              assert len(text) <= BYTE_LIMIT, (
                f'spell::{spell_name} of {name} overflows one window on {harness_name} '
                f'at {hold} with {choices} ({len(text):,} characters)'
              )


def _choice_combinations(spell):
  parameters = [parameter for parameter in spell.parameters if parameter.choices]
  for values in product(*(parameter.choices for parameter in parameters)):
    yield dict(zip((parameter.name for parameter in parameters), values, strict=True))


def test_size_probe_visits_every_choice_combination(tmp_path):
  path = tmp_path / 'choose.md'
  path.write_text(
    _spell(
      parameters={
        'mode?': {'description': 'mode', 'choices': ['short', 'full'], 'default': 'short'},
        'format': {'description': 'format', 'choices': ['text', 'json']},
        'notes?': 'context',
      }
    )
  )
  spell = load_spell('choose', path)
  assert list(_choice_combinations(spell)) == [
    {'mode': 'short', 'format': 'text'},
    {'mode': 'short', 'format': 'json'},
    {'mode': 'full', 'format': 'text'},
    {'mode': 'full', 'format': 'json'},
  ]


@pytest.mark.parametrize('optional_choices', [1, 2])
def test_window_probe_rejects_a_defaulted_call_that_would_page(
  fake_packages,
  monkeypatch,
  optional_choices,
):
  parameters = {
    f'mode{index}?': {
      'description': 'mode',
      'choices': ['short', 'full'],
      'default': 'short',
    }
    for index in range(optional_choices)
  }
  package = fake_packages('_choice_window_boundary', {'choose': _spell(parameters=parameters)})
  path = package.files['choose'].path
  spell = load_spell('choose', path)
  arguments = {parameter.name: parameter.default for parameter in spell.parameters}
  prefix = 'spell: spell::choose\n\n'
  argument_listing = spell_store.call_text(spell, '', arguments)
  body = 'x' * (BYTE_LIMIT - len(prefix) - len(argument_listing))
  path.write_text(_spell(parameters=parameters, body=body))
  assert len(prefix + spell_store.call_text(spell, body, arguments)) == BYTE_LIMIT

  bro = package.bro_class()()
  monkeypatch.setattr(sys.modules[__name__], 'create_bro', lambda name: bro)
  with pytest.raises(AssertionError, match='overflows one window'):
    test_every_spell_call_fits_one_window('boundary', monkeypatch)
