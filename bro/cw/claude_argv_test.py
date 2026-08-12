import json
import shlex
import sys
from typing import ClassVar
from unittest.mock import patch

import pytest

import bro.cw.claude_argv as cw_claude_argv
from bro.cw.mcp import MCPEndpoint
from bro.cw.session_test import _spec as _session_spec

_ENDPOINT = MCPEndpoint(port=1234, token='tok')


def _spec(**kwargs):
  kwargs.setdefault('bro', 'dev')
  return _session_spec(**kwargs)


@pytest.fixture(autouse=True)
def _brog_config(monkeypatch):
  # the launch builder constructs the session bro's live servers to enumerate
  # namespaces, and brog's state factory reads the self-contained `brog` secret
  # at build; pin a fake so launches build hermetically
  monkeypatch.setattr(
    'bro.base.credentials.get_json',
    lambda name: {'backend': 'github', 'token': 't', 'repo': 'owner/repository'},
  )


def _dev_namespaces() -> list[str]:
  from bro.registry import create_bro

  return list(dict.fromkeys(s.namespace for s in create_bro('dev').claude_bro_mcp_servers()))


def _dev_persona_namespaces() -> list[str]:
  from bro.registry import create_bro

  return list(dict.fromkeys(s.namespace for s in create_bro('dev').claude_persona_mcp_servers()))


def _cw_session_launch(spec, **kwargs) -> cw_claude_argv.ClaudeLaunch:
  kwargs.setdefault('endpoint', _ENDPOINT)
  with patch('bro.cw.claude_argv._session_append_prompt', return_value='append text'):
    return cw_claude_argv.build_claude_launch(spec, **kwargs)


def _settings(argv: list[str]) -> dict:
  return json.loads(argv[argv.index('--settings') + 1])


class TestCwSessionLaunch:
  def test_basic_shape(self):
    launch = _cw_session_launch(_spec(), claude_args=['--foo'])
    argv = launch.argv
    assert argv[:2] == ['--model', cw_claude_argv._CW_MODEL]
    assert '--bare' not in argv
    assert argv[argv.index('--disallowed-tools') + 1] == 'mcp__claude_ai_*'
    assert argv[argv.index('--append-system-prompt') + 1] == 'append text'
    assert launch.system_prompt == 'append text'
    assert '--foo' in argv

  def test_selected_tool_blocks_reach_disallowed_tools(self, monkeypatch):
    from bro.base.condition import when
    from bro.bro import BaseBro
    from bro.llm.mcp import block, harness

    class BlockingBro(BaseBro):
      name = 'blocking'
      description = 'd'
      tools: ClassVar = [when(harness == 'claude', block('Read', 'Write', 'Bash'))]

      def __init__(self):
        super().__init__(system_prompt='')

    monkeypatch.setattr('bro.registry.create_bro', lambda name: BlockingBro())
    argv = _cw_session_launch(_spec(bro='blocking'), claude_args=[]).argv
    assert argv[argv.index('--disallowed-tools') + 1] == 'mcp__claude_ai_*,Read,Write,Bash'

  def test_fast_mode_lands_in_settings(self):
    assert _settings(_cw_session_launch(_spec(fast=True), claude_args=[]).argv)['fastMode'] is True
    assert _settings(_cw_session_launch(_spec(), claude_args=[]).argv)['fastMode'] is False

  def test_status_line_lands_in_settings(self):
    status_line = _settings(_cw_session_launch(_spec(), claude_args=[]).argv)['statusLine']
    assert status_line['type'] == 'command'
    assert status_line['command'] == f'{shlex.quote(sys.executable)} -m bro.cw.statusline'

  def test_effort_injected(self):
    argv = _cw_session_launch(_spec(effort='xhigh'), claude_args=[]).argv
    assert argv[argv.index('--effort') + 1] == 'xhigh'

  @pytest.mark.parametrize('hold', ['unattended', 'detached', 'attended'])
  def test_non_guided_holds_skip_permissions(self, hold):
    argv = _cw_session_launch(_spec(hold=hold), claude_args=[]).argv
    assert '--dangerously-skip-permissions' in argv

  def test_guided_hold_keeps_permission_prompts(self):
    argv = _cw_session_launch(_spec(hold='guided'), claude_args=[]).argv
    assert '--dangerously-skip-permissions' not in argv

  def test_mcp_config_covers_the_personas_namespaces(self):
    argv = _cw_session_launch(_spec(bro='dev'), claude_args=[]).argv
    config = json.loads(argv[argv.index('--mcp-config') + 1])
    namespaces = _dev_persona_namespaces()
    # the service server's `banner` tool rides the `bro` namespace
    assert 'bro' in namespaces
    assert list(config['mcpServers']) == namespaces
    for namespace, entry in config['mcpServers'].items():
      assert entry['type'] == 'http'
      assert entry['url'] == f'http://127.0.0.1:1234/{namespace}'
      assert entry['headers'] == {'Authorization': 'Bearer tok'}
      assert entry['alwaysLoad'] is True

  def test_cw_session_keeps_the_full_harness(self):
    # no --strict-mcp-config / --allowed-tools: the persona namespaces mount on
    # top of claude's own tools, not instead of them
    argv = _cw_session_launch(_spec(bro='dev'), claude_args=[]).argv
    assert '--strict-mcp-config' not in argv
    assert '--allowed-tools' not in argv

  def test_claude_args_precede_prompt_tail(self):
    argv = _cw_session_launch(_spec(prompt='go'), claude_args=['--x']).argv
    assert argv[-2:] == ['--', 'go']
    assert argv.index('--mcp-config') < argv.index('--x')


