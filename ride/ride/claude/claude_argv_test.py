import json
import shlex
import subprocess
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import pytest

import ride.claude.claude_argv as ride_claude_argv
from bro.base.spawn import console_script, module_argv
from bro.bro import BaseBro
from bro.llm.llms import claude_code
from bro.mcp import ToolLayer, brash, files
from ride.claude import native_tools
from ride.claude.assembly import persona_servers
from ride.claude.mcp import MCPEndpoint
from ride.claude.statusline import statusline_command
from ride.session_test import _spec as _session_spec

_ENDPOINT = MCPEndpoint(port=1234, token='tok')
_ACTIVITY_FILE = Path('/session/activity')


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


def _dev_persona_namespaces() -> list[str]:
  from bro.registry import create_bro

  return list(dict.fromkeys(server.namespace for server in persona_servers(create_bro('dev'))))


def _ride_session_launch(spec, **kwargs) -> ride_claude_argv.ClaudeLaunch:
  kwargs.setdefault('resume_session', None)
  kwargs.setdefault('endpoint', _ENDPOINT)
  kwargs.setdefault('brash_policy', None)
  kwargs.setdefault('activity_file', _ACTIVITY_FILE)
  with patch('ride.claude.claude_argv.session_append_prompt', return_value='append text'):
    return ride_claude_argv.build_claude_launch(spec, **kwargs)


def _settings(argv: list[str]) -> dict:
  return json.loads(argv[argv.index('--settings') + 1])


def _tools(argv: list[str]) -> list[str]:
  return argv[argv.index('--tools') + 1].split(',')


def _declaring(*layers: ToolLayer) -> BaseBro:
  class Declaring(BaseBro):
    name = 'declaring'
    description = 'd'
    tools: ClassVar = list(layers)

    def __init__(self):
      super().__init__(system_prompt='')

  return Declaring()


