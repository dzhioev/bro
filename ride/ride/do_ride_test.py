import dataclasses
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import bro.workspace.session as workspace_session
import ride.do_ride as do_ride
import ride.repo_attachment as repo_attachment
from bro import watches
from bro.llm.providers import format_recipe
from bro.monitor import workspace_session_dir
from bro.summon import RUNTIME_ENV
from ride.harness import get_harness
from ride.repo_attachment import repository_attachment
from ride.session_test import _spec
from ride.workspace.metadata import Isolation
from ride.workspace.model import Workspace


@pytest.fixture(autouse=True)
def isolated_environ():
  with patch.dict(os.environ, {'RIDE_ISOLATION': 'boxed'}, clear=False):
    yield


@pytest.fixture(autouse=True)
def admitted_recipe(monkeypatch):
  monkeypatch.setenv(
    do_ride.RESOLVED_RECIPE_ENV, do_ride.encode_resolved_recipe(_spec().resolved_recipe)
  )


_BUNDLE_HASH = 'ab' * 32


def _command(spec) -> list[str]:
  return do_ride.command(
    spec, () if spec.repo is None else (repository_attachment(Path(spec.repo), spec.name),)
  )


@pytest.fixture(autouse=True)
def _session_dir(monkeypatch, tmp_path):
  monkeypatch.setenv('RIDE_SESSION_DIR', str(tmp_path / 'session'))


def _parsed_run(argv: list[str]) -> do_ride.SessionRun:
  _, run = do_ride._session_run(do_ride._parse(argv))
  return run


class TestCommand:
  def test_carries_only_the_session_shape(self):
    spec = _spec(
      isolation=Isolation.UNBOXED,
      hold='attended',
      drop=True,
      llm='anthropic:claude-opus-5,effort=xhigh,fast_mode=true',
      bro='dev',
      grant=['aws'],
      revoke=['brog'],
      into='feature',
      prompt='do it',
    )
    assert _command(spec) == [
      'do-ride', 'along', '--workspace', 'w', '--harness', 'claude', '--attachment', json.dumps(repository_attachment(Path(str(spec.repo)), 'w').dump()),
      '--hold', 'attended', '--', 'dev', 'do it',
    ]  # fmt: skip

  def test_resume_is_carried(self):
    spec = _spec(resume=True, bro='dev')
    assert _command(spec) == [
      'do-ride', 'along', '--workspace', 'w', '--harness', 'claude', '--resume',
      '--attachment', json.dumps(repository_attachment(Path(str(spec.repo)), 'w').dump()), '--hold', 'attended', '--', 'dev',
    ]  # fmt: skip

  def test_recorded_recipe_crosses_the_launcher_boundary(self, monkeypatch):
    from bro.anthropic.declaration import Recipe

    recorded = {'recipe': Recipe(model='recorded-model').dump(), 'source': 'defaults'}
    spec = dataclasses.replace(_spec(), resolved_recipe=recorded)
    monkeypatch.setenv(do_ride.RESOLVED_RECIPE_ENV, do_ride.encode_resolved_recipe(recorded))
    assert _parsed_run(_command(spec)).resolved_recipe == recorded

  def test_a_run_without_the_admitted_recipe_is_refused(self, monkeypatch):
    monkeypatch.delenv(do_ride.RESOLVED_RECIPE_ENV)
    with pytest.raises(RuntimeError, match=do_ride.RESOLVED_RECIPE_ENV):
      _parsed_run(_command(_spec()))

  def test_the_bro_harness_uses_the_same_executable(self):
    spec = dataclasses.replace(_spec(solo=True, bro='dev', prompt='go'), harness='bro')
    assert _command(spec) == [
      'do-ride', 'solo', '--workspace', 'w', '--harness', 'bro', '--attachment', json.dumps(repository_attachment(Path(str(spec.repo)), 'w').dump()),
      '--hold', 'attended', '--', 'dev', 'go',
    ]  # fmt: skip

  def test_a_prompt_that_reads_as_a_flag_crosses_as_the_prompt(self):
    spec = dataclasses.replace(_spec(solo=True, bro='dev', prompt='--resume'), harness='bro')
    run = _parsed_run(_command(spec))
    assert (run.bro, run.prompt, run.resume) == ('dev', '--resume', False)


