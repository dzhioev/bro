from types import SimpleNamespace
from unittest.mock import patch

import pytest

import ride.cli as ride_cli
from ride.claude.harness import options


@pytest.fixture(autouse=True)
def project(monkeypatch):
  monkeypatch.setattr(
    ride_cli,
    'project_config',
    lambda: SimpleNamespace(default_bro='bro-dev', harness='claude'),
  )
  monkeypatch.setattr(ride_cli, 'fresh_workspace_name', lambda base: f'{base}-12345678')


class TestSolo:
  def test_builds_an_unattended_claude_session(self):
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
    assert spec.inner_command()[:7] == [
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

  def test_forwards_claude_arguments_only_after_the_separator(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      ride_cli.main(['ride', 'solo', 'dev', 'hello', '--', '--debug', 'mcp'])
    assert options(start.call_args.args[0]).arguments == ['--debug', 'mcp']

  def test_bro_solo_owns_rich_and_no_trails(self):
    from ride.bro import options as bro_options

    with patch('ride.cli.start_session', return_value=0) as start:
      assert (
        ride_cli.main(['ride', 'solo', '--harness', 'bro', '--rich', '--no-trails', 'dev', 'hello'])
        == 0
      )
    spec = start.call_args.args[0]
    assert bro_options(spec).rich
    assert bro_options(spec).no_trails
    assert spec.inner_command() == [
      'bro', 'run', 'dev', 'hello', '--rich', '--hold', 'unattended', '--in-place'
    ]  # fmt: skip

  def test_claude_rejects_bro_harness_flags(self, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'solo', '--no-trails', 'dev', 'hello'])
    assert '--no-trails require --harness bro' in capsys.readouterr().err


class TestAlong:
  def test_builds_an_attended_claude_session(self):
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
    assert spec.inner_command()[:7] == [
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

  def test_forwards_claude_arguments_only_after_the_separator(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      ride_cli.main(['ride', 'along', 'dev', 'hello', '--', '--debug', 'mcp'])
    assert options(start.call_args.args[0]).arguments == ['--debug', 'mcp']

  def test_raw_host_combination_errors(self, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'along', '--host', '--raw', 'dev'])
    assert '--raw cannot be combined with --host' in capsys.readouterr().err

  def test_incompatible_provider_names_the_harness_remedy(self, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'along', '--provider', 'openai', 'dev'])
    assert '--harness bro' in capsys.readouterr().err

  def test_bro_harness_builds_a_native_chat(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'along', '--harness', 'bro', '--text', 'dev']) == 0
    spec = start.call_args.args[0]
    assert spec.harness == 'bro'
    assert spec.inner_command() == [
      'bro', 'chat', 'dev', '--text', '--hold', 'attended', '--in-place'
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
