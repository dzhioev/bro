import json
from dataclasses import replace
from typing import Optional
from unittest.mock import patch

import pytest

import bro.launch.scope
import bro.launch.spawn
import bro.launch.summon_control
import bro.summon
import bro.workspace.project as workspace_project
import ride.claude.harness as claude_harness
import ride.claude.session as claude_session
import ride.session as ride_session
from bro.base import credentials
from bro.launch.scope import ScopedSecrets
from bro.workspace.metadata import WorkspaceKind
from bro.workspace.model import Workspace
from ride.claude.harness import ClaudeOptions
from ride.flags import DEFAULT_HOLD


def _spec(
  *,
  name: str = 'w',
  host: bool = False,
  drop: bool = False,
  hold: str = DEFAULT_HOLD,
  grant: Optional[list[str]] = None,
  revoke: Optional[list[str]] = None,
  llm: Optional[str] = None,
  solo: bool = False,
  resume: bool = False,
  into: Optional[str] = None,
  bro: Optional[str] = None,
  raw: bool = False,
  prompt: Optional[str] = None,
  claude_args: Optional[list[str]] = None,
) -> ride_session.SessionSpec:
  from ride.claude.harness import ClaudeOptions

  resolved_bro = bro if bro is not None else 'bro-dev'
  return ride_session.SessionSpec(
    name=name,
    harness='claude',
    workspace_pinned=True,
    host=host,
    drop=drop,
    hold=hold,
    grant=grant if grant is not None else [],
    revoke=revoke if revoke is not None else [],
    llm=llm,
    resolved_llm=claude_harness.CLAUDE.resolve_llm(llm, resolved_bro).dump(),
    solo=solo,
    resume=resume,
    into=into,
    bro=resolved_bro,
    prompt=prompt,
    harness_options=ClaudeOptions(
      raw=raw, arguments=claude_args if claude_args is not None else []
    ).dump(),
  )


def _resume(
  name: str = 'w',
  *,
  grant: Optional[list[str]] = None,
  revoke: Optional[list[str]] = None,
) -> int:
  return ride_session.resume_session(
    name,
    grant=grant if grant is not None else [],
    revoke=revoke if revoke is not None else [],
  )


def _launch_scope(**overrides) -> ride_session.ScopedLaunch:
  base = {
    'scoped': ScopedSecrets({'github'}, set(), True),
    'may_summon': set(),
    'store': {},
  }
  base.update(overrides)
  return ride_session.ScopedLaunch(**base)


def _workspace(tmp_path, kind: WorkspaceKind = WorkspaceKind.CONTAINER) -> Workspace:
  return Workspace.ensure('w', tmp_path, kind)


@pytest.fixture(autouse=True)
def configured_project(monkeypatch, tmp_path):
  # every launch path takes the workspace session lock and records a resume spec
  # under the project root; keep both off the real repo
  monkeypatch.setattr(ride_session, 'project_root', lambda: tmp_path)
  # the suite itself may run inside a container; without this every launch would
  # hit the nested-launch refusal
  monkeypatch.setattr(ride_session, 'in_container', lambda: False)


class _ContainerHarness:
  """patches for driving start_session through the container path without docker,
  bro imports, or git side effects."""

  def __init__(
    self, secrets: Optional[set[str]] = None, optional_secrets: Optional[set[str]] = None
  ):
    self.secrets = secrets if secrets is not None else {'github'}
    self.optional_secrets = optional_secrets if optional_secrets is not None else set()

  def __enter__(self):
    self._patches = [
      patch.dict('os.environ', {}, clear=False),
      patch('ride.claude.session.find_container_id', return_value=None),
      patch('ride.claude.session.run_in_container', return_value=0),
      patch(
        'ride.session.scoped_secrets',
        return_value=ScopedSecrets(set(self.secrets), set(self.optional_secrets), True),
      ),
      patch('ride.claude.harness.credentials.try_get', return_value='tok'),
      patch('bro.launch.scope.credentials.build_scoped_store', return_value={}),
      patch('ride.claude.session.container_claude_state', return_value=([], {})),
      patch('bro.workspace.model.ContainerWorkspace.remove'),
      patch('ride.session._print_resume_hint'),
      # keep the bro-registry import out; threading is asserted per-test
      patch('bro.launch.summon_control.summon_allow_list', return_value=set()),
      patch('ride.claude.harness._load_anthropic_key', return_value={'api_key': 'k'}),
      patch('ride.claude.session.local_trails_mounts', return_value=()),
    ]
    entered = [p.__enter__() for p in self._patches]
    self.env = entered[0]
    self.env.pop('RIDE_BRO', None)
    self.run_in_container = entered[2]
    self.try_get = entered[4]
    self.build_scoped_store = entered[5]
    self.container_claude_state = entered[6]
    self.remove_workspace = entered[7]
    self.summon_allow_list = entered[9]
    self.local_trails_mounts = entered[11]
    return self

  def __exit__(self, *exception):
    for p in reversed(self._patches):
      p.__exit__(*exception)
    return False


class TestNestedLaunch:
  def test_ride_refuses_with_process_and_summon_remedies(self, monkeypatch, caplog):
    monkeypatch.setattr(ride_session, 'in_container', lambda: True)
    spec = replace(_spec(), workspace_pinned=False)
    assert ride_session.start_session(spec) == 1
    assert '`summon`' in caplog.text
    assert '`bro run|chat`' in caplog.text

  def test_an_unmarked_container_is_refused_too(self, monkeypatch, caplog):
    monkeypatch.delenv('RIDE_IN_CONTAINER', raising=False)
    monkeypatch.setattr(ride_session, 'in_container', lambda: True)
    assert ride_session.start_session(_spec()) == 1
    assert 'cannot start inside a container' in caplog.text