class TestRideSessionLaunch:
  def test_basic_shape(self):
    argv = _ride_session_launch(_spec()).argv
    assert argv[:2] == ['--model', claude_code.DEFAULT_MODEL]
    assert '--bare' not in argv and '--resume' not in argv
    assert argv[argv.index('--disallowed-tools') + 1] == 'mcp__claude_ai_*'
    assert argv[argv.index('--append-system-prompt') + 1] == 'append text'

  def test_tools_carry_the_allowlist_the_personas_reach_maps_to(self):
    from bro.registry import create_bro

    argv = _ride_session_launch(_spec(bro='dev')).argv
    assert _tools(argv) == list(native_tools.allowlist(create_bro('dev').reach()))

  def test_a_persona_declaring_no_group_keeps_the_loop_tools_and_a_gated_read(self, monkeypatch):
    monkeypatch.setattr('bro.registry.create_bro', lambda name: _declaring())
    argv = _ride_session_launch(_spec(bro='declaring')).argv
    assert _tools(argv) == [native_tools.GATED_READ, *native_tools.LOOP]
    (entry,) = _settings(argv)['hooks']['PreToolUse']
    assert entry['matcher'] == native_tools.GATED_READ
    (hook,) = entry['hooks']
    assert hook['type'] == 'command'
    assert shlex.split(hook['command']) == module_argv('ride.claude.read_gate')

  @pytest.mark.parametrize('write', [True, False])
  def test_files_serve_an_ungated_read(self, monkeypatch, write):
    monkeypatch.setattr('bro.registry.create_bro', lambda name: _declaring(files(write=write)))
    argv = _ride_session_launch(_spec(bro='declaring')).argv
    assert native_tools.GATED_READ in _tools(argv)
    assert 'PreToolUse' not in _settings(argv)['hooks']

  def test_read_only_files_withhold_the_writing_natives(self, monkeypatch):
    monkeypatch.setattr('bro.registry.create_bro', lambda name: _declaring(files(write=False)))
    tools = _tools(_ride_session_launch(_spec(bro='declaring')).argv)
    assert set(native_tools.READ) <= set(tools)
    assert set(native_tools.WRITE).isdisjoint(tools)

  def test_a_finite_command_list_runs_bash_and_monitor_through_the_command_gate(
    self, monkeypatch, tmp_path
  ):
    monkeypatch.setattr(
      'bro.registry.create_bro', lambda name: _declaring(files(), brash('git status'))
    )
    policy = tmp_path / 'brash-policy.json'
    argv = _ride_session_launch(_spec(bro='declaring'), brash_policy=policy).argv
    assert set(native_tools.SHELL) <= set(_tools(argv))
    hooks = _settings(argv)['hooks']['PreToolUse']
    assert [entry['matcher'] for entry in hooks] == ['Bash', 'Monitor']
    for entry in hooks:
      (hook,) = entry['hooks']
      assert hook['type'] == 'command'
      assert shlex.split(hook['command']) == [
        *module_argv('ride.claude.command_gate'),
        console_script('brash'),
        str(policy),
      ]

  def test_a_brash_policy_goes_with_a_finite_command_list_and_nothing_else(
    self, monkeypatch, tmp_path
  ):
    with pytest.raises(ValueError, match='brash policy'):
      _ride_session_launch(_spec(), brash_policy=tmp_path / 'brash-policy.json')
    monkeypatch.setattr('bro.registry.create_bro', lambda name: _declaring(brash('git status')))
    with pytest.raises(ValueError, match='brash policy'):
      _ride_session_launch(_spec(bro='declaring'))

  def test_a_summoning_session_without_brash_gets_no_shell(self, monkeypatch):
    from bro.summon import LAUNCH_ENV, encode_launch

    monkeypatch.setattr('bro.registry.create_bro', lambda name: _declaring())
    monkeypatch.setenv(
      LAUNCH_ENV,
      encode_launch({'bro': {'bros': frozenset({'reviewer'})}}),
    )
    argv = _ride_session_launch(_spec(bro='declaring')).argv
    assert set(native_tools.SHELL).isdisjoint(_tools(argv))
    matchers = [entry['matcher'] for entry in _settings(argv)['hooks']['PreToolUse']]
    assert matchers == [native_tools.GATED_READ]

  def test_an_unrestricted_shell_declares_no_tool_gate(self):
    assert 'PreToolUse' not in _settings(_ride_session_launch(_spec()).argv)['hooks']

  def test_every_session_is_woken_by_the_watch_waiter_at_each_turn_end(self):
    for spec in (_spec(), _spec(solo=True, hold='unattended', prompt='go')):
      hooks = _settings(_ride_session_launch(spec).argv)['hooks']
      for event in ('Stop', 'StopFailure'):
        (entry,) = [entry for entry in hooks[event] if entry['hooks'][0].get('asyncRewake')]
        (hook,) = entry['hooks']
        assert 'matcher' not in entry
        assert hook['asyncRewake'] is True
        # the waiter's one argument is the bound claude ends it at
        expected = [*module_argv('ride.claude.watch_waiter'), str(hook['timeout'])]
        assert shlex.split(hook['command'])[-len(expected) :] == expected

  def test_every_turn_and_tool_call_touches_the_activity_file(self, tmp_path):
    for event in ride_claude_argv.ACTIVITY_EVENTS:
      activity_file = tmp_path / event
      argv = _ride_session_launch(_spec(), activity_file=activity_file).argv
      (command,) = [
        hook['command']
        for entry in _settings(argv)['hooks'][event]
        for hook in entry['hooks']
        if hook.get('asyncRewake') is None
      ]
      subprocess.run(['sh', '-c', command], check=True)
      assert activity_file.is_file()

  def test_a_summoning_solo_session_keeps_both_hook_kinds(self, monkeypatch, tmp_path):
    from bro.summon import LAUNCH_ENV, encode_launch

    monkeypatch.setattr('bro.registry.create_bro', lambda name: _declaring(brash('git status')))
    monkeypatch.setenv(
      LAUNCH_ENV,
      encode_launch({'bro': {'bros': frozenset({'reviewer'})}}),
    )
    argv = _ride_session_launch(
      _spec(bro='declaring', solo=True, hold='unattended', prompt='go'),
      brash_policy=tmp_path / 'brash-policy.json',
    ).argv

    hooks = _settings(argv)['hooks']
    assert [entry['matcher'] for entry in hooks['PreToolUse']] == ['Bash', 'Monitor', 'Read']
    assert 'ride.claude.watch_waiter' in shlex.split(hooks['Stop'][0]['hooks'][0]['command'])

  def test_fast_mode_lands_in_settings(self):
    assert _settings(_ride_session_launch(_spec(llm='+fast')).argv)['fastMode'] is True
    assert _settings(_ride_session_launch(_spec()).argv)['fastMode'] is False

  def test_status_line_lands_in_settings(self):
    status_line = _settings(_ride_session_launch(_spec()).argv)['statusLine']
    assert status_line['type'] == 'command'
    assert status_line['command'] == statusline_command()

  def test_effort_injected(self):
    argv = _ride_session_launch(_spec(llm='::xhigh')).argv
    assert argv[argv.index('--effort') + 1] == 'xhigh'

  @pytest.mark.parametrize('hold', ['unattended', 'detached', 'attended'])
  def test_non_guided_holds_skip_permissions(self, hold):
    argv = _ride_session_launch(_spec(hold=hold)).argv
    assert '--dangerously-skip-permissions' in argv

  def test_guided_hold_keeps_permission_prompts(self):
    argv = _ride_session_launch(_spec(hold='guided')).argv
    assert '--dangerously-skip-permissions' not in argv

  def test_mcp_config_covers_the_personas_namespaces(self):
    argv = _ride_session_launch(_spec(bro='dev')).argv
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

  def test_the_launch_config_is_the_sessions_only_mcp_source(self):
    argv = _ride_session_launch(_spec(bro='dev')).argv
    assert '--strict-mcp-config' in argv

  def test_a_resume_continues_its_session_before_the_prompt_tail(self):
    argv = _ride_session_launch(_spec(prompt='go'), resume_session='session-id').argv
    assert argv[-4:] == ['--resume', 'session-id', '--', 'go']

  def test_solo_adds_print_mode_to_the_full_session_composition(self):
    argv = _ride_session_launch(_spec(solo=True, hold='unattended', prompt='answer')).argv
    assert '-p' in argv
    assert '--dangerously-skip-permissions' in argv
    assert '--mcp-config' in argv
    assert '--append-system-prompt' in argv
    assert '--no-session-persistence' not in argv


