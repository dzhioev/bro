"""Live probes of the command gate against the pinned Claude Code.

Under a finite command list the gate rewrites each `Bash` and `Monitor` call into
a brash call. These probes hold that a line reaches brash untouched and only
brash starts its programs, that brash's refusal reaches the model, that
`Monitor`'s WebSocket form is denied, that the places `competing_hooks` scans are
places the pinned release loads `PreToolUse` hooks from, under each matcher
form, and that a managed policy allowing only managed hooks turns the gate off.
A release that changes any of them fails here instead of taking a session's
lines around brash unseen.
"""

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from bro import brash_policy, mcp
from bro.base.spawn import console_script
from ride.claude.claude_argv import STREAM_JSON_ARGS, _command_gate_hooks, reach_arguments
from ride.claude.claude_config import _SESSION_SETTINGS_JSON
from ride.claude.competing_hooks import find
from ride.claude.live_claude_test_helper import (
  REQUIRES_CLAUDE_CREDENTIAL,
  claude_token,
  pinned_claude,
)

pytestmark = REQUIRES_CLAUDE_CREDENTIAL

_SESSION_TIMEOUT_SECONDS = 300
_REACH = mcp.Reach(brash=mcp.Brash(commands=('probe ...',)))
# single quotes, a newline inside quotes, a substitution, and every list operator
_ROUND_TRIP_LINES = (
  "probe 'it'\"'\"'s' \"two\nlines\"",
  'probe "$(probe inner)" && probe outer | probe piped; false || probe last',
)
_REFUSED_LINE = 'probe listed; cat /dev/null'

# a stand-in program recording each start: its arguments, and the command line
# of the process that started it
_PROBE = """#!{python}
import json, os, sys
with open(f'/proc/{{os.getppid()}}/cmdline', 'rb') as parent:
  started_by = [part.decode() for part in parent.read().split(b'\\0') if part]
with open({log!r}, 'a') as log:
  log.write(json.dumps({{'argv': sys.argv[1:], 'started_by': started_by}}) + '\\n')
"""
# a hook recording the tag it was declared under
_HOOK = """#!{python}
import sys
with open({log!r}, 'a') as log:
  log.write(sys.argv[1] + '\\n')
"""


class _Session:
  """a workspace and Claude folder a probe drives the pinned release in, with the
  `probe` program on the session's PATH and a shell prefix recording each command
  string Claude's shell is handed."""

  def __init__(self, root: Path, *, telemetry: bool = False) -> None:
    self.root = root
    self.workspace = root / 'above' / 'workspace'
    self.workspace.mkdir(parents=True)
    self.config = root / 'config'
    self.config.mkdir()
    (self.config / '.claude.json').write_text(
      json.dumps(
        {
          'hasCompletedOnboarding': True,
          'projects': {str(self.workspace): {'hasTrustDialogAccepted': True}},
        }
      )
    )
    settings = json.loads(json.dumps(_SESSION_SETTINGS_JSON))
    if telemetry:
      # the pinned release serves Monitor only where it may fetch its flags
      del settings['env']['DISABLE_TELEMETRY']
    (self.config / 'settings.json').write_text(json.dumps(settings))
    programs = root / 'bin'
    programs.mkdir()
    self.probe_log = root / 'probe.log'
    self.hook_log = root / 'hook.log'
    self.shell_log = root / 'shell.log'
    for name, source, log in (('probe', _PROBE, self.probe_log), ('hook', _HOOK, self.hook_log)):
      program = programs / name
      program.write_text(source.format(python=sys.executable, log=str(log)))
      program.chmod(0o755)
    prefix = root / 'shell-prefix'
    prefix.write_text(
      f'#!/bin/sh\nprintf "%s\\0" "$1" >> {shlex.quote(str(self.shell_log))}\nexec bash -c "$1"\n'
    )
    prefix.chmod(0o755)
    self.env = {
      name: value
      for name, value in os.environ.items()
      if name not in ('CLAUDECODE', 'CLAUDE_CODE_CHILD_SESSION', 'ANTHROPIC_API_KEY')
      and not name.startswith('CLAUDE_CODE_SHELL')
    }
    self.env.update(
      CLAUDE_CODE_OAUTH_TOKEN=claude_token() or '',
      CLAUDE_CONFIG_DIR=str(self.config),
      DISABLE_AUTOUPDATER='1',
      PATH=f'{programs}:{os.environ["PATH"]}',
      CLAUDE_CODE_SHELL_PREFIX=str(prefix),
    )
    if telemetry:
      self.env.pop('DISABLE_TELEMETRY', None)

  def hook(self, tag: str) -> str:
    return shlex.join([str(self.root / 'bin' / 'hook'), tag])

  def run(self, prompt: str, *arguments: str) -> list[dict[str, Any]]:
    message = {'type': 'user', 'message': {'role': 'user', 'content': prompt}}
    completed = subprocess.run(
      [
        str(pinned_claude()),
        '--model',
        'haiku',
        '--dangerously-skip-permissions',
        *arguments,
        *STREAM_JSON_ARGS,
      ],
      cwd=self.workspace,
      env=self.env,
      input=json.dumps(message) + '\n',
      capture_output=True,
      text=True,
      timeout=_SESSION_TIMEOUT_SECONDS,
    )
    assert completed.returncode == 0, completed.stderr
    return [json.loads(line) for line in completed.stdout.splitlines()]

  def probe_starts(self) -> list[dict[str, Any]]:
    if not self.probe_log.exists():
      return []
    return [json.loads(line) for line in self.probe_log.read_text().splitlines()]

  def shell_commands(self) -> list[str]:
    if not self.shell_log.exists():
      return []
    return [command for command in self.shell_log.read_text().split('\0') if command]