class TestGrantRevoke:
  def test_start_session_applies_grant_and_revoke(self):
    with _ContainerHarness(secrets={'notion', 'trails', 'github'}) as h:
      rc = ride_session.start_session(_spec(drop=True, grant=['gmail_creds'], revoke=['notion']))
    assert rc == 0
    launch = h.run_in_container.call_args.args[0]
    assert 'gmail_creds' in launch.secrets
    assert 'notion' not in launch.secrets

  def test_start_session_grant_replaces_a_credential_instance(self):
    with _ContainerHarness(secrets={'brog', 'github'}) as harness:
      rc = ride_session.start_session(_spec(drop=True, grant=['brog+github']))
    assert rc == 0
    launch = harness.run_in_container.call_args.args[0]
    assert launch.secrets == {'brog+github', 'github'}

  def test_start_session_can_revoke_an_optional_secret(self):
    with _ContainerHarness(optional_secrets={'openai'}) as harness:
      rc = ride_session.start_session(_spec(drop=True, revoke=['openai']))
    assert rc == 0
    launch = harness.run_in_container.call_args.args[0]
    assert launch.optional_secrets == set()

  def test_missing_secret_fails_cleanly_before_container_launch(self, caplog):
    with _ContainerHarness() as harness:
      harness.build_scoped_store.side_effect = credentials.SecretNotFound('github')
      rc = ride_session.start_session(_spec(drop=True))
    assert rc == 1
    assert harness.run_in_container.call_count == 0
    assert 'github' in caplog.text

  def test_missing_setup_token_has_actionable_container_error(self, caplog):
    with _ContainerHarness() as harness:
      harness.try_get.return_value = None
      rc = ride_session.start_session(_spec(drop=True))
    assert rc == 1
    assert harness.run_in_container.call_count == 0
    assert 'mint one with `claude setup-token`' in caplog.text

  def test_missing_setup_token_does_not_gate_a_raw_launch(self):
    with _ContainerHarness() as harness:
      harness.try_get.return_value = None
      rc = ride_session.start_session(_spec(drop=True, raw=True))
    assert rc == 0

  def test_start_session_grant_already_present_returns_1(self):
    with _ContainerHarness() as h:
      rc = ride_session.start_session(_spec(drop=True, grant=['github']))
    assert rc == 1
    assert h.run_in_container.call_count == 0

  def test_start_session_injects_the_llm_recipe_into_the_container_command(self):
    with _ContainerHarness() as h:
      rc = ride_session.start_session(_spec(drop=True, llm='::xhigh'))
    assert rc == 0
    command = h.run_in_container.call_args.args[0].command
    assert command[command.index('--llm') + 1] == '::xhigh'


class TestSummonAllowList:
  def test_container_session_threads_the_allow_list(self):
    with _ContainerHarness() as h:
      h.summon_allow_list.return_value = {'dev'}
      rc = ride_session.start_session(_spec(drop=True, grant=['@dev']))
    assert rc == 0
    assert h.summon_allow_list.call_args == (
      ('bro-dev',),
      {'grant': ['dev'], 'revoke': []},
    )
    assert h.run_in_container.call_args.kwargs['may_summon'] == {'dev'}

  def test_container_session_keys_identity_on_the_bro(self):
    with _ContainerHarness() as h:
      rc = ride_session.start_session(_spec(drop=True, bro='dev'))
    assert rc == 0
    assert h.summon_allow_list.call_args[0] == ('dev',)

  def test_bad_summon_flag_fails_the_launch(self):
    with _ContainerHarness() as h:
      h.summon_allow_list.side_effect = ValueError('unknown summon target(s): devoop')
      rc = ride_session.start_session(_spec(drop=True, grant=['@devoop']))
    assert rc == 1
    assert h.run_in_container.call_count == 0


