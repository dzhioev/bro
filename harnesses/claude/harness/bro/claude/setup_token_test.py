import json
import stat
from pathlib import Path

import pytest

from bro.base import credentials
from bro.claude import claude_release, setup_token
from bro.claude.setup_token import SetupTokenError, __cli_name__, mint, setup_command
from bro.workspace.paths import ISOLATION_ENV

TOKEN = 'sk-ant-oat01-minted'


@pytest.fixture
def host(tmp_path, monkeypatch):
  """a host terminal whose pinned Claude Code is a stub that records its run, and whose
  store is empty; returns the stub's run log."""
  monkeypatch.delenv(ISOLATION_ENV, raising=False)
  monkeypatch.setattr(credentials, 'STORE_DIR', str(tmp_path / 'store'))
  monkeypatch.setattr(setup_token.sys.stdin, 'isatty', lambda: True)
  monkeypatch.setattr(claude_release, 'host_platform', lambda: 'linux-x64')
  runs = tmp_path / 'runs.log'
  binary = tmp_path / 'claude'
  binary.write_text(
    f'#!/bin/bash\necho "$* DISABLE_AUTOUPDATER=$DISABLE_AUTOUPDATER" >> {runs}\n'
    f'echo "$CLAUDE_CONFIG_DIR" > {tmp_path / "config-directory"}\n'
    'exit "${CLAUDE_STATUS:-0}"\n'
  )
  binary.chmod(0o755)
  monkeypatch.setattr(claude_release, 'cached_binary', lambda version, platform: binary)
  monkeypatch.setattr(setup_token.getpass, 'getpass', lambda prompt: f'  {TOKEN}\n')
  return runs


def _material(name: str) -> Path:
  return Path(credentials.STORE_DIR) / credentials.MATERIAL_DIR / f'{name}.cred'


def test_the_minted_token_is_stored_as_the_instance(host):
  summary = mint('work')

  material = _material('claude_code+work')
  assert material.read_text() == TOKEN
  assert stat.S_IMODE(material.stat().st_mode) == 0o600
  assert summary == f'stored claude_code+work at {material}'
  assert host.read_text() == 'setup-token DISABLE_AUTOUPDATER=1\n'


def test_setup_token_runs_under_a_throwaway_claude_config(host, tmp_path, monkeypatch):
  monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'host-claude'))

  mint('')

  directory = Path((tmp_path / 'config-directory').read_text().strip())
  assert directory != tmp_path / 'host-claude'
  assert not directory.exists()


def test_minting_again_replaces_the_stored_token(host):
  _material('claude_code').parent.mkdir(parents=True)
  _material('claude_code').write_text('sk-ant-oat01-older')

  summary = mint('')

  assert _material('claude_code').read_text() == TOKEN
  assert summary.startswith('replaced claude_code at ')


def test_the_empty_instance_is_stored_and_reported_past_a_default_pick(host):
  store = Path(credentials.STORE_DIR)
  store.mkdir()
  (store / 'creds.json').write_text(json.dumps({'defaults': ['claude_code+work']}))

  summary = mint('')

  assert summary == f'stored claude_code at {_material("claude_code")}'
  assert _material('claude_code').read_text() == TOKEN
  assert not _material('claude_code+work').exists()


def test_a_pasted_api_key_is_refused(host, monkeypatch):
  monkeypatch.setattr(setup_token.getpass, 'getpass', lambda prompt: 'sk-ant-api03-key')

  with pytest.raises(SetupTokenError, match='not a setup token'):
    mint('')

  assert not _material('claude_code').exists()


def test_a_failed_setup_token_stores_nothing(host, monkeypatch):
  monkeypatch.setenv('CLAUDE_STATUS', '1')

  def unexpected(prompt):
    raise AssertionError('prompted after a failed setup-token')

  monkeypatch.setattr(setup_token.getpass, 'getpass', unexpected)

  with pytest.raises(SetupTokenError, match='exited with status 1'):
    mint('')

  assert not _material('claude_code').exists()


def test_a_managed_session_is_refused(host, monkeypatch):
  monkeypatch.setenv(ISOLATION_ENV, 'boxed')

  with pytest.raises(SetupTokenError, match='outside a managed session'):
    mint('')

  assert not host.exists()


def test_a_host_without_a_terminal_is_refused(host, monkeypatch):
  monkeypatch.setattr(setup_token.sys.stdin, 'isatty', lambda: False)

  with pytest.raises(SetupTokenError, match='requires a terminal'):
    mint('')


def test_a_malformed_instance_is_refused_before_minting(host):
  with pytest.raises(SetupTokenError, match='malformed'):
    mint('two words')

  assert not host.exists()


@pytest.mark.parametrize(
  ('name', 'arguments'), [('claude_code', []), ('claude_code+work', ['--instance', 'work'])]
)
def test_the_setup_command_mints_the_named_instance(name, arguments):
  assert setup_command(name) == ' '.join([__cli_name__, *arguments])


def test_the_setup_command_refuses_another_kind():
  with pytest.raises(ValueError, match='no claude_code instance'):
    setup_command('github+dev')