def _gated_arguments(policy: Path) -> list[str]:
  return [
    *reach_arguments(_REACH),
    '--settings',
    json.dumps({'hooks': _command_gate_hooks(policy)}),
  ]


def _tool_calls(events: list[dict[str, Any]], tool: str) -> list[tuple[dict[str, Any], str]]:
  """each call of `tool` in a session's stream: its input, and the result the
  model was handed for it."""
  calls: dict[str, dict[str, Any]] = {}
  results: dict[str, str] = {}
  for event in events:
    content = event.get('message', {}).get('content')
    if not isinstance(content, list):
      continue
    for block in content:
      if event['type'] == 'assistant' and block['type'] == 'tool_use' and block['name'] == tool:
        calls[block['id']] = block['input']
      elif event['type'] == 'user' and block.get('type') == 'tool_result':
        result = block['content']
        results[block['tool_use_id']] = (
          result if isinstance(result, str) else ''.join(part.get('text', '') for part in result)
        )
  return [(calls[identifier], results[identifier]) for identifier in calls]


def _verbatim(lines: tuple[str, ...]) -> str:
  listing = '\n\n'.join(
    f'Command {number}, between the markers:\n<<<\n{line}\n>>>'
    for number, line in enumerate(lines, start=1)
  )
  return (
    'Run each command below with the Bash tool, one Bash call per command, in order. '
    'Pass each command exactly as written between its markers, byte for byte, newlines '
    'included, and run nothing else. Then reply with the single word done.\n\n' + listing
  )


def _bash_starts(session: _Session, line: str) -> list[list[str]]:
  """the probe starts bash makes running `line`: the reference brash runs it against."""
  before = len(session.probe_starts())
  subprocess.run(['bash', '-c', line], env=session.env, check=False)
  return sorted(start['argv'] for start in session.probe_starts()[before:])


def _started_by_brash(start: dict[str, Any]) -> bool:
  return console_script('brash') in start['started_by']


def test_bash_lines_run_in_brash_untouched_and_a_refusal_reaches_the_model(
  tmp_path: Path,
) -> None:
  session = _Session(tmp_path)
  policy = brash_policy.write(tmp_path, _REACH)
  assert policy is not None

  lines = (*_ROUND_TRIP_LINES, _REFUSED_LINE)
  calls = _tool_calls(session.run(_verbatim(lines), *_gated_arguments(policy)), 'Bash')

  assert [call['command'] for call, _ in calls] == list(lines)
  for line in lines:
    # Claude's shell evaluates the gate's argv as one quoted word, and nothing else
    brash_call = shlex.join([console_script('brash'), '--policy', str(policy), '-c', line])
    assert any(f'eval {shlex.quote(brash_call)}' in command for command in session.shell_commands())
  starts = session.probe_starts()
  assert len(starts) > 0
  for start in starts:
    assert start['started_by'][-4:] == ['--policy', str(policy), '-c', start['started_by'][-1]]
    assert _started_by_brash(start) and start['started_by'][-1] in _ROUND_TRIP_LINES
  assert sorted(start['argv'] for start in starts) == sorted(
    argv for line in _ROUND_TRIP_LINES for argv in _bash_starts(session, line)
  )
  _, refusal = calls[-1]
  assert "brash: refused 'cat'" in refusal


def test_monitor_commands_run_in_brash_and_its_websocket_form_is_denied(tmp_path: Path) -> None:
  session = _Session(tmp_path, telemetry=True)
  policy = brash_policy.write(tmp_path, _REACH)
  assert policy is not None
  prompt = (
    'Load the Monitor tool with ToolSearch, querying "select:Monitor". Then call Monitor '
    'exactly twice: first with command "probe monitored" and description "probe"; then with '
    'description "socket" and the ws parameter {"url": "wss://example.invalid/stream"}, and no '
    'command. Then reply with the single word done.'
  )
  events = session.run(prompt, *_gated_arguments(policy))

  (init,) = (event for event in events if event.get('subtype') == 'init')
  assert 'Monitor' in init['tools'], 'the pinned release served no Monitor'
  (command_call, _), (socket_call, denial) = _tool_calls(events, 'Monitor')
  assert command_call['command'] == 'probe monitored'
  assert 'ws' in socket_call and 'command' not in socket_call
  assert 'a call carrying no command is refused' in denial
  deadline = time.monotonic() + 30
  while len(session.probe_starts()) == 0:
    assert time.monotonic() < deadline, 'the monitored command never started'
    time.sleep(0.1)
  (start,) = session.probe_starts()
  assert start['argv'] == ['monitored'] and _started_by_brash(start)


