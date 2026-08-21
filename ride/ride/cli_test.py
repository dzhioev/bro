from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import ride.cli as ride_cli
from ride.harness import get_harness
from ride.inner import inner_command


@pytest.fixture(autouse=True)
def project(monkeypatch):
  monkeypatch.setattr(
    ride_cli,
    'project_config',
    lambda _repo=None: SimpleNamespace(default_bro='bro-dev', harness='claude'),
  )
  monkeypatch.setattr(ride_cli, 'fresh_workspace_name', lambda base: f'{base}-12345678')


def _inner_command(spec) -> list[str]:
  harness = get_harness(spec.harness)
  return inner_command(spec, harness_flags=harness.inner_flags(spec))


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
    assert _inner_command(spec)[:7] == [
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

  def test_pinned_workspace_rejects_keep(self, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'solo', '--workspace', 'shared', '--keep', 'dev', 'do it'])
    assert 'pinned workspaces are always kept' in capsys.readouterr().err

  def test_forwards_arguments_only_after_the_separator(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      ride_cli.main(['ride', 'solo', 'dev', 'hello', '--', '--debug', 'mcp'])
    assert start.call_args.args[0].arguments == ['--debug', 'mcp']

  def test_no_trails_is_a_neutral_flag_the_bro_harness_env_carries(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'solo', '--harness', 'bro', '--no-trails', 'dev', 'hello']) == 0
    spec = start.call_args.args[0]
    assert spec.no_trails
    assert _inner_command(spec) == [
      'ride', 'solo', '--in-place', '--workspace', 'ride-dev-12345678', '--harness', 'bro',
      '--no-trails', '--hold', 'unattended', 'dev', 'hello',
    ]  # fmt: skip

  def test_no_trails_is_restated_in_the_claude_inner_argv(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'solo', '--no-trails', 'dev', 'hello']) == 0
    spec = start.call_args.args[0]
    assert spec.no_trails
    assert '--no-trails' in _inner_command(spec)


class TestAttachment:
  def test_mode_launches_detached_by_default(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'along', 'dev']) == 0
    spec = start.call_args.args[0]
    assert spec.repo is None
    assert spec.harness == 'claude'

  def test_repo_resolves_any_directory_inside_the_checkout(self, monkeypatch):
    monkeypatch.setattr(ride_cli, 'project_root', lambda path: Path('/repo'))
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'along', '--repo', '/repo/subdir', 'dev']) == 0
    assert start.call_args.args[0].repo == '/repo'

  def test_git_url_is_preserved_in_the_session_spec(self, monkeypatch):
    url = 'https://example.test/owner/repo.git'
    repository = SimpleNamespace(
      identity=url,
      is_url=True,
      project_config=lambda: SimpleNamespace(default_bro='bro-dev', harness='claude', sections={}),
    )
    monkeypatch.setattr(ride_cli, '_resolve_repository_argument', lambda _value: repository)
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'along', '--repo', url, 'dev']) == 0
    spec, resolved = start.call_args.args
    assert resolved is repository
    assert spec.repo == url

  def test_into_requires_an_attachment(self, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'along', '--into', 'feature', 'dev'])
    assert '--into requires --repo' in capsys.readouterr().err


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
    assert _inner_command(spec)[:7] == [
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

  def test_forwarded_arguments_reach_the_bro_harness_too(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'along', '--harness', 'bro', 'dev', '--', '--fork']) == 0
    spec = start.call_args.args[0]
    assert spec.arguments == ['--fork']
    assert _inner_command(spec) == [
      'ride', 'along', '--in-place', '--workspace', 'ride-dev-12345678', '--harness', 'bro',
      '--hold', 'attended', 'dev', '--', '--fork',
    ]  # fmt: skip

  def test_raw_host_combination_errors(self, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'along', '--host', '--raw', 'dev'])
    assert '--raw cannot be combined with --host' in capsys.readouterr().err

  def test_incompatible_provider_names_the_harness_remedy(self, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'along', '--provider', 'openai', 'dev'])
    assert '--harness bro' in capsys.readouterr().err

  def test_an_unknown_bro_is_named_on_either_harness(self, capsys):
    for harness in ('claude', 'bro'):
      with pytest.raises(SystemExit):
        ride_cli.main(['ride', 'along', '--harness', harness, 'no-such-bro'])
      assert "unknown bro: 'no-such-bro'" in capsys.readouterr().err

  def test_bro_harness_builds_a_native_chat(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'along', '--harness', 'bro', 'dev']) == 0
    spec = start.call_args.args[0]
    assert spec.harness == 'bro'
    assert _inner_command(spec) == [
      'ride', 'along', '--in-place', '--workspace', 'ride-dev-12345678', '--harness', 'bro',
      '--hold', 'attended', 'dev',
    ]  # fmt: skip

  def test_project_harness_default_is_used(self, monkeypatch):
    monkeypatch.setattr(
      ride_cli,
      'project_config',
      lambda _repo: SimpleNamespace(default_bro='bro-dev', harness='bro'),
    )
    monkeypatch.setattr(ride_cli, 'project_root', lambda _path: Path('/repo'))
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'along', '--repo', '/repo', 'dev']) == 0
    assert start.call_args.args[0].harness == 'bro'