class TestParser:
  @pytest.mark.parametrize('harness_name', ['claude', 'bro'])
  def test_runs_the_named_harness(self, harness_name):
    with patch('ride.do_ride.run_session', return_value=0) as run:
      assert (
        do_ride.main(
          [
            'do-ride',
            'along',
            '--workspace',
            'w',
            '--harness',
            harness_name,
            '--hold',
            'attended',
            'dev',
          ]
        )
        == 0
      )
    harness, spec = run.call_args.args
    assert harness.name == harness_name
    assert spec.name == 'w'
    assert not spec.solo

  @pytest.mark.parametrize('flag', ['--in-place', '--unboxed', '--drop', '--grant'])
  def test_has_no_outer_machinery_flags(self, flag, capsys):
    with pytest.raises(SystemExit):
      do_ride.main(
        [
          'do-ride',
          'along',
          '--workspace',
          'w',
          '--harness',
          'bro',
          '--hold',
          'attended',
          flag,
          'dev',
        ]
      )
    assert 'unrecognized arguments' in capsys.readouterr().err

  def test_resume_is_an_ordinary_session_flag(self):
    run = _parsed_run(
      [
        'do-ride',
        'along',
        '--workspace',
        'w',
        '--harness',
        'bro',
        '--resume',
        '--hold',
        'attended',
        'dev',
      ]
    )
    assert run.resume