def _skill(path: Path, *, matcher: str, tag: str, hook: str) -> Path:
  path.parent.mkdir(parents=True)
  path.write_text(
    '---\n'
    f'description: Use this skill when asked to run the {tag} probe.\n'
    'hooks:\n'
    '  PreToolUse:\n'
    f'    - matcher: {json.dumps(matcher)}\n'
    '      hooks:\n'
    '        - type: command\n'
    f'          command: {json.dumps(hook)}\n'
    '---\n'
    f'Run `probe {tag}` with the Bash tool.\n'
  )
  return path


def _hooks(matcher: str, command: str) -> dict[str, Any]:
  return {'PreToolUse': [{'matcher': matcher, 'hooks': [{'type': 'command', 'command': command}]}]}


def test_the_scanned_places_and_matcher_forms_are_where_the_release_loads_hooks(
  tmp_path: Path,
) -> None:
  session = _Session(tmp_path)
  claude_folder = session.workspace / '.claude'
  claude_folder.mkdir()
  project_settings = claude_folder / 'settings.json'
  project_settings.write_text(
    json.dumps(
      {
        'hooks': _hooks('', session.hook('project-settings')),
        'enabledPlugins': {'probe-plugin@probe-market': True},
      }
    )
  )
  local_settings = claude_folder / 'settings.local.json'
  local_settings.write_text(json.dumps({'hooks': _hooks('*', session.hook('local-settings'))}))
  skill = _skill(
    claude_folder / 'skills' / 'probe-skill' / 'SKILL.md',
    matcher='Bash',
    tag='skill',
    hook=session.hook('skill'),
  )
  skill_above = _skill(
    session.workspace.parent / '.claude' / 'skills' / 'above-skill' / 'SKILL.md',
    matcher='^Ba.h$',
    tag='skill-above',
    hook=session.hook('skill-above'),
  )
  market = tmp_path / 'market'
  plugin = market / 'plugin'
  plugin_hooks = plugin / 'hooks' / 'hooks.json'
  plugin_hooks.parent.mkdir(parents=True)
  plugin_hooks.write_text(json.dumps({'hooks': _hooks('Edit|Bash', session.hook('plugin'))}))
  (plugin / '.claude-plugin').mkdir()
  (plugin / '.claude-plugin' / 'plugin.json').write_text(json.dumps({'name': 'probe-plugin'}))
  (market / '.claude-plugin').mkdir()
  (market / '.claude-plugin' / 'marketplace.json').write_text(
    json.dumps(
      {
        'name': 'probe-market',
        'owner': {'name': 'probe'},
        'plugins': [{'name': 'probe-plugin', 'source': './plugin'}],
      }
    )
  )
  records = session.config / 'plugins'
  records.mkdir()
  (records / 'known_marketplaces.json').write_text(
    json.dumps(
      {
        'probe-market': {
          'source': {'source': 'directory', 'path': str(market)},
          'installLocation': str(market),
          'lastUpdated': '2026-10-09T00:00:00.000Z',
        }
      }
    )
  )
  (records / 'installed_plugins.json').write_text(
    json.dumps(
      {
        'version': 2,
        'plugins': {
          'probe-plugin@probe-market': [
            {
              'scope': 'user',
              'installPath': str(plugin),
              'installedAt': '2026-10-09T00:00:00.000Z',
              'lastUpdated': '2026-10-09T00:00:00.000Z',
            }
          ]
        },
      }
    )
  )

  prompt = (
    'Invoke the probe-skill skill with the Skill tool and follow it. Then invoke the '
    'above-skill skill with the Skill tool and follow it. Then reply with the single word done.'
  )
  session.run(prompt, '--tools', 'Bash,Skill')

  ran = set(session.hook_log.read_text().split())
  assert ran == {'project-settings', 'local-settings', 'skill', 'skill-above', 'plugin'}
  assert sorted(find(session.workspace, session.config)) == sorted(
    [
      f'{project_settings}: a PreToolUse hook matching every tool',
      f'{local_settings}: a PreToolUse hook matching every tool',
      f"{skill}: a PreToolUse hook matching 'Bash'",
      f"{skill_above}: a PreToolUse hook matching '^Ba.h$'",
      f"{plugin_hooks}: a PreToolUse hook matching 'Edit|Bash'",
    ]
  )


@pytest.mark.parametrize('managed', [False, True])
def test_a_managed_policy_allowing_only_managed_hooks_turns_the_gate_off(
  tmp_path: Path, managed: bool
) -> None:
  session = _Session(tmp_path)
  policy = brash_policy.write(tmp_path, _REACH)
  assert policy is not None
  arguments = _gated_arguments(policy)
  if managed:
    arguments += ['--managed-settings', json.dumps({'allowManagedHooksOnly': True})]

  session.run(_verbatim(('probe managed',)), *arguments)

  (start,) = session.probe_starts()
  assert start['argv'] == ['managed']
  assert _started_by_brash(start) is not managed
