import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import ride.cli as ride_cli
from bro.base import configs
from bro.workspace.paths import workspace_dir
from ride import pending_summon
from ride.do_ride import command as do_ride_command
from ride.harness import get_harness
from ride.workspace.metadata import Isolation


@pytest.fixture(autouse=True)
def project(monkeypatch):
  monkeypatch.setattr(
    ride_cli,
    'project_config',
    lambda _repo=None: SimpleNamespace(
      default_bro='bro-dev',
      harness='claude',
      summon_harness=configs.DEFAULT_SUMMON_HARNESS,
      summon_depth=configs.DEFAULT_SUMMON_DEPTH,
    ),
  )
  monkeypatch.setattr(ride_cli, 'fresh_workspace_name', lambda base: f'{base}-12345678')
  monkeypatch.setattr(ride_cli, 'reexec_from_runtime', lambda _runtime, _argv: None)


def _session_command(spec) -> list[str]:
  harness = get_harness(spec.harness)
  return do_ride_command(spec, harness_flags=harness.session_flags(spec))


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
    assert _session_command(spec)[:6] == [
      'do-ride',
      'solo',
      '--workspace',
      'ride-dev-12345678',
      '--harness',
      'claude',
    ]

  def test_unboxed_keeps_the_unattended_default(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      ride_cli.main(['ride', 'solo', '--unboxed', 'dev', 'do it'])
    assert start.call_args.args[0].hold == 'unattended'
    assert start.call_args.args[0].isolation is Isolation.UNBOXED

  def test_boxed_is_the_default_and_has_an_explicit_spelling(self):
    for flags in ([], ['--boxed']):
      with patch('ride.cli.start_session', return_value=0) as start:
        ride_cli.main(['ride', 'solo', *flags, 'dev', 'do it'])
      assert start.call_args.args[0].isolation is Isolation.BOXED

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

  def test_no_trails_stays_an_outer_launch_setting(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'solo', '--harness', 'bro', '--no-trails', 'dev', 'hello']) == 0
    spec = start.call_args.args[0]
    assert spec.no_trails
    assert '--no-trails' not in _session_command(spec)


class TestAttachment:
  def test_mode_launches_detached_by_default(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'along', 'dev']) == 0
    spec = start.call_args.args[0]
    assert spec.repo is None
    assert spec.harness == 'claude'

  def test_detached_launch_resolves_depth_without_a_project(self):
    with (
      patch('ride.cli.host_config.summon_depth', return_value=6) as resolve_depth,
      patch('ride.cli.start_session', return_value=0) as start,
    ):
      assert ride_cli.main(['ride', 'along', 'dev']) == 0

    resolve_depth.assert_called_once_with(None)
    assert start.call_args.args[0].summon_depth == 6

  def test_detached_launch_summons_under_the_default_harness(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'along', 'dev']) == 0
    assert start.call_args.args[0].summon_harness == configs.DEFAULT_SUMMON_HARNESS

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
      project_config=lambda: SimpleNamespace(
        default_bro='bro-dev',
        harness='claude',
        summon_harness=configs.DEFAULT_SUMMON_HARNESS,
        summon_depth=configs.DEFAULT_SUMMON_DEPTH,
        sections={},
      ),
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

  def test_external_tree_is_recorded_as_an_absolute_path(self, tmp_path):
    tree = tmp_path / 'task'
    tree.mkdir()
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'solo', '--unboxed', '--tree', str(tree), 'dev', 'work']) == 0
    assert start.call_args.args[0].tree == str(tree.resolve())

  @pytest.mark.parametrize('flags', [[], ['--boxed'], ['--unboxed', '--repo', '.']])
  def test_external_tree_requires_a_detached_unboxed_launch(self, tmp_path, flags, capsys):
    tree = tmp_path / 'task'
    tree.mkdir()
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'solo', *flags, '--tree', str(tree), 'dev', 'work'])
    assert '--tree' in capsys.readouterr().err

  def test_given_runtime_is_recorded_as_an_absolute_path(self, tmp_path):
    runtime = tmp_path / 'runtime'
    with patch('ride.cli.start_session', return_value=0) as start:
      assert (
        ride_cli.main(
          ['ride', 'solo', '--unboxed', '--runtime-bundle', str(runtime), 'dev', 'work']
        )
        == 0
      )
    assert start.call_args.args[0].runtime_bundle == str(runtime.resolve())


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
    assert _session_command(spec)[:6] == [
      'do-ride',
      'along',
      '--workspace',
      'ride-dev-12345678',
      '--harness',
      'claude',
    ]

  def test_unboxed_defaults_to_guided(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      ride_cli.main(['ride', 'along', '--unboxed', 'dev'])
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
    assert _session_command(spec) == [
      'do-ride', 'along', '--workspace', 'ride-dev-12345678', '--harness', 'bro',
      '--hold', 'attended', 'dev', '--', '--fork',
    ]  # fmt: skip

  def test_raw_unboxed_combination_errors(self, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'along', '--unboxed', '--raw', 'dev'])
    assert '--raw cannot be combined with --unboxed' in capsys.readouterr().err

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
    assert _session_command(spec) == [
      'do-ride', 'along', '--workspace', 'ride-dev-12345678', '--harness', 'bro',
      '--hold', 'attended', 'dev',
    ]  # fmt: skip

  def test_attached_launch_passes_project_depth_to_host_resolution(self, monkeypatch):
    monkeypatch.setattr(
      ride_cli,
      'project_config',
      lambda _repo: SimpleNamespace(
        default_bro='bro-dev',
        harness='claude',
        summon_harness=configs.DEFAULT_SUMMON_HARNESS,
        summon_depth=5,
      ),
    )
    monkeypatch.setattr(ride_cli, 'project_root', lambda _path: Path('/repo'))
    with (
      patch('ride.cli.host_config.summon_depth', return_value=7) as resolve_depth,
      patch('ride.cli.start_session', return_value=0) as start,
    ):
      assert ride_cli.main(['ride', 'along', '--repo', '/repo', 'dev']) == 0

    resolve_depth.assert_called_once_with(5)
    assert start.call_args.args[0].summon_depth == 7

  def test_project_harness_default_is_used(self, monkeypatch):
    monkeypatch.setattr(
      ride_cli,
      'project_config',
      lambda _repo: SimpleNamespace(
        default_bro='bro-dev',
        harness='bro',
        summon_harness=configs.DEFAULT_SUMMON_HARNESS,
        summon_depth=configs.DEFAULT_SUMMON_DEPTH,
      ),
    )
    monkeypatch.setattr(ride_cli, 'project_root', lambda _path: Path('/repo'))
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'along', '--repo', '/repo', 'dev']) == 0
    assert start.call_args.args[0].harness == 'bro'

  def test_project_summon_harness_reaches_the_spec(self, monkeypatch):
    monkeypatch.setattr(
      ride_cli,
      'project_config',
      lambda _repo: SimpleNamespace(
        default_bro='bro-dev',
        harness='claude',
        summon_harness='claude',
        summon_depth=configs.DEFAULT_SUMMON_DEPTH,
      ),
    )
    monkeypatch.setattr(ride_cli, 'project_root', lambda _path: Path('/repo'))
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'along', '--repo', '/repo', 'dev']) == 0
    assert start.call_args.args[0].summon_harness == 'claude'