class TestRunSession:
  def _run(
    self,
    monkeypatch,
    tmp_path,
    *,
    repo=True,
    party_member=False,
    harness_code=0,
    harness_effect=None,
    resume=False,
    hooks=None,
    persona_effect=None,
  ):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(RUNTIME_ENV, str(tmp_path / 'runtime' / _BUNDLE_HASH / 'host'))
    if party_member:
      monkeypatch.setenv('RIDE_PARTY_MEMBER', 'broker-CH')
    else:
      monkeypatch.delenv('RIDE_PARTY_MEMBER', raising=False)
    monkeypatch.setattr(do_ride, 'bro_git_identity_env', lambda name: {'GIT_AUTHOR_NAME': name})
    monkeypatch.setattr(
      repo_attachment, 'recorded_origin_url', lambda _tree: 'https://example.test/repository.git'
    )
    declaration = MagicMock()
    declaration.provision_workspace.side_effect = persona_effect
    monkeypatch.setattr(repo_attachment, 'create_bro', lambda _name: declaration)
    monkeypatch.setattr(do_ride, '_install_credential_hooks', hooks or (lambda: None))
    harness = MagicMock()
    if harness_effect is None:
      harness.run_session.return_value = harness_code
    else:
      harness.run_session.side_effect = harness_effect
    run = do_ride.SessionRun(
      name='w',
      attachments=(repository_attachment(Path('/repo'), 'w'),) if repo else (),
      harness='bro',
      hold='attended',
      resolved_recipe=_spec().resolved_recipe,
      solo=False,
      resume=resume,
      bro='dev',
      prompt=None,
      activity_file=tmp_path / 'session' / 'activity',
    )
    return do_ride.run_session(harness, run), harness, declaration

  @pytest.mark.parametrize('isolation', ['boxed', 'unboxed'])
  def test_declared_command_runs_on_launch_and_resume_after_hooks(
    self, monkeypatch, tmp_path, capfd, isolation
  ):
    monkeypatch.setenv('RIDE_ISOLATION', isolation)
    config = tmp_path / '.bro' / 'config.toml'
    config.parent.mkdir()
    config.write_text(
      'provision-command = \'test -n "$RIDE_RUNNER_PID" && '
      'test "$BRO_HOLD" = attended && '
      'test "$(pwd)" = "$EXPECTED_ROOT" && '
      '! read -r answer && '
      'printf "%s\\n" "$HOOK_VALUE" >> provisioned && '
      'printf "setup output\\n" && printf "setup error\\n" >&2\'\n'
    )
    observed = []

    def hooks():
      assert os.environ['RIDE_WORKSPACE'] == 'w'
      monkeypatch.setenv('HOOK_VALUE', 'session-identity')
      monkeypatch.setenv('EXPECTED_ROOT', str(tmp_path))

    def persona(root):
      observed.append((root / 'provisioned').read_text())

    def harness(run, _launch_facts):
      assert len(observed) == (2 if run.resume else 1)
      return 0

    for resume in (False, True):
      code, driver, _ = self._run(
        monkeypatch,
        tmp_path,
        resume=resume,
        hooks=hooks,
        persona_effect=persona,
        harness_effect=harness,
      )
      assert code == 0
      driver.prepare_session.assert_called_once()
    assert observed == ['session-identity\n', 'session-identity\nsession-identity\n']
    output = capfd.readouterr()
    assert output.out == ''
    assert output.err.count('setup output') == 2
    assert output.err.count('setup error') == 2

  def test_resume_reads_the_clones_updated_configuration(self, monkeypatch, tmp_path):
    config = tmp_path / '.bro' / 'config.toml'
    config.parent.mkdir()
    config.write_text('provision-command = "touch provisioned"\n')
    assert self._run(monkeypatch, tmp_path)[0] == 0
    assert (tmp_path / 'provisioned').is_file()
    config.write_text('provision-command = "exit 23"\n')
    code, harness, declaration = self._run(monkeypatch, tmp_path, resume=True)
    assert code == 1
    declaration.provision_workspace.assert_not_called()
    harness.run_session.assert_not_called()

  def test_a_setup_script_without_a_declaration_does_not_run(self, monkeypatch, tmp_path):
    script = tmp_path / 'setup.sh'
    script.write_text('#!/bin/sh\ntouch filename-hook-ran\n')
    script.chmod(0o755)
    code, harness, _ = self._run(monkeypatch, tmp_path)
    assert code == 0
    harness.run_session.assert_called_once()
    assert not (tmp_path / 'filename-hook-ran').exists()

  @pytest.mark.parametrize('resume', [False, True])
  def test_failing_command_stops_before_persona_and_harness(
    self, monkeypatch, tmp_path, caplog, resume
  ):
    config = tmp_path / '.bro' / 'config.toml'
    config.parent.mkdir()
    config.write_text('provision-command = "exit 23"\n')
    code, harness, declaration = self._run(monkeypatch, tmp_path, resume=resume)
    assert code == 1
    declaration.provision_workspace.assert_not_called()
    harness.prepare_session.assert_not_called()
    harness.run_session.assert_not_called()
    assert 'provisioning command failed with exit status 23: exit 23' in caplog.text

  def test_refused_configuration_stops_before_persona_and_harness(
    self, monkeypatch, tmp_path, caplog
  ):
    config = tmp_path / '.bro' / 'config.toml'
    config.parent.mkdir()
    config.write_text('unknown = true\n')
    code, harness, declaration = self._run(monkeypatch, tmp_path)
    assert code == 1
    declaration.provision_workspace.assert_not_called()
    harness.prepare_session.assert_not_called()
    harness.run_session.assert_not_called()
    assert str(config) in caplog.text and 'unknown' in caplog.text

  @pytest.mark.parametrize('repo, member', [(False, False), (True, True)])
  def test_detached_and_joined_sessions_do_not_read_repository_configuration(
    self, monkeypatch, tmp_path, repo, member
  ):
    config = tmp_path / '.bro' / 'config.toml'
    config.parent.mkdir()
    config.write_text('unknown = true\n')
    code, harness, declaration = self._run(monkeypatch, tmp_path, repo=repo, party_member=member)
    assert code == 0
    declaration.provision_workspace.assert_not_called()
    harness.run_session.assert_called_once()

  def test_the_session_environment_precedes_the_harness_run(self, monkeypatch, tmp_path):
    code, harness, declaration = self._run(monkeypatch, tmp_path, harness_code=7)
    assert code == 7
    assert os.environ['RIDE_WORKSPACE'] == 'w'
    assert os.environ['RIDE_BRO'] == 'dev'
    assert os.environ['GIT_AUTHOR_NAME'] == 'dev'
    assert os.environ['BRO_HOLD'] == 'attended'
    assert os.environ['RIDE_RUNNER_PID'] == str(os.getpid())
    declaration.provision_workspace.assert_called_once_with(tmp_path)
    harness.run_session.assert_called_once()

  def test_detached_session_skips_persona_provisioning(self, monkeypatch, tmp_path):
    _, _, declaration = self._run(monkeypatch, tmp_path, repo=False)
    declaration.provision_workspace.assert_not_called()

  def test_party_member_skips_persona_provisioning(self, monkeypatch, tmp_path):
    _, _, declaration = self._run(monkeypatch, tmp_path, party_member=True)
    declaration.provision_workspace.assert_not_called()

  def test_process_record_exists_during_the_run_and_is_removed_after(self, monkeypatch, tmp_path):
    seen: list[dict] = []
    process_path = tmp_path / 'session' / do_ride.PROCESS_FILENAME

    def capture(_run, _launch_facts):
      seen.append(json.loads(process_path.read_text()))
      return 0

    code, _, _ = self._run(monkeypatch, tmp_path, harness_effect=capture)
    assert code == 0
    assert seen[0]['pid'] == os.getpid()
    assert seen[0]['start_time'].startswith(('linux-ticks:', 'ps:'))
    assert not process_path.exists()

  def test_the_session_is_marked_active_at_its_start_and_its_end(self, monkeypatch, tmp_path):
    activity_file = tmp_path / 'session' / 'activity'

    def age_the_start_mark(_run, _launch_facts):
      os.utime(activity_file, (1000.0, 1000.0))
      return 0

    self._run(monkeypatch, tmp_path, harness_effect=age_the_start_mark)
    assert activity_file.stat().st_mtime > 1000.0

  def test_the_harness_marks_where_the_host_reads(self, tmp_path):
    workspace = Workspace.create('w', (), Isolation.UNBOXED)
    with patch.dict(os.environ, {'RIDE_SESSION_DIR': str(workspace_session_dir(workspace.path))}):
      run = _parsed_run(
        ['do-ride', 'along', '--workspace', 'w', '--harness', 'bro', '--hold', 'attended', 'dev']
      )
    run.activity_file.parent.mkdir(parents=True)
    run.activity_file.touch()
    assert workspace.last_active() is not None

  def test_arms_the_admitted_session_watch_before_the_harness(self, monkeypatch, tmp_path):
    monkeypatch.setattr(watches, 'session_watch_admitted', lambda: True)
    started: list[str] = []
    monkeypatch.setattr('bro.authority_view.capture_initial', lambda: started.append('snapshot'))

    def record_start(store, command):
      started.append(command)
      return MagicMock()

    monkeypatch.setattr(watches.Store, 'start', record_start)

    def capture(_run, _launch_facts):
      assert started == ['snapshot', watches.SESSION_WATCH_COMMAND]
      return 0

    self._run(monkeypatch, tmp_path, harness_effect=capture)

  def test_the_harness_runs_under_the_facts_of_its_launch(self, monkeypatch, tmp_path):
    monkeypatch.setattr(watches, 'session_watch_admitted', lambda: True)
    monkeypatch.setattr(watches.Store, 'start', lambda store, command: MagicMock())
    captured: list[bool] = []
    monkeypatch.setattr('bro.authority_view.capture_initial', lambda: captured.append(True))

    def initial():
      assert captured, 'the facts were collected before the initial authority was captured'
      return {
        'may_summon': ['eyebro'],
        'summon_entries': ['@eyebro for bro{}'],
        'permits': ['bro for *'],
        'may_give': [],
      }

    monkeypatch.setattr('bro.authority_view.initial', initial)
    _, harness, _ = self._run(monkeypatch, tmp_path)
    run, facts = harness.run_session.call_args.args
    assert facts.runtime == _BUNDLE_HASH
    assert facts.llm == format_recipe(run.recipe)
    assert facts.hold == run.hold
    assert facts.may_summon == ('@eyebro for bro{}',)

  def test_skips_the_session_watch_when_the_rule_does_not_admit_it(self, monkeypatch, tmp_path):
    monkeypatch.setattr(watches, 'session_watch_admitted', lambda: False)
    with patch.object(watches.Store, 'start') as start:
      self._run(monkeypatch, tmp_path)
    start.assert_not_called()

  def test_session_exit_stops_its_watch_producers(self, monkeypatch, tmp_path):
    monkeypatch.setattr(watches, 'session_watch_admitted', lambda: False)
    identities = []

    def start_watch(_run, _launch_facts):
      watch = watches.session_store().start('sleep 30')
      identity = watch.producer_identity()
      assert identity is not None
      identities.append(identity)
      return 0

    self._run(monkeypatch, tmp_path, harness_effect=start_watch)

    assert len(identities) == 1
    assert not identities[0].alive()

  def test_resumed_session_clears_the_previous_watch_store(self, monkeypatch, tmp_path):
    monkeypatch.setattr(watches, 'session_watch_admitted', lambda: False)
    watch_directory = tmp_path / 'session' / watches.WATCH_DIRNAME
    watch_directory.mkdir(parents=True)
    stale = watches.Watch('old command', watch_directory, watches.slug('old command'))
    stale.command_file.write_text('old command')
    stale.log.write_text('uncommitted\n[watch-run] exited 0\n')

    def capture(_run, _launch_facts):
      store = watches.session_store()
      assert store.declared() == []
      assert store.take() is None
      return 0

    self._run(monkeypatch, tmp_path, harness_effect=capture)