def test_unknown_bro_raises():
  with pytest.raises(KeyError, match='unknown bro'):
    ride_claude_argv.build_claude_launch(
      _spec(bro='does-not-exist'),
      resume_session=None,
      endpoint=_ENDPOINT,
      brash_policy=None,
      activity_file=_ACTIVITY_FILE,
    )


def test_the_attribution_opt_out_lands_in_settings():
  launch = _ride_session_launch(_spec())
  assert _settings(launch.argv)['attribution'] == ride_claude_argv._ATTRIBUTION


def test_a_solo_session_streams_its_prompt_over_stdin():
  launch = _ride_session_launch(_spec(solo=True, hold='unattended', prompt='go'))

  assert launch.prompt == 'go'
  assert '--' not in launch.argv and 'go' not in launch.argv
  assert '-p' in launch.argv and '--verbose' in launch.argv
  for flag, value in (('--input-format', 'stream-json'), ('--output-format', 'stream-json')):
    assert launch.argv[launch.argv.index(flag) + 1] == value


def test_an_interactive_session_seeds_its_prompt_through_the_argv():
  launch = _ride_session_launch(_spec(prompt='hi'))

  assert launch.prompt is None
  assert launch.argv[-2:] == ['--', 'hi'] and '-p' not in launch.argv
