import json
from pathlib import Path
from unittest.mock import patch

import pytest

import cw.claude_argv
from cw.mcp import MCPEndpoint
from cw.session_test import _spec

_WORKSPACE = Path('/ws')
_ENDPOINT = MCPEndpoint(port=1234, token='tok')


def _pm_namespaces() -> list[str]:
  from bro.registry import create_bro

  return list(dict.fromkeys(s.namespace for s in create_bro('pm').claude_bro_mcp_servers()))


def _native_launch(spec, **kwargs) -> cw.claude_argv.ClaudeLaunch:
  with patch('cw.claude_argv._session_append_prompt', return_value='append text'):
    return cw.claude_argv.build_claude_launch(spec, workspace=_WORKSPACE, **kwargs)


def _settings(argv: list[str]) -> dict:
  return json.loads(argv[argv.index('--settings') + 1])


class TestNativeLaunch:
  def test_basic_shape(self):
    launch = _native_launch(_spec(), claude_args=['--foo'])
    argv = launch.argv
    assert argv[:2] == ['--model', cw.claude_argv._CW_MODEL]
    assert '--bare' not in argv
    assert argv[argv.index('--disallowed-tools') + 1] == 'mcp__claude_ai_*'
    assert argv[argv.index('--append-system-prompt') + 1] == 'append text'
    assert launch.system_prompt == 'append text'
    assert '--foo' in argv

  def test_fast_mode_lands_in_settings(self):
    assert _settings(_native_launch(_spec(fast=True), claude_args=[]).argv)['fastMode'] is True
    assert _settings(_native_launch(_spec(), claude_args=[]).argv)['fastMode'] is False

  def test_stop_listen_hook_in_settings(self):
    hooks = _settings(_native_launch(_spec(), claude_args=[]).argv)['hooks']
    (entry,) = hooks['Stop'][0]['hooks']
    # the workspace's own venv script, absolute — hook commands run with no venv on PATH
    assert entry == {'type': 'command', 'command': '/ws/.venv/bin/cw.listen', 'timeout': 60}

  def test_effort_injected(self):
    argv = _native_launch(_spec(effort='xhigh'), claude_args=[]).argv
    assert argv[argv.index('--effort') + 1] == 'xhigh'

  def test_auto_skips_permissions(self):
    assert '--dangerously-skip-permissions' in _native_launch(_spec(auto=True), claude_args=[]).argv
    assert '--dangerously-skip-permissions' not in _native_launch(_spec(), claude_args=[]).argv

  def test_mcp_http_uses_deployed_config(self):
    with patch(
      'cw.claude_argv.credentials.get_json',
      return_value={'url': 'https://flow.example', 'token': 'T'},
    ):
      argv = _native_launch(_spec(mcp='http'), claude_args=[]).argv
    entry = json.loads(argv[argv.index('--mcp-config') + 1])['mcpServers']['flow']
    assert entry == {
      'type': 'http',
      'url': 'https://flow.example',
      'headers': {'Authorization': 'Bearer T'},
      'alwaysLoad': True,
    }

  def test_mcp_local_uses_endpoint(self):
    argv = _native_launch(_spec(mcp='local'), claude_args=[], endpoint=_ENDPOINT).argv
    entry = json.loads(argv[argv.index('--mcp-config') + 1])['mcpServers']['flow']
    assert entry['url'] == 'http://127.0.0.1:1234/flow'
    assert entry['headers'] == {'Authorization': 'Bearer tok'}

  def test_mcp_local_without_endpoint_raises(self):
    with pytest.raises(ValueError, match='session-local MCP endpoint'):
      _native_launch(_spec(mcp='local'), claude_args=[])

  def test_skills_dir_added_before_prompt_tail(self):
    argv = _native_launch(_spec(prompt='do it'), claude_args=[], skills_dir=Path('/skills')).argv
    assert argv[-4:] == ['--add-dir', '/skills', '--', 'do it']

  def test_claude_args_precede_prompt_tail(self):
    argv = _native_launch(
      _spec(prompt='go', mcp='local'), claude_args=['--x'], endpoint=_ENDPOINT
    ).argv
    assert argv[-2:] == ['--', 'go']
    assert argv.index('--mcp-config') < argv.index('--x')