class TestContainerCommand:
  def test_command_is_the_in_place_invocation(self):
    # the docker command is the same in-place runner host mode spawns; the
    # argv/MCP/spell-delivery work happens inside the container, next to claude
    with _ContainerHarness() as h:
      rc = ride_session.start_session(_spec(drop=True, bro='dev', llm='::xhigh+fast', prompt='go'))
    assert rc == 0
    command = h.run_in_container.call_args.args[0].command
    assert command == [
      'ride', 'along', '--in-place', '--workspace', 'w', '--harness', 'claude',
      '--llm', '::xhigh+fast', 'dev', 'go',
    ]  # fmt: skip

  def test_bro_carried_in_command_and_stamped_into_the_container_env(self):
    with _ContainerHarness() as h:
      rc = ride_session.start_session(_spec(drop=True, bro='dev'))
    assert rc == 0
    command = h.run_in_container.call_args.args[0].command
    assert command == [
      'ride',
      'along',
      '--in-place',
      '--workspace',
      'w',
      '--harness',
      'claude',
      'dev',
    ]
    # RIDE_BRO themes the whole container (ride exec shells), set explicitly in the
    # container env — never forwarded from the launcher's environment
    launch = h.run_in_container.call_args.args[0]
    assert launch.env['RIDE_BRO'] == 'dev'

  def test_local_trails_data_is_combined_with_claude_launch_data(self):
    with _ContainerHarness(secrets={'github', 'trails'}) as harness:
      harness.container_claude_state.return_value = (
        ['/host/claude:/home/ride/.claude'],
        {'CLAUDE_CONFIG_DIR': '/home/ride/.claude'},
      )
      harness.local_trails_mounts.return_value = ('/host/trails:/var/ride/trails',)
      result = ride_session.start_session(_spec(drop=True))

    assert result == 0
    harness.local_trails_mounts.assert_called_once_with(
      ScopedSecrets({'github', 'trails'}, set(), True)
    )
    launch = harness.run_in_container.call_args.args[0]
    assert launch.extra_mounts == (
      '/host/claude:/home/ride/.claude',
      '/host/trails:/var/ride/trails',
    )

  def test_raw_carried_in_the_container_command(self):
    with _ContainerHarness() as h:
      rc = ride_session.start_session(_spec(drop=True, bro='dev', raw=True))
    assert rc == 0
    command = h.run_in_container.call_args.args[0].command
    assert command == [
      'ride',
      'along',
      '--in-place',
      '--workspace',
      'w',
      '--harness',
      'claude',
      '--raw',
      'dev',
    ]

  def test_solo_launch_has_no_container_tty(self):
    spec = replace(
      _spec(solo=True, hold='unattended', prompt='go'),
      workspace_pinned=False,
    )
    with _ContainerHarness() as harness:
      assert ride_session.start_session(spec) == 0
    launch = harness.run_in_container.call_args.args[0]
    assert not launch.tty
    assert launch.command == [
      'ride', 'solo', '--in-place', '--workspace', 'w', '--harness', 'claude',
      'bro-dev', 'go',
    ]  # fmt: skip

  def test_ride_session_stamps_the_default_bro_as_ride_bro(self):
    with _ContainerHarness() as h:
      rc = ride_session.start_session(_spec(drop=True))
    assert rc == 0
    launch = h.run_in_container.call_args.args[0]
    assert launch.env['RIDE_BRO'] == 'bro-dev'

  def test_default_base_is_left_to_the_entrypoint_head_fallback(self):
    # no RIDE_BASE_REF by default: the clone bases on HEAD — the host checkout as
    # cloned — with no network touched on the way
    with _ContainerHarness() as h:
      rc = ride_session.start_session(_spec(drop=True))
    assert rc == 0
    launch = h.run_in_container.call_args.args[0]
    assert 'RIDE_BASE_REF' not in launch.env

  def test_into_threads_the_resolved_base_into_the_container_env(self):
    with _ContainerHarness() as h:
      with patch('ride.session.resolve_ref', return_value='intosha') as resolve:
        rc = ride_session.start_session(_spec(drop=True, into='feature'))
    assert rc == 0
    assert resolve.call_args[0][1] == 'feature'
    launch = h.run_in_container.call_args.args[0]
    assert launch.env['RIDE_BASE_REF'] == 'intosha'

  def test_unresolvable_into_fails_launch(self):
    with _ContainerHarness() as h:
      with patch('ride.session.resolve_ref', return_value=None):
        rc = ride_session.start_session(_spec(drop=True, into='nope'))
    assert rc == 1
    assert h.run_in_container.call_count == 0

  def test_resume_guard_fails_fast_without_a_session(self, tmp_path):
    with _ContainerHarness() as h:
      with patch('ride.claude.session.workspace_projects_dir') as projects:
        projects.return_value = tmp_path / 'projects'
        rc = ride_session.start_session(_spec(resume=True))
    assert rc == 1
    assert h.run_in_container.call_count == 0

  def test_resume_carried_as_bare_flag_the_runner_resolves(self, tmp_path):
    projects_dir = tmp_path / 'projects'
    projects_dir.mkdir()
    (projects_dir / 'abc.jsonl').write_text('{}')
    with _ContainerHarness() as h:
      with patch('ride.claude.session.workspace_projects_dir') as projects:
        projects.return_value = projects_dir
        rc = ride_session.start_session(_spec(resume=True))
    assert rc == 0
    command = h.run_in_container.call_args.args[0].command
    assert command == [
      'ride',
      'along',
      '--in-place',
      '--workspace',
      'w',
      '--harness',
      'claude',
      '--resume',
      'bro-dev',
    ]


class TestContainerDrop:
  def test_drop_removes_the_workspace_on_clean_exit(self):
    with _ContainerHarness() as h:
      rc = ride_session.start_session(_spec(drop=True))
    assert rc == 0
    assert h.remove_workspace.call_count == 1

  def test_drop_keeps_the_workspace_when_the_session_failed(self):
    with _ContainerHarness() as h:
      h.run_in_container.return_value = 3
      rc = ride_session.start_session(_spec(drop=True))
    assert rc == 3
    assert h.remove_workspace.call_count == 0


class TestCommandArgv:
  def test_create_command_includes_drop_into_and_claude_args(self):
    parts = _spec(
      hold='attended',
      drop=True,
      llm='::xhigh+fast',
      bro='dev',
      grant=['gmail_creds', '@bro'],
      revoke=['notion'],
      into='feature',
      claude_args=['--foo'],
    ).to_command_argv()
    assert parts == [
      'ride', 'along', '--drop', '--llm', '::xhigh+fast', '--harness', 'claude',
      '--workspace', 'w', '--grant', 'gmail_creds', '--grant', '@bro',
      '--revoke', 'notion', '--into', 'feature', 'dev', '--', '--foo',
    ]  # fmt: skip

  def test_host_session_carries_the_host_flag(self):
    parts = _spec(host=True, hold='detached').to_command_argv()
    assert parts == [
      'ride',
      'along',
      '--host',
      '--hold',
      'detached',
      '--harness',
      'claude',
      '--workspace',
      'w',
      'bro-dev',
    ]

  def test_default_hold_is_elided(self):
    # the parser's default hold stays implicit in the reconstructed command
    assert _spec().to_command_argv() == [
      'ride',
      'along',
      '--hold',
      'guided',
      '--harness',
      'claude',
      '--workspace',
      'w',
      'bro-dev',
    ]

  def test_solo_command_uses_keep_only_for_a_retained_automatic_workspace(self):
    automatic = replace(
      _spec(solo=True, drop=True, hold='unattended', prompt='go'),
      workspace_pinned=False,
    )
    assert automatic.to_command_argv() == [
      'ride',
      'solo',
      '--harness',
      'claude',
      'bro-dev',
      'go',
    ]
    assert replace(automatic, drop=False).to_command_argv() == [
      'ride',
      'solo',
      '--keep',
      '--harness',
      'claude',
      'bro-dev',
      'go',
    ]

  def test_a_resume_is_its_own_command(self):
    # the recorded spec carries the flags, so the name is the whole command
    assert _spec(hold='attended', bro='dev').resume_variant().to_command_argv() == [
      'ride',
      'resume',
      'w',
    ]


