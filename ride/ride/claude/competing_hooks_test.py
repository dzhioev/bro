import json
from pathlib import Path

import pytest

from ride.claude.competing_hooks import find
from ride.claude.native_tools import COMMAND_TOOLS, GATED_READ


def _hooks(matcher: str | None = 'Bash', event: str = 'PreToolUse') -> dict:
  group: dict = {'hooks': [{'type': 'command', 'command': 'rewrite'}]}
  if matcher is not None:
    group['matcher'] = matcher
  return {event: [group]}


def _write(path: Path, text: str) -> Path:
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(text)
  return path


def _component(path: Path, hooks: dict, extra: str = 'description: d') -> Path:
  fields = '\n'.join(f'  {line}' for line in json.dumps(hooks, indent=1).splitlines())
  return _write(path, f'---\n{extra}\nhooks:\n{fields}\n---\nbody\n')


@pytest.fixture
def project(tmp_path) -> Path:
  directory = tmp_path / 'above' / 'project'
  directory.mkdir(parents=True)
  return directory


@pytest.fixture
def config(tmp_path) -> Path:
  directory = tmp_path / 'config'
  directory.mkdir()
  _write(directory / 'settings.json', json.dumps({'enabledPlugins': {}}))
  return directory


def _plugin(config: Path, root: Path, *, key: str = 'probe@market') -> Path:
  _write(root / '.claude-plugin' / 'plugin.json', json.dumps({'name': key.split('@')[0]}))
  record = config / 'plugins' / 'installed_plugins.json'
  installed = json.loads(record.read_text()) if record.exists() else {'version': 2, 'plugins': {}}
  installed['plugins'][key] = [{'scope': 'user', 'installPath': str(root)}]
  _write(record, json.dumps(installed))
  return root


class TestSettings:
  @pytest.mark.parametrize('name', ['settings.json', 'settings.local.json'])
  def test_a_project_settings_hook_on_bash_is_found(self, project, config, name):
    path = _write(project / '.claude' / name, json.dumps({'hooks': _hooks()}))
    assert find(project, config, COMMAND_TOOLS) == [f"{path}: a PreToolUse hook matching 'Bash'"]

  @pytest.mark.parametrize(
    'matcher', [None, '', '*', 'Monitor', 'Edit|Bash', 'Write, Monitor', 'Ba.*', '^Mon']
  )
  def test_every_matcher_form_claude_applies_to_bash_or_monitor_counts(
    self, project, config, matcher
  ):
    _write(project / '.claude' / 'settings.json', json.dumps({'hooks': _hooks(matcher)}))
    assert len(find(project, config, COMMAND_TOOLS)) == 1

  @pytest.mark.parametrize('matcher', ['Edit|Write', 'Bashful', 'mcp__.*', '^Edit$'])
  def test_a_matcher_reaching_neither_tool_is_left_alone(self, project, config, matcher):
    _write(project / '.claude' / 'settings.json', json.dumps({'hooks': _hooks(matcher)}))
    assert find(project, config, COMMAND_TOOLS) == []

  def test_the_scan_looks_for_hooks_on_the_tools_it_is_given(self, project, config):
    path = _write(project / '.claude' / 'settings.json', json.dumps({'hooks': _hooks(GATED_READ)}))
    assert find(project, config, COMMAND_TOOLS) == []
    assert find(project, config, (GATED_READ,)) == [f"{path}: a PreToolUse hook matching 'Read'"]

  def test_other_events_and_empty_groups_are_left_alone(self, project, config):
    hooks = {**_hooks(event='PostToolUse'), 'PreToolUse': [{'matcher': 'Bash', 'hooks': []}]}
    _write(project / '.claude' / 'settings.json', json.dumps({'hooks': hooks}))
    assert find(project, config, COMMAND_TOOLS) == []

  def test_settings_above_the_working_directory_are_left_alone(self, project, config):
    # Claude reads project settings from its working directory alone
    _write(project.parent / '.claude' / 'settings.json', json.dumps({'hooks': _hooks()}))
    assert find(project, config, COMMAND_TOOLS) == []


class TestComponents:
  @pytest.mark.parametrize(
    'relative',
    [
      'project/.claude/skills/probe/SKILL.md',
      'project/.claude/commands/group/probe.md',
      'project/.claude/agents/probe.md',
      '.claude/skills/probe/SKILL.md',
      'project/package/.claude/skills/probe/SKILL.md',
    ],
  )
  def test_a_component_claude_would_load_carrying_a_bash_hook_is_found(
    self, project, config, relative
  ):
    path = _component(project.parent / relative, _hooks())
    assert find(project, config, COMMAND_TOOLS) == [f"{path}: a PreToolUse hook matching 'Bash'"]

  def test_a_component_in_the_sessions_claude_folder_is_found(self, project, config):
    path = _component(config / 'skills' / 'probe' / 'SKILL.md', _hooks('Monitor'))
    assert find(project, config, COMMAND_TOOLS) == [f"{path}: a PreToolUse hook matching 'Monitor'"]

  def test_a_skills_supporting_file_is_not_a_skill(self, project, config):
    _component(project / '.claude' / 'skills' / 'probe' / 'reference.md', _hooks())
    assert find(project, config, COMMAND_TOOLS) == []

  def test_a_value_claude_reads_as_a_quoted_string_parses(self, project, config):
    path = _component(
      project / '.claude' / 'skills' / 'probe' / 'SKILL.md',
      _hooks(),
      extra='description: Use when: the user asks [for] it',
    )
    assert find(project, config, COMMAND_TOOLS) == [f"{path}: a PreToolUse hook matching 'Bash'"]