class TestLifecycle:
  def test_outer_command_migrates_legacy_runtime_state_first(self):
    with (
      patch('ride.cli.migrate_runtime_state') as migrate,
      patch('ride.cli.list_workspaces', return_value=0),
    ):
      assert ride_cli.main(['ride', 'list']) == 0
    migrate.assert_called_once_with()

  def test_given_runtime_reexec_precedes_migration(self, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
      ride_cli,
      'reexec_from_runtime',
      lambda _runtime, _argv: calls.append('reexec'),
    )
    monkeypatch.setattr(ride_cli, 'migrate_runtime_state', lambda: calls.append('migrate'))
    monkeypatch.setattr(
      ride_cli, 'start_session', lambda *_args, **_kwargs: calls.append('start') or 0
    )
    assert (
      ride_cli.main(['ride', 'solo', '--unboxed', '--runtime-bundle', str(tmp_path), 'dev', 'work'])
      == 0
    )
    assert calls == ['reexec', 'migrate', 'start']

  def test_resume_runtime_reexec_precedes_migration(self, monkeypatch):
    record = workspace_dir('recorded') / 'resume.json'
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({'runtime_bundle': '/runtime'}))
    calls = []
    monkeypatch.setattr(
      ride_cli,
      'reexec_from_runtime',
      lambda _runtime, _argv: calls.append('reexec'),
    )
    monkeypatch.setattr(ride_cli, 'migrate_runtime_state', lambda: calls.append('migrate'))
    monkeypatch.setattr(
      ride_cli,
      'resume_session',
      lambda *_args, **_kwargs: calls.append('resume') or 0,
    )
    assert ride_cli.main(['ride', 'resume', 'recorded']) == 0
    assert calls == ['reexec', 'migrate', 'resume']

  def test_mode_parser_has_no_in_place_entry(self, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'solo', '--in-place', 'dev', 'prompt'])
    assert 'unrecognized arguments' in capsys.readouterr().err

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
    record = pending_summon.PendingSummon(
      token='TOK-1',
      runtime='/runtime',
      port=7321,
      channel_token='tk',
      target='dev',
      prompt='work this out with the user',
      parent_workspace=str(tmp_path / 'parent'),
      may_summon=('bro',),
      permits=('party.start.boxed',),
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
    assert spec.runtime_bundle == '/runtime'
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

  def test_summoned_refuses_permit_overrides(self, pending, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'along', '--summoned', 'TOK-1', '--grant', ':party.join', 'dev'])
    assert 'permits were fixed by the summon request' in capsys.readouterr().err

  def test_summoned_validates_the_bro_against_the_record(self, pending, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'along', '--summoned', 'TOK-1', 'bro'])
    assert "names bro 'dev', not 'bro'" in capsys.readouterr().err

  def test_unknown_token_is_a_cli_error(self, pending, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'along', '--summoned', 'TOK-9', 'dev'])
    assert 'no pending manual summon for token' in capsys.readouterr().err

  def test_runtime_is_read_and_reexeced_before_the_full_record(self, pending, monkeypatch):
    path = pending_summon._path(pending.token)
    data = json.loads(path.read_text())
    data['future_field'] = {'new': 'shape'}
    path.write_text(json.dumps(data))
    calls = []

    def reexec(runtime, argv):
      calls.append((runtime, argv))
      raise RuntimeError('reexec stopped')

    monkeypatch.setattr(ride_cli, 'reexec_from_runtime', reexec)
    with pytest.raises(SystemExit):
      ride_cli.main(['old-ride', 'along', '--summoned', 'TOK-1', 'dev'])
    assert calls == [('/runtime', ['old-ride', 'along', '--summoned', 'TOK-1', 'dev'])]

  def test_solo_has_no_summoned_flag(self, pending, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'solo', '--summoned', 'TOK-1', 'dev', 'p'])
    capsys.readouterr()