class TestScopeOverrides:
  def test_values_join_the_recorded_lists(self):
    updated = _spec(grant=['brog+github'], revoke=['openai']).with_scope_overrides(
      grant=['@bro-dev'], revoke=['brave']
    )
    assert updated.grant == ['brog+github', '@bro-dev']
    assert updated.revoke == ['openai', 'brave']

  def test_an_override_cancels_the_opposite_recorded_one(self):
    # granting back a revoked credential leaves the computed scope's own selection
    updated = _spec(grant=['@bro-dev'], revoke=['openai']).with_scope_overrides(
      grant=['openai'], revoke=['@bro-dev']
    )
    assert (updated.grant, updated.revoke) == ([], [])

  def test_restating_a_recorded_override_raises(self):
    with pytest.raises(ValueError, match='already in the recorded --grant: @bro-dev'):
      _spec(grant=['@bro-dev']).with_scope_overrides(grant=['@bro-dev'], revoke=[])
    with pytest.raises(ValueError, match='already in the recorded --revoke: openai'):
      _spec(revoke=['openai']).with_scope_overrides(grant=[], revoke=['openai'])

  def test_a_contradicting_pair_survives_for_the_scope_layer(self):
    # nothing recorded to cancel, so both land and the launch preflight rejects them
    updated = _spec().with_scope_overrides(grant=['openai'], revoke=['openai'])
    assert (updated.grant, updated.revoke) == (['openai'], ['openai'])


class TestResumeSpecRecord:
  def test_recorded_spec_clears_create_only_inputs_and_round_trips(self, tmp_path):
    spec = _spec(
      hold='attended',
      drop=True,
      llm='::xhigh',
      bro='dev',
      grant=['gmail_creds'],
      into='feature',
      prompt='do it',
      claude_args=['--foo'],
    )
    workspace = _workspace(tmp_path)
    ride_session.record_resume_spec(workspace, spec)
    loaded = ride_session.load_resume_spec(workspace)
    assert loaded == spec.resume_variant()
    assert loaded is not None and loaded.resume and not loaded.drop
    assert (
      loaded.into is None
      and loaded.prompt is None
      and ClaudeOptions.load(loaded.harness_options).arguments == []
    )
    # the forwarded flags survive, so the resumed session runs as it was launched
    assert (loaded.hold, loaded.llm, loaded.bro, loaded.grant) == (
      'attended',
      '::xhigh',
      'dev',
      ['gmail_creds'],
    )

  def test_solo_resume_becomes_an_along_session_with_its_default_hold(self, tmp_path):
    solo = replace(
      _spec(solo=True, hold='unattended', prompt='go'),
      workspace_pinned=False,
    )
    workspace = _workspace(tmp_path)
    ride_session.record_resume_spec(workspace, solo)
    loaded = ride_session.load_resume_spec(workspace)
    assert loaded is not None
    assert not loaded.solo
    assert loaded.hold == 'attended'
    assert loaded.to_command_argv() == ['ride', 'resume', 'w']

  def test_recording_a_resume_is_a_fixpoint(self, tmp_path):
    workspace = _workspace(tmp_path)
    ride_session.record_resume_spec(workspace, _spec(bro='dev'))
    first = ride_session.load_resume_spec(workspace)
    assert first is not None
    ride_session.record_resume_spec(workspace, first)
    assert ride_session.load_resume_spec(workspace) == first

  def test_missing_record_reads_as_none(self, tmp_path):
    assert ride_session.load_resume_spec(_workspace(tmp_path)) is None

  def test_record_from_an_incompatible_ride_reads_as_none(self, tmp_path, caplog):
    workspace = _workspace(tmp_path)
    workspace.resume_file.write_text(json.dumps({'name': 'w', 'gone': True}))
    assert ride_session.load_resume_spec(workspace) is None
    assert 'unreadable resume spec' in caplog.text

  def test_start_session_records_before_launching(self, tmp_path):
    with _ContainerHarness():
      recorded: list = []
      with patch(
        'ride.claude.session._container_session',
        side_effect=lambda *args: recorded.append(ride_session.load_resume_spec(args[1])) or 0,
      ):
        assert ride_session.start_session(_spec(drop=True, bro='dev')) == 0
    assert recorded[0] == _spec(drop=True, bro='dev').resume_variant()


class TestResumeSession:
  def test_relaunches_the_recorded_spec(self, tmp_path):
    ride_session.record_resume_spec(_workspace(tmp_path), _spec(bro='dev', hold='attended'))
    with patch('ride.session.start_session', return_value=0) as start:
      assert _resume() == 0
    assert start.call_args[0][0] == _spec(bro='dev', hold='attended').resume_variant()

  def test_scope_overrides_reach_the_relaunch(self, tmp_path):
    ride_session.record_resume_spec(_workspace(tmp_path), _spec(grant=['brog+github']))
    with patch('ride.session.start_session', return_value=0) as start:
      assert _resume(grant=['@bro-dev']) == 0
    assert start.call_args[0][0].grant == ['brog+github', '@bro-dev']

  def test_a_no_op_override_errors(self, tmp_path, caplog):
    ride_session.record_resume_spec(_workspace(tmp_path), _spec(grant=['@bro-dev']))
    with patch('ride.session.start_session') as start:
      assert _resume(grant=['@bro-dev']) == 1
    assert start.call_count == 0
    assert 'already in the recorded --grant: @bro-dev' in caplog.text

  def test_unknown_workspace_errors(self, caplog):
    with patch('ride.session.start_session') as start:
      assert _resume('gone') == 1
    assert start.call_count == 0
    assert 'workspace not found: gone' in caplog.text

  def test_workspace_without_a_record_errors(self, tmp_path, caplog):
    _workspace(tmp_path)
    with patch('ride.session.start_session') as start:
      assert _resume() == 1
    assert start.call_count == 0
    assert 'no session recorded for w' in caplog.text