class TestLifecycle:
  def test_outer_command_migrates_legacy_runtime_state_first(self):
    with (
      patch('ride.cli.migrate_legacy_runtime_state') as migrate,
      patch('ride.cli.list_workspaces', return_value=0),
    ):
      assert ride_cli.main(['ride', 'list']) == 0
    migrate.assert_called_once_with()

  def test_inner_mode_does_not_run_host_state_migration(self):
    with (
      patch('ride.cli.migrate_legacy_runtime_state') as migrate,
      patch('ride.inner.run_in_place', return_value=0),
    ):
      assert (
        ride_cli.main(['ride', 'solo', '--in-place', '--workspace', 'session', 'dev', 'prompt'])
        == 0
      )
    migrate.assert_not_called()

  def test_resume_dispatches_scope_overrides(self):
    with patch('ride.cli.resume_session', return_value=0) as resume:
      assert ride_cli.main(['ride', 'resume', '--grant', '@dev', 'workspace']) == 0
    assert resume.call_args.args == ('workspace',)
    assert resume.call_args.kwargs == {'grant': ['@dev'], 'revoke': []}

  def test_scope_dispatches_harness(self):
    with patch('ride.scope_report.report_scope', return_value=0) as report:
      assert ride_cli.main(['ride', 'scope', '--bro', 'dev', '--harness', 'claude', '--raw']) == 0
    assert report.call_args.kwargs == {
      'repo': None,
      'bro': 'dev',
      'harness': 'claude',
      'options': {'raw': True},
    }

  def test_scope_rejects_a_non_selected_harness_flag(self, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'scope', '--bro', 'dev', '--harness', 'bro', '--raw'])
    assert '--raw requires --harness claude' in capsys.readouterr().err

  def test_an_unusable_runtime_location_is_a_cli_error(self, monkeypatch, caplog):
    monkeypatch.setenv('XDG_DATA_HOME', 'share')
    assert ride_cli.main(['ride', 'list']) == 1
    assert 'XDG_DATA_HOME must be an absolute path' in caplog.text


class TestSummonedLaunch:
  @pytest.fixture
  def pending(self, monkeypatch, tmp_path):
    import ride.pending_summon as pending_summon

    record = pending_summon.PendingSummon(
      token='TOK-1',
      socket='/broker/CH.sock',
      target='dev',
      prompt='work this out with the user',
      parent_workspace=str(tmp_path / 'parent'),
      may_summon=('bro',),
      grant=('aws',),
      revoke=('openai',),
      summoner={'trail_id': 'T1'},
    )
    pending_summon.write(record)
    return record

  def test_summoned_launch_takes_prompt_and_scope_from_the_record(self, pending):
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'along', '--summoned', 'TOK-1', 'dev']) == 0
    spec = start.call_args.args[0]
    assert spec.prompt == 'work this out with the user'
    assert spec.grant == ['aws']
    assert spec.revoke == ['openai']
    assert start.call_args.kwargs['summoned'] == pending

  def test_user_credential_overrides_layer_on_the_records(self, pending):
    with patch('ride.cli.start_session', return_value=0) as start:
      ride_cli.main(['ride', 'along', '--summoned', 'TOK-1', '--grant', 'github', 'dev'])
    assert start.call_args.args[0].grant == ['aws', 'github']

  def test_summoned_refuses_a_prompt(self, pending, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'along', '--summoned', 'TOK-1', 'dev', 'my own prompt'])
    assert 'takes its initial prompt from the summon request' in capsys.readouterr().err

  def test_summoned_refuses_into(self, pending, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'along', '--summoned', 'TOK-1', '--into', 'master', 'dev'])
    assert 'takes its base from the summon request' in capsys.readouterr().err

  def test_summoned_refuses_bro_overrides(self, pending, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'along', '--summoned', 'TOK-1', '--grant', '@bro', 'dev'])
    assert 'drop the @bro override(s): bro' in capsys.readouterr().err

  def test_summoned_validates_the_bro_against_the_record(self, pending, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'along', '--summoned', 'TOK-1', 'bro'])
    assert "names bro 'dev', not 'bro'" in capsys.readouterr().err

  def test_unknown_token_is_a_cli_error(self, pending, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'along', '--summoned', 'TOK-9', 'dev'])
    assert 'no pending manual summon for token' in capsys.readouterr().err

  def test_solo_has_no_summoned_flag(self, pending, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'solo', '--summoned', 'TOK-1', 'dev', 'p'])
    capsys.readouterr()
