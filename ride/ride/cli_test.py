from types import SimpleNamespace
from unittest.mock import patch

import pytest

import ride.cli as ride_cli
from bro.workspace.model import Workspace
from ride.harness import get_harness


@pytest.fixture(autouse=True)
def project(monkeypatch):
  monkeypatch.setattr(
    ride_cli,
    'project_config',
    lambda: SimpleNamespace(default_bro='bro-dev', harness='claude'),
  )
  monkeypatch.setattr(ride_cli, 'fresh_workspace_name', lambda base: f'{base}-12345678')


def _inner_command(spec, tmp_path) -> list[str]:
  workspace = Workspace.ensure(spec.name, tmp_path, spec.kind)
  return get_harness(spec.harness).inner_command(spec, workspace)


class TestSolo:
  def test_builds_an_unattended_claude_session(self, tmp_path):
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'solo', 'dev', 'do it']) == 0
    spec = start.call_args.args[0]
    assert spec.harness == 'claude'
    assert spec.name == 'ride-dev-12345678'
    assert spec.bro == 'dev'
    assert spec.prompt == 'do it'
    assert spec.hold == 'unattended'
    assert spec.solo
    assert spec.drop
    assert not spec.workspace_pinned
    assert _inner_command(spec, tmp_path)[:7] == [
      'ride',
      'solo',
      '--in-place',
      '--workspace',
      'ride-dev-12345678',
      '--harness',
      'claude',
    ]

  def test_host_keeps_the_unattended_default(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      ride_cli.main(['ride', 'solo', '--host', 'dev', 'do it'])
    assert start.call_args.args[0].hold == 'unattended'

  def test_keep_retains_an_automatic_workspace(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      ride_cli.main(['ride', 'solo', '--keep', 'dev', 'do it'])
    assert not start.call_args.args[0].drop

  def test_pinned_workspace_is_retained(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      ride_cli.main(['ride', 'solo', '--workspace', 'shared', 'dev', 'do it'])
    spec = start.call_args.args[0]
    assert spec.name == 'shared'
    assert spec.workspace_pinned
    assert not spec.drop

  def test_forwards_arguments_only_after_the_separator(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      ride_cli.main(['ride', 'solo', 'dev', 'hello', '--', '--debug', 'mcp'])
    assert start.call_args.args[0].arguments == ['--debug', 'mcp']

  def test_no_trails_is_a_neutral_flag_the_bro_harness_env_carries(self, tmp_path):
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'solo', '--harness', 'bro', '--no-trails', 'dev', 'hello']) == 0
    spec = start.call_args.args[0]
    assert spec.no_trails
    assert _inner_command(spec, tmp_path) == [
      'bro', 'run', 'dev', 'hello', '--hold', 'unattended', '--in-place'
    ]  # fmt: skip

  def test_no_trails_is_restated_in_the_claude_inner_argv(self, tmp_path):
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'solo', '--no-trails', 'dev', 'hello']) == 0
    spec = start.call_args.args[0]
    assert spec.no_trails
    assert '--no-trails' in _inner_command(spec, tmp_path)


class TestAlong:
  def test_builds_an_attended_claude_session(self, tmp_path):
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'along', 'dev', 'do it']) == 0
    spec = start.call_args.args[0]
    assert spec.harness == 'claude'
    assert spec.name == 'ride-dev-12345678'
    assert spec.bro == 'dev'
    assert spec.prompt == 'do it'
    assert spec.hold == 'attended'
    assert not spec.drop
    assert not spec.workspace_pinned
    assert _inner_command(spec, tmp_path)[:7] == [
      'ride',
      'along',
      '--in-place',
      '--workspace',
      'ride-dev-12345678',
      '--harness',
      'claude',
    ]

  def test_host_defaults_to_guided(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      ride_cli.main(['ride', 'along', '--host', 'dev'])
    assert start.call_args.args[0].hold == 'guided'

  def test_workspace_pins_an_existing_name(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      ride_cli.main(['ride', 'along', '--workspace', 'shared', 'dev'])
    spec = start.call_args.args[0]
    assert spec.name == 'shared'
    assert spec.workspace_pinned

  def test_pinned_workspace_rejects_drop(self, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'along', '--workspace', 'shared', '--drop', 'dev'])
    assert 'pinned workspaces are always kept' in capsys.readouterr().err

  def test_forwards_arguments_only_after_the_separator(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      ride_cli.main(['ride', 'along', 'dev', 'hello', '--', '--debug', 'mcp'])
    assert start.call_args.args[0].arguments == ['--debug', 'mcp']

  def test_forwarded_arguments_reach_the_bro_harness_too(self, tmp_path):
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'along', '--harness', 'bro', 'dev', '--', '--fork']) == 0
    spec = start.call_args.args[0]
    assert spec.arguments == ['--fork']
    assert _inner_command(spec, tmp_path) == [
      'bro', 'chat', 'dev', '--hold', 'attended', '--fork', '--in-place'
    ]  # fmt: skip

  def test_raw_host_combination_errors(self, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'along', '--host', '--raw', 'dev'])
    assert '--raw cannot be combined with --host' in capsys.readouterr().err

  def test_incompatible_provider_names_the_harness_remedy(self, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'along', '--provider', 'openai', 'dev'])
    assert '--harness bro' in capsys.readouterr().err

  def test_bro_harness_builds_a_native_chat(self, tmp_path):
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'along', '--harness', 'bro', 'dev']) == 0
    spec = start.call_args.args[0]
    assert spec.harness == 'bro'
    assert _inner_command(spec, tmp_path) == [
      'bro', 'chat', 'dev', '--hold', 'attended', '--in-place'
    ]  # fmt: skip

  def test_project_harness_default_is_used(self, monkeypatch):
    monkeypatch.setattr(
      ride_cli,
      'project_config',
      lambda: SimpleNamespace(default_bro='bro-dev', harness='bro'),
    )
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'along', 'dev']) == 0
    assert start.call_args.args[0].harness == 'bro'


class TestLifecycle:
  def test_resume_dispatches_scope_overrides(self):
    with patch('ride.cli.resume_session', return_value=0) as resume:
      assert ride_cli.main(['ride', 'resume', '--grant', '@dev', 'workspace']) == 0
    assert resume.call_args.args == ('workspace',)
    assert resume.call_args.kwargs == {'grant': ['@dev'], 'revoke': []}

  def test_scope_dispatches_harness(self):
    with patch('ride.scope_report.report_scope', return_value=0) as report:
      assert ride_cli.main(['ride', 'scope', '--harness', 'claude', '--raw']) == 0
    assert report.call_args.kwargs == {'bro': None, 'harness': 'claude', 'raw': True}

  def test_an_unusable_runtime_location_is_a_cli_error(self, monkeypatch, caplog):
    monkeypatch.setenv('XDG_DATA_HOME', 'share')
    assert ride_cli.main(['ride', 'list']) == 1
    assert 'XDG_DATA_HOME must be an absolute path' in caplog.text