class TestResumeHint:
  HINT = 'Resume this session with:\n  ride resume w\n'

  def test_prints_the_resume_command_and_nothing_else(self, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(claude_harness.CLAUDE, 'session_exists', lambda workspace: True)
    monkeypatch.setattr('sys.stdout.isatty', lambda: True)
    ride_session._print_resume_hint(_spec(solo=True, prompt='go'), _workspace(tmp_path))
    assert capsys.readouterr().out == self.HINT

  def test_silent_without_a_session_jsonl(self, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(claude_harness.CLAUDE, 'session_exists', lambda workspace: False)
    monkeypatch.setattr('sys.stdout.isatty', lambda: True)
    ride_session._print_resume_hint(_spec(host=True), _workspace(tmp_path, WorkspaceKind.WORKTREE))
    assert capsys.readouterr().out == ''

  def test_silent_when_stdout_is_redirected(self, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(claude_harness.CLAUDE, 'session_exists', lambda workspace: True)
    monkeypatch.setattr('sys.stdout.isatty', lambda: False)
    ride_session._print_resume_hint(_spec(), _workspace(tmp_path))
    assert capsys.readouterr().out == ''


class TestInPlaceArgv:
  def test_drops_machinery_flags_and_carries_the_rest(self):
    parts = _spec(
      host=True,
      hold='attended',
      drop=True,
      llm='::xhigh+fast',
      bro='dev',
      grant=['gmail_creds'],
      revoke=['notion'],
      into='feature',
      prompt='do it',
      claude_args=['--foo'],
    ).inner_command()
    assert parts == [
      'ride', 'along', '--in-place', '--workspace', 'w', '--harness', 'claude',
      '--hold', 'attended', '--llm', '::xhigh+fast', 'dev', 'do it', '--', '--foo',
    ]  # fmt: skip

  def test_resume_and_raw_carried(self):
    parts = _spec(resume=True, bro='dev', raw=True).inner_command()
    assert parts == [
      'ride',
      'along',
      '--in-place',
      '--workspace',
      'w',
      '--harness',
      'claude',
      '--resume',
      '--raw',
      'dev',
    ]


class TestSessionBro:
  def test_bro_names_the_identity(self):
    assert _spec(bro='dev').session_bro == 'dev'

  def test_session_uses_the_project_default(self):
    assert _spec().session_bro == 'bro-dev'

  def test_raw_selects_the_harness_scope_recipe(self):
    assert claude_harness.CLAUDE.scope_recipe(_spec(raw=True)).name == 'claude-raw'
    assert claude_harness.CLAUDE.scope_recipe(_spec()).name == 'claude-full'


class TestConcurrentSessionGuard:
  """the one-session-per-workspace lock, taken by start_session before either mode
  prepares anything."""

  @pytest.fixture(autouse=True)
  def launch_preflights(self, monkeypatch):
    # start_session runs the auth and scope preflights ahead of the guards these
    # tests drive; without stubs they read the machine's own credential store
    monkeypatch.setattr(credentials, 'try_get', lambda name: 'tok')
    monkeypatch.setattr(
      ride_session, 'scoped_secrets', lambda *_a, **_k: ScopedSecrets(set(), set(), True)
    )
    monkeypatch.setattr(
      bro.launch.scope.credentials, 'build_scoped_store', lambda names, optional=(): {}
    )
    monkeypatch.setattr(bro.launch.summon_control, 'summon_allow_list', lambda *_a, **_k: set())

  def test_second_launch_is_refused_while_the_lock_is_held(self, tmp_path, caplog):
    workspace = _workspace(tmp_path)
    with workspace.hold_session_lock():
      with patch('ride.claude.session._container_session') as launch:
        assert ride_session.start_session(_spec()) == 1
    assert launch.call_count == 0
    assert 'session already active on workspace' in caplog.text

  def test_the_lock_releases_with_the_session(self, tmp_path):
    with patch('ride.claude.session._container_session', return_value=0):
      assert ride_session.start_session(_spec()) == 0
    assert not _workspace(tmp_path).is_active(set())

  def test_a_launch_naming_a_workspace_of_the_other_kind_is_refused(self, tmp_path, caplog):
    _workspace(tmp_path, WorkspaceKind.WORKTREE)
    with patch('ride.claude.session._container_session') as launch:
      assert ride_session.start_session(_spec()) == 1
    assert launch.call_count == 0
    assert 'is a worktree workspace, not container' in caplog.text

  def test_container_refuses_an_orphaned_running_container(self, monkeypatch, tmp_path, caplog):
    # a launcher killed outright releases the lock but leaves its container bound
    monkeypatch.setattr(claude_session, 'find_container_id', lambda path: 'abc123')

    def boom(*_a, **_k):
      raise AssertionError('must not launch a second container session')

    monkeypatch.setattr(claude_session, 'run_in_container', boom)
    assert ride_session.start_session(_spec()) == 1
    assert 'session already active in the container' in caplog.text


class TestHostSession:
  def _fake_workspace(self, monkeypatch, tmp_path, *, has_session: bool):
    projects = tmp_path / 'projects'
    projects.mkdir()
    if has_session:
      (projects / 'abc.jsonl').write_text('{}')
    workspace = _workspace(tmp_path, WorkspaceKind.WORKTREE)
    monkeypatch.setattr(claude_session, 'workspace_projects_dir', lambda ws: projects)
    monkeypatch.setattr(type(workspace), 'remove', lambda self: None)
    return workspace, workspace.tree

  def _prepare_launch(self, monkeypatch, tmp_path):
    workspace, worktree = self._fake_workspace(monkeypatch, tmp_path, has_session=False)
    ride_binary = worktree / '.venv' / 'bin' / 'ride'
    ride_binary.parent.mkdir(parents=True)
    ride_binary.write_text('')
    monkeypatch.setattr(workspace_project, 'project_root', lambda: tmp_path)
    monkeypatch.setattr(claude_session, 'project_root', lambda: tmp_path)
    monkeypatch.setattr(claude_session.os, 'chdir', lambda p: None)
    monkeypatch.setattr(claude_session, 'ensure_host_worktree', lambda *_a: True)
    monkeypatch.setattr(claude_session, 'provision_host_worktree', lambda *_a: True)
    # keep the launch tests off the real credential store; the auth-transform
    # test overrides this with its own fake
    monkeypatch.setattr(claude_session, '_apply_claude_auth', lambda env, **_k: None)
    monkeypatch.setattr(credentials, 'try_get', lambda name: 'tok')
    monkeypatch.setattr(
      claude_session,
      '_provision_host_claude_dir',
      lambda ws, wt, project: tmp_path / 'claude-config',
    )
    monkeypatch.setattr(
      ride_session, 'scoped_secrets', lambda *_a, **_k: ScopedSecrets({'github'}, set(), True)
    )
    monkeypatch.setattr(
      bro.launch.scope.credentials, 'build_scoped_store', lambda names, optional=(): {}
    )
    monkeypatch.setattr(
      claude_session,
      'materialize_scoped_store',
      lambda store, directory: directory / 'credentials.json',
    )
    monkeypatch.setattr(bro.launch.summon_control, 'summon_allow_list', lambda *_a, **_k: set())
    return workspace, ride_binary, worktree

  def test_broker_supervises_the_worktrees_own_in_place_runner(self, monkeypatch, tmp_path):
    workspace, ride_binary, worktree = self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(claude_session, 'broker_enabled', lambda: True)
    monkeypatch.setattr(bro.launch.summon_control, 'summon_allow_list', lambda *_a, **_k: {'dev'})
    roots: list = []

    def fake_root(
      root_workspace,
      command,
      env,
      may_summon,
      credential_scope,
      *,
      interactive,
      trail_pointer,
    ):
      roots.append(
        {
          'workspace': root_workspace,
          'command': command,
          'env': env,
          'may_summon': may_summon,
          'credential_scope': credential_scope,
          'interactive': interactive,
        }
      )
      return 5

    monkeypatch.setattr(claude_session, 'run_host_process_via_broker', fake_root)
    spec = _spec(host=True, hold='attended', llm='::xhigh', prompt='go', claude_args=['--foo'])
    scope = _launch_scope(may_summon={'dev'})
    assert claude_session._host_session(spec, workspace, None, scope) == 5
    assert roots[0]['workspace'] is workspace
    assert roots[0]['command'] == [
      str(ride_binary), 'along', '--in-place', '--workspace', 'w', '--harness', 'claude',
      '--hold', 'attended', '--llm', '::xhigh', 'bro-dev', 'go', '--', '--foo',
    ]  # fmt: skip
    assert roots[0]['env']['VIRTUAL_ENV'] == str(worktree / '.venv')
    # the host root gets the session's summon allow-list like container mode
    assert roots[0]['may_summon'] == {'dev'}
    assert roots[0]['interactive']

  def test_host_runner_env_carries_the_summon_facts(self, monkeypatch, tmp_path):
    workspace, _, _ = self._prepare_launch(monkeypatch, tmp_path)
    captured: dict = {}

    def fake_run_root(launch, **_kwargs):
      captured['launch'] = launch
      captured['env'] = launch.env
      return 0

    monkeypatch.setattr(bro.launch.spawn, 'run_root_via_broker', fake_run_root)
    assert (
      claude_session.run_host_process_via_broker(
        workspace, ['ride'], {}, {'dev', 'bro'}, set(), interactive=False
      )
      == 0
    )
    assert captured['env'][bro.summon.MAY_SUMMON_ENV] == 'bro,dev'
    assert captured['env'][bro.launch.summon_control.STATUS_ENV].endswith('w.status.json')
    assert not captured['launch'].interactive

  def test_bad_summon_flag_fails_before_the_workspace_is_recorded(self, monkeypatch, tmp_path):
    self._prepare_launch(monkeypatch, tmp_path)

    def bad_allow_list(*_a, **_k):
      raise ValueError('unknown summon target(s): devoop')

    monkeypatch.setattr(bro.launch.summon_control, 'summon_allow_list', bad_allow_list)

    def boom(*_a, **_k):
      raise AssertionError('must not launch when the summon grant is bad')

    monkeypatch.setattr(claude_session, '_host_session', boom)
    assert ride_session.start_session(_spec(name='fresh', host=True, grant=['@devoop'])) == 1
    assert not (tmp_path / 'var' / 'ride' / 'workspaces' / 'fresh').exists()

  def test_direct_spawn_when_broker_disabled(self, monkeypatch, tmp_path):
    workspace, ride_binary, worktree = self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(claude_session, 'broker_enabled', lambda: False)
    runs: list = []

    def fake_run(argv, **kwargs):
      runs.append((argv, kwargs))
      from types import SimpleNamespace

      return SimpleNamespace(returncode=0)

    monkeypatch.setattr(claude_session.subprocess, 'run', fake_run)
    spec = _spec(host=True, hold='attended', llm='::xhigh', prompt='go', claude_args=['--foo'])
    assert claude_session._host_session(spec, workspace, None, _launch_scope()) == 0
    argv, kwargs = runs[0]
    assert argv == [
      str(ride_binary), 'along', '--in-place', '--workspace', 'w', '--harness', 'claude',
      '--hold', 'attended', '--llm', '::xhigh', 'bro-dev', 'go', '--', '--foo',
    ]  # fmt: skip
    assert kwargs['cwd'] == str(worktree)
    assert kwargs['env']['VIRTUAL_ENV'] == str(worktree / '.venv')

  def test_runner_env_gets_the_claude_auth_transform(self, monkeypatch, tmp_path):
    # the outer applies _apply_claude_auth to the runner env it spawns, so a
    # worktree whose own runner predates the transform still inherits the token
    workspace, ride_binary, worktree = self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(claude_session, 'broker_enabled', lambda: False)

    def fake_apply(env, **_kwargs):
      env['CLAUDE_CODE_OAUTH_TOKEN'] = 'applied'

    monkeypatch.setattr(claude_session, '_apply_claude_auth', fake_apply)
    runs: list = []

    def fake_run(argv, **kwargs):
      runs.append((argv, kwargs))
      from types import SimpleNamespace

      return SimpleNamespace(returncode=0)

    monkeypatch.setattr(claude_session.subprocess, 'run', fake_run)
    assert claude_session._host_session(_spec(host=True), workspace, None, _launch_scope()) == 0
    assert runs[0][1]['env']['CLAUDE_CODE_OAUTH_TOKEN'] == 'applied'

  def test_runner_env_points_at_the_private_claude_config_dir(self, monkeypatch, tmp_path):
    # the outer provisions the per-session state dir and exports CLAUDE_CONFIG_DIR
    # itself, so a worktree whose own runner predates the config dir is covered too
    workspace, _, _ = self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(claude_session, 'broker_enabled', lambda: False)
    runs: list = []

    def fake_run(argv, **kwargs):
      runs.append((argv, kwargs))
      from types import SimpleNamespace

      return SimpleNamespace(returncode=0)

    monkeypatch.setattr(claude_session.subprocess, 'run', fake_run)
    assert claude_session._host_session(_spec(host=True), workspace, None, _launch_scope()) == 0
    assert runs[0][1]['env']['CLAUDE_CONFIG_DIR'] == str(tmp_path / 'claude-config')

  def test_missing_claude_code_fails_a_ride_session_launch_before_the_workspace(
    self, monkeypatch, tmp_path
  ):
    self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(credentials, 'try_get', lambda name: None)

    def boom(*_a, **_k):
      raise AssertionError('must not launch without the setup-token')

    monkeypatch.setattr(claude_session, '_host_session', boom)
    assert ride_session.start_session(_spec(name='fresh', host=True)) == 1
    assert not (tmp_path / 'var' / 'ride' / 'workspaces' / 'fresh').exists()

  def test_runner_env_points_at_the_scoped_store_registry(self, monkeypatch, tmp_path):
    from types import SimpleNamespace

    workspace, _, _ = self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(claude_session, 'broker_enabled', lambda: False)
    materialized: dict = {}

    def fake_materialize(store, directory):
      materialized.update(store=store, directory=directory)
      return directory / 'credentials.json'

    monkeypatch.setattr(claude_session, 'materialize_scoped_store', fake_materialize)
    runs: list = []

    def fake_run(argv, **kwargs):
      runs.append((argv, kwargs))
      return SimpleNamespace(returncode=0)

    monkeypatch.setattr(claude_session.subprocess, 'run', fake_run)
    scope = _launch_scope(store={'x.cred': b'v'})
    assert claude_session._host_session(_spec(host=True), workspace, None, scope) == 0
    registry = workspace.path / 'credentials' / 'credentials.json'
    assert runs[0][1]['env']['CREDENTIALS_REGISTRY'] == str(registry)
    assert materialized['store'] == {'x.cred': b'v'}
    assert materialized['directory'] == registry.parent

  def test_grant_and_revoke_shape_and_log_the_hydrated_scope(self, monkeypatch, tmp_path, caplog):
    from types import SimpleNamespace

    self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(claude_session, 'broker_enabled', lambda: False)
    monkeypatch.setattr(
      ride_session,
      'scoped_secrets',
      lambda *_a, **_k: ScopedSecrets({'github', 'notion'}, {'openai'}, True),
    )
    hydrated: dict = {}

    def fake_build(names, optional=()):
      hydrated.update(names=set(names), optional=set(optional))
      return {}

    monkeypatch.setattr(bro.launch.scope.credentials, 'build_scoped_store', fake_build)
    monkeypatch.setattr(
      claude_session.subprocess, 'run', lambda *_a, **_k: SimpleNamespace(returncode=0)
    )
    spec = _spec(host=True, grant=['gmail_creds'], revoke=['notion'])
    with caplog.at_level('INFO'):
      assert ride_session.start_session(spec) == 0
    assert hydrated == {
      'names': {'github', 'gmail_creds'},
      'optional': {'openai'},
    }
    assert 'scoped secrets for w: github, gmail_creds' in caplog.text
    assert 'optional (best-effort) secrets for w: openai' in caplog.text

  def test_unresolvable_secret_fails_before_the_workspace(self, monkeypatch, tmp_path):
    self._prepare_launch(monkeypatch, tmp_path)

    def missing(names, optional=()):
      raise credentials.SecretNotFound('github')

    monkeypatch.setattr(bro.launch.scope.credentials, 'build_scoped_store', missing)

    def boom(*_a, **_k):
      raise AssertionError('must not launch when hydration fails')

    monkeypatch.setattr(claude_session, '_host_session', boom)
    assert ride_session.start_session(_spec(name='fresh', host=True)) == 1
    assert not (tmp_path / 'var' / 'ride' / 'workspaces' / 'fresh').exists()

  def test_missing_inner_ride_fails_before_spawn(self, monkeypatch, tmp_path):
    workspace, worktree = self._fake_workspace(monkeypatch, tmp_path, has_session=False)
    monkeypatch.setattr(workspace_project, 'project_root', lambda: tmp_path)
    monkeypatch.setattr(claude_session, 'project_root', lambda: tmp_path)
    monkeypatch.setattr(claude_session.os, 'chdir', lambda p: None)
    monkeypatch.setattr(claude_session, 'ensure_host_worktree', lambda *_a: True)
    monkeypatch.setattr(claude_session, 'provision_host_worktree', lambda *_a: True)
    monkeypatch.setattr(credentials, 'try_get', lambda name: 'tok')
    monkeypatch.setattr(
      claude_session,
      '_provision_host_claude_dir',
      lambda ws, wt, project: tmp_path / 'claude-config',
    )
    monkeypatch.setattr(
      ride_session, 'scoped_secrets', lambda *_a, **_k: ScopedSecrets(set(), set(), True)
    )
    monkeypatch.setattr(
      bro.launch.scope.credentials, 'build_scoped_store', lambda names, optional=(): {}
    )
    monkeypatch.setattr(bro.launch.summon_control, 'summon_allow_list', lambda *_a, **_k: set())

    def boom(*_a, **_k):
      raise AssertionError('must not spawn without the inner ride')

    monkeypatch.setattr(claude_session.subprocess, 'run', boom)
    assert claude_session._host_session(_spec(host=True), workspace, None, _launch_scope()) == 1

  def test_resume_guard_fails_fast_before_worktree_create(self, monkeypatch, tmp_path):
    workspace, _ = self._fake_workspace(monkeypatch, tmp_path, has_session=False)
    monkeypatch.setattr(workspace_project, 'project_root', lambda: tmp_path)
    monkeypatch.setattr(claude_session, 'project_root', lambda: tmp_path)
    monkeypatch.setattr(claude_session.os, 'chdir', lambda p: None)

    def boom(*_a, **_k):
      raise AssertionError('must not create a worktree for a resume with no session')

    monkeypatch.setattr(claude_session, 'ensure_host_worktree', boom)
    assert (
      claude_session._host_session(_spec(host=True, resume=True), workspace, None, _launch_scope())
      == 1
    )


class TestHostBrokerPingRoundTrip:
  """the host broker channel, live: the runner process a host session spawns gets a
  provisioned channel in its env, and `broker request ping` receives a correlated
  pong over it — real socket, real broker loop, real CLI subprocess. Only the
  worktree git plumbing is stubbed (covered by its own tests); the claude-state
  provisioning runs for real against a fake HOME, so the test neither depends on
  the machine's claude login nor writes into the real ~/.claude."""

  def test_broker_request_ping_from_a_host_session(self, monkeypatch, capfd, socket_dir):
    # socket_dir doubles as the project root: the channel socket lands under the
    # runtime root, whose length the sun_path limit bounds
    root = socket_dir
    monkeypatch.setenv('XDG_DATA_HOME', str(root / 'state'))
    home = root / 'home'
    home.mkdir()
    # the identity fields _seed_claude_json requires from the host ~/.claude.json
    (home / '.claude.json').write_text(json.dumps({'oauthAccount': {'id': 'acct'}, 'userID': 'u'}))
    monkeypatch.setenv('HOME', str(home))
    workspace = Workspace.create('w', root, WorkspaceKind.WORKTREE)
    worktree = workspace.tree
    ride_binary = worktree / '.venv' / 'bin' / 'ride'
    ride_binary.parent.mkdir(parents=True)
    # stands in for the in-place runner: the real `broker` CLI resolves from the
    # ambient venv PATH (retained through _venv_env) and rides BROKER_CHANNEL
    ride_binary.write_text('#!/bin/sh\nexec broker request ping "{}" --timeout 30\n')
    ride_binary.chmod(0o755)

    monkeypatch.setattr(ride_session, 'project_root', lambda: root)
    monkeypatch.setattr(claude_session.os, 'chdir', lambda p: None)
    monkeypatch.setattr(claude_session, 'ensure_host_worktree', lambda *_a: True)
    monkeypatch.setattr(claude_session, 'provision_host_worktree', lambda *_a: True)
    monkeypatch.setattr(bro.launch.summon_control, 'summon_allow_list', lambda *_a, **_k: set())
    monkeypatch.setattr(credentials, 'try_get', lambda name: 'tok')
    monkeypatch.setattr(
      ride_session, 'scoped_secrets', lambda *_a, **_k: ScopedSecrets(set(), set(), True)
    )
    monkeypatch.setattr(
      bro.launch.scope.credentials, 'build_scoped_store', lambda names, optional=(): {}
    )
    monkeypatch.delenv('BROKER_DISABLED', raising=False)
    assert claude_session._host_session(_spec(host=True), workspace, None, _launch_scope()) == 0
    # the CLI printed the correlated reply's wire JSON
    assert '"pong"' in capfd.readouterr().out
    # the channel socket is unlinked once the root exits
    assert list((root / 'var' / 'ride' / 'broker').glob('*.sock')) == []
    # the session claude state landed in the workspace, seeded from its identity
    seeded = workspace.path / 'claude' / '.claude.json'
    assert json.loads(seeded.read_text())['userID'] == 'u'
