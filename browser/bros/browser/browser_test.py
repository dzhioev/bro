import subprocess
import sys

from bro.harness import claude
from bros.browser import Browser


def test_browser_module_imports_without_host_optional_sdks() -> None:
  probe = subprocess.run(
    [
      sys.executable,
      '-c',
      'import sys; import bros.browser; '
      "assert 'openai' not in sys.modules; "
      "assert 'mcp' not in sys.modules; "
      "assert not any(name == 'ride' or name.startswith('ride.') for name in sys.modules)",
    ],
    capture_output=True,
    text=True,
  )

  assert probe.returncode == 0, probe.stderr


def test_browser_declares_its_worker_seed_reader_and_spell() -> None:
  browser = Browser()

  assert browser._may_launch == ('webview',)
  assert 'openai' in browser.optional_secrets('bro')
  assert 'browse' in browser.spell_paths
  assert browser.get_spell_body('browse', harness='bro').startswith('# browse\n')


def test_browse_spell_uses_session_watch_notifications_and_branches_final_delivery() -> None:
  browser = Browser()

  for harness in ('bro', 'claude'):
    body = browser.get_spell_body('browse', harness=harness)
    assert 'session watch delivers owner questions as notifications' in body
    assert '[[watch quest watch]]' not in body
    assert 'call `bro::answer`' in body
    assert 'do not call `bro::answer`' in body


def test_browser_mounts_the_same_bounded_roster_on_both_harnesses() -> None:
  for harness in ('bro', 'claude'):
    namespaces = {
      server.namespace for server in Browser().assemble(harness=harness, include_raise=False)
    }
    assert {'webview', 'browser', 'artifact', 'current-time-source'} <= namespaces


def test_browser_withholds_claudes_file_shell_delegation_and_web_tools() -> None:
  blocked = set(Browser().blocked_tool_names('claude'))

  assert {*claude.FILES, *claude.SHELL, *claude.DELEGATION, *claude.WEB} <= blocked
  assert Browser().blocked_tool_names('bro') == ()
  assert Browser()._selected_tools_for('bro').shell_unrestricted is False