class TestCredentialHooks:
  def test_installs_the_hydrated_kinds_and_exports_the_result(self, monkeypatch, tmp_path):
    store = MagicMock()
    store.registry = {'github': MagicMock()}
    monkeypatch.setenv('RIDE_WORKSPACE', 'w')
    monkeypatch.setenv('BRO_STORE', str(tmp_path / 'store'))
    monkeypatch.setenv('BRO_INSTALL_KINDS', 'github')
    monkeypatch.setenv('RIDE_ISOLATION', 'unboxed')
    monkeypatch.setattr(do_ride, 'workspace_dir', lambda _name: tmp_path / 'workspace')
    monkeypatch.setattr(do_ride.credentials, 'default_store', lambda: store)
    with patch(
      'ride.do_ride.credentials.install_hooks', return_value={'GITHUB_TOKEN': 'token'}
    ) as install:
      do_ride._install_credential_hooks()
    assert install.call_args.args[1] == ['github']
    assert install.call_args.args[3] == tmp_path / 'workspace' / 'environment'
    assert os.environ[do_ride.INSTALL_DIRECTORY_ENV] == str(tmp_path / 'workspace' / 'environment')
    assert os.environ['GITHUB_TOKEN'] == 'token'

  def test_partial_contract_fails_fast(self, monkeypatch):
    monkeypatch.setenv('BRO_STORE', '/store')
    monkeypatch.delenv('BRO_INSTALL_KINDS', raising=False)
    with pytest.raises(RuntimeError, match='must be set together'):
      do_ride._install_credential_hooks()


class TestClaudeState:
  def test_preprovisioned_state_is_used_as_it_is(self, monkeypatch, tmp_path):
    config = tmp_path / 'claude'
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(config))
    monkeypatch.setenv('RIDE_ISOLATION', 'unboxed')
    with patch('bro.claude.harness.provision_unboxed_claude_dir') as provision:
      get_harness('claude').prepare_session(MagicMock(name='w'))
    provision.assert_not_called()


class TestRequestedExitStatus:
  def test_the_requested_status_outranks_the_harness_exit(self, monkeypatch, tmp_path):
    monkeypatch.setattr(os, 'kill', lambda pid, sig: None)

    def request(_run, _launch_facts):
      workspace_session.terminate_session(4)
      return 0

    code, _, _ = TestRunSession()._run(monkeypatch, tmp_path, harness_effect=request)
    assert code == 4