class TestBroLaunch:
  def _launch(self, **kwargs) -> cw.claude_argv.ClaudeLaunch:
    spec = _spec(bro='pm', **kwargs)
    return cw.claude_argv.build_claude_launch(
      spec, workspace=_WORKSPACE, claude_args=[], endpoint=_ENDPOINT
    )

  def test_basic_shape(self):
    argv = self._launch().argv
    assert '--bare' in argv
    assert '--strict-mcp-config' in argv
    # skills reach a --bro session through the `bro::skill` MCP tool (--bare
    # skips .claude/skills/ discovery); built-in slash commands stay enabled
    assert '--disable-slash-commands' not in argv
    # tools disabled (empty string follows --tools)
    assert argv[argv.index('--tools') + 1] == ''

  def test_allowed_tools_cover_each_namespace(self):
    argv = self._launch().argv
    assert argv[argv.index('--allowed-tools') + 1] == ','.join(
      f'mcp__{namespace}__*' for namespace in _pm_namespaces()
    )

  def test_mcp_config_one_http_entry_per_namespace(self):
    argv = self._launch().argv
    config = json.loads(argv[argv.index('--mcp-config') + 1])
    namespaces = _pm_namespaces()
    # the service server's `skill` tool rides the `bro` namespace
    assert 'bro' in namespaces
    assert list(config['mcpServers']) == namespaces
    for namespace, entry in config['mcpServers'].items():
      assert entry['type'] == 'http'
      assert entry['url'] == f'http://127.0.0.1:1234/{namespace}'
      assert entry['headers'] == {'Authorization': 'Bearer tok'}
      assert entry['alwaysLoad'] is True

  def test_settings_merge_fast_mode_and_api_key_helper(self):
    # the merged --settings is what lets --fast reach a --bro session; the
    # apiKeyHelper is the workspace's own copy, mode-neutral
    settings = _settings(self._launch(fast=True).argv)
    assert settings['fastMode'] is True
    assert settings['apiKeyHelper'] == '/ws/setup/print_anthropic_key.sh'
    assert _settings(self._launch().argv)['fastMode'] is False

  def test_stop_listen_hook_in_settings(self):
    # flagSettings is the only settings source a --bare session loads, so the
    # listener must ride it to reach the bro flavor
    hooks = _settings(self._launch().argv)['hooks']
    (entry,) = hooks['Stop'][0]['hooks']
    assert entry['command'] == '/ws/.venv/bin/cw.listen'

  def test_system_prompt_is_bros_claude_flavor(self):
    from bro.registry import create_bro

    launch = self._launch()
    argv = launch.argv
    prompt = argv[argv.index('--system-prompt') + 1]
    assert prompt.startswith(create_bro('pm').claude_system_prompt)
    assert launch.system_prompt == prompt
    # the flavor whose tool-name rule matches the mcp__<namespace>__<tool> mounts
    assert '`mcp__namespace__tool`' in prompt

  def test_mode_fragment_follows_auto(self):
    manual = self._launch().system_prompt
    assert '# Manual session' in manual
    assert '# Autonomous session' not in manual
    auto = self._launch(auto=True)
    assert '# Autonomous session' in auto.system_prompt
    assert 'Land mode: PR' in auto.system_prompt
    assert '--dangerously-skip-permissions' in auto.argv

  def test_without_endpoint_raises(self):
    with pytest.raises(ValueError, match='session-local MCP endpoint'):
      cw.claude_argv.build_claude_launch(_spec(bro='pm'), workspace=_WORKSPACE, claude_args=[])

  def test_unknown_bro_raises(self):
    with pytest.raises(KeyError, match='unknown bro'):
      cw.claude_argv.build_claude_launch(
        _spec(bro='does-not-exist'), workspace=_WORKSPACE, claude_args=[], endpoint=_ENDPOINT
      )