class TestPlugins:
  def test_an_enabled_plugins_hooks_and_components_are_found(self, tmp_path, project, config):
    root = _plugin(config, tmp_path / 'plugin')
    hooks_file = _write(root / 'hooks' / 'hooks.json', json.dumps({'hooks': _hooks()}))
    skill = _component(root / 'skills' / 'probe' / 'SKILL.md', _hooks('*'))
    _write(
      project / '.claude' / 'settings.json', json.dumps({'enabledPlugins': {'probe@market': True}})
    )

    assert find(project, config, COMMAND_TOOLS) == [
      f"{hooks_file}: a PreToolUse hook matching 'Bash'",
      f'{skill}: a PreToolUse hook matching every tool',
    ]

  def test_a_manifests_inline_and_listed_hooks_are_found(self, tmp_path, project, config):
    root = _plugin(config, tmp_path / 'plugin')
    manifest = root / '.claude-plugin' / 'plugin.json'
    listed = _write(root / 'extra-hooks.json', json.dumps({'hooks': _hooks('Monitor')}))
    _write(manifest, json.dumps({'name': 'probe', 'hooks': ['./extra-hooks.json']}))
    _write(config / 'settings.json', json.dumps({'enabledPlugins': {'probe@market': True}}))
    assert find(project, config, COMMAND_TOOLS) == [
      f"{listed}: a PreToolUse hook matching 'Monitor'"
    ]

    _write(manifest, json.dumps({'name': 'probe', 'hooks': _hooks()}))
    assert find(project, config, COMMAND_TOOLS) == [
      f"{manifest}: a PreToolUse hook matching 'Bash'"
    ]

  def test_a_plugin_local_settings_disable_is_left_alone(self, tmp_path, project, config):
    root = _plugin(config, tmp_path / 'plugin')
    _write(root / 'hooks' / 'hooks.json', json.dumps({'hooks': _hooks()}))
    enabled = {'enabledPlugins': {'probe@market': True}}
    _write(project / '.claude' / 'settings.json', json.dumps(enabled))
    _write(
      project / '.claude' / 'settings.local.json',
      json.dumps({'enabledPlugins': {'probe@market': False}}),
    )
    assert find(project, config, COMMAND_TOOLS) == []

  def test_an_installed_plugin_no_settings_enable_is_left_alone(self, tmp_path, project, config):
    root = _plugin(config, tmp_path / 'plugin')
    _write(root / 'hooks' / 'hooks.json', json.dumps({'hooks': _hooks()}))
    assert find(project, config, COMMAND_TOOLS) == []


class TestUnreadable:
  @pytest.mark.parametrize(
    ('relative', 'text'),
    [
      ('project/.claude/settings.json', '{"hooks": '),
      ('project/.claude/settings.local.json', '[]'),
      ('project/.claude/settings.json', json.dumps({'hooks': {'PreToolUse': {'Bash': []}}})),
      ('project/.claude/settings.json', json.dumps({'hooks': _hooks('(?<x>Bash)')})),
      ('project/.claude/skills/probe/SKILL.md', '---\nhooks: [unclosed\n---\n'),
    ],
  )
  def test_a_candidate_the_scan_cannot_read_is_found(self, project, config, relative, text):
    path = _write(project.parent / relative, text)
    (finding,) = find(project, config, COMMAND_TOOLS)
    assert finding.startswith(f'{path}: cannot be read')

  def test_an_unreadable_installed_plugin_record_is_found(self, project, config):
    record = _write(config / 'plugins' / 'installed_plugins.json', 'not json')
    _write(config / 'settings.json', json.dumps({'enabledPlugins': {'probe@market': True}}))
    (finding,) = find(project, config, COMMAND_TOOLS)
    assert finding.startswith(f'{record}: cannot be read')


def test_a_session_configured_as_ride_configures_it_has_nothing_to_report(project, config):
  # ride's own settings enable the pyright plugin, which ships no hooks
  root = _plugin(config, config / 'plugins' / 'cache' / 'pyright', key='pyright-lsp@official')
  _write(root / 'README.md', '# pyright\n')
  _write(config / 'settings.json', json.dumps({'enabledPlugins': {'pyright-lsp@official': True}}))
  assert find(project, config, (*COMMAND_TOOLS, GATED_READ)) == []