class TestRawLaunch:
  def _launch(self, **kwargs) -> cw_claude_argv.ClaudeLaunch:
    spec = _spec(bro='dev', raw=True, **kwargs)
    return cw_claude_argv.build_claude_launch(spec, claude_args=[], endpoint=_ENDPOINT)

  def test_basic_shape(self):
    argv = self._launch().argv
    assert '--bare' in argv
    assert '--strict-mcp-config' in argv
    # --bare skips project/user skill discovery; framework tools provide the
    # script and skill surfaces while built-in slash commands stay enabled
    assert '--disable-slash-commands' not in argv
    # tools disabled (empty string follows --tools)
    assert argv[argv.index('--tools') + 1] == ''

  def test_seeded_prompt_carries_the_launch_note(self):
    argv = self._launch(prompt='do it').argv
    seeded = argv[argv.index('--') + 1]
    assert seeded.startswith('[launch note:')
    assert seeded.endswith('do it')
    # cw-sessions keep the seed verbatim (harness reminders cover them)
    native = _cw_session_launch(_spec(prompt='do it'), claude_args=[]).argv
    assert native[native.index('--') + 1] == 'do it'

  def test_allowed_tools_cover_each_namespace(self):
    argv = self._launch().argv
    assert argv[argv.index('--allowed-tools') + 1] == ','.join(
      f'mcp__{namespace}__*' for namespace in _dev_namespaces()
    )

  def test_mcp_config_one_http_entry_per_namespace(self):
    argv = self._launch().argv
    config = json.loads(argv[argv.index('--mcp-config') + 1])
    namespaces = _dev_namespaces()
    # the service tools ride the `bro` namespace
    assert 'bro' in namespaces
    assert list(config['mcpServers']) == namespaces
    for namespace, entry in config['mcpServers'].items():
      assert entry['type'] == 'http'
      assert entry['url'] == f'http://127.0.0.1:1234/{namespace}'
      assert entry['headers'] == {'Authorization': 'Bearer tok'}
      assert entry['alwaysLoad'] is True

  def test_settings_merge_fast_mode_and_api_key_helper(self):
    # the merged --settings is what lets --fast reach a --raw session
    settings = _settings(self._launch(fast=True).argv)
    assert settings['fastMode'] is True
    assert (
      settings['apiKeyHelper'] == f'{shlex.quote(sys.executable)} -m bro.cw.print_anthropic_key'
    )
    assert _settings(self._launch().argv)['fastMode'] is False

  def test_status_line_lands_in_settings(self):
    status_line = _settings(self._launch().argv)['statusLine']
    assert status_line['command'] == f'{shlex.quote(sys.executable)} -m bro.cw.statusline'

  def test_system_prompt_is_bros_claude_flavor(self):
    from bro.registry import create_bro

    launch = self._launch()
    argv = launch.argv
    prompt = argv[argv.index('--system-prompt') + 1]
    assert prompt.startswith(create_bro('dev').claude_system_prompt)
    assert launch.system_prompt == prompt
    # the flavor whose tool-name rule matches the mcp__<namespace>__<tool> mounts
    assert '`mcp__namespace__tool`' in prompt

  def test_hold_fragment_follows_the_hold(self):
    attended = self._launch(hold='attended')
    assert '# Attended session' in attended.system_prompt
    assert 'full authorization' in attended.system_prompt
    assert '--dangerously-skip-permissions' in attended.argv
    guided = self._launch().system_prompt  # the DEFAULT_HOLD launch
    assert '# Guided session' in guided
    assert 'full authorization' not in guided
    # the fragment renders at build — no directive may leak into the prompt
    assert '{{' not in guided

  def test_unknown_bro_raises(self):
    with pytest.raises(KeyError, match='unknown bro'):
      cw_claude_argv.build_claude_launch(
        _spec(bro='does-not-exist'), claude_args=[], endpoint=_ENDPOINT
      )
