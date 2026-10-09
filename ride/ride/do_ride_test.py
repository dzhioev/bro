import dataclasses
import json
import os
from unittest.mock import MagicMock, patch

import pytest

import bro.workspace.session as workspace_session
import ride.do_ride as do_ride
from bro import watches
from bro.monitor import workspace_session_dir
from ride.harness import get_harness
from ride.session_test import _spec
from ride.workspace.metadata import Isolation
from ride.workspace.model import Workspace


@pytest.fixture(autouse=True)
def isolated_environ():
  with patch.dict(os.environ, {'RIDE_ISOLATION': 'boxed'}, clear=False):
    yield


def _command(spec) -> list[str]:
  return do_ride.command(spec)


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
      llm='::xhigh+fast',
      bro='dev',
      grant=['gmail_creds'],
      revoke=['notion'],
      into='feature',
      prompt='do it',
    )
    assert _command(spec) == [
      'do-ride', 'along', '--workspace', 'w', '--harness', 'claude', '--repo', str(spec.repo),
      '--hold', 'attended', '--llm', '::xhigh+fast', '--', 'dev', 'do it',
    ]  # fmt: skip

  def test_resume_is_carried(self):
    spec = _spec(resume=True, bro='dev')
    assert _command(spec) == [
      'do-ride', 'along', '--workspace', 'w', '--harness', 'claude', '--resume',
      '--repo', str(spec.repo), '--hold', 'attended', '--', 'dev',
    ]  # fmt: skip

  def test_recorded_recipe_crosses_the_launcher_boundary(self, monkeypatch):
    from bro.llm.llms.claude_code import LLMSpec

    recorded = LLMSpec(model='recorded-model').dump()
    spec = dataclasses.replace(_spec(llm=None), resolved_llm=recorded)
    monkeypatch.setenv(do_ride.RESOLVED_LLM_ENV, do_ride.encode_resolved_llm(recorded))
    with patch.object(get_harness('claude'), 'resolve_llm') as resolve:
      run = _parsed_run(_command(spec))
    assert run.resolved_llm == recorded
    resolve.assert_not_called()

  def test_the_launchers_host_entry_does_not_reach_the_run(self, monkeypatch, tmp_path):
    config = tmp_path / 'bro.json'
    config.write_text(
      json.dumps({'projects': {'/repo': {'bros': {'dev': {'llm': 'openai:sol:xhigh'}}}}})
    )
    monkeypatch.setattr('bro.base.host_config.HOST_CONFIG_FILE', str(config))
    argv = ['do-ride', 'solo', '--workspace', 'w', '--harness', 'bro', '--repo', '/repo']
    run = _parsed_run([*argv, '--hold', 'unattended', '--llm', '::low', 'dev', 'prompt'])
    assert run.resolved_llm == get_harness('bro').resolve_llm('::low', 'dev').dump()

  def test_the_bro_harness_uses_the_same_executable(self):
    spec = dataclasses.replace(_spec(solo=True, bro='dev', prompt='go'), harness='bro')
    assert _command(spec) == [
      'do-ride', 'solo', '--workspace', 'w', '--harness', 'bro', '--repo', str(spec.repo),
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
  ):
    monkeypatch.chdir(tmp_path)
    if party_member:
      monkeypatch.setenv('RIDE_PARTY_MEMBER', 'broker-CH')
    else:
      monkeypatch.delenv('RIDE_PARTY_MEMBER', raising=False)
    monkeypatch.setattr(do_ride, 'bro_git_identity_env', lambda name: {'GIT_AUTHOR_NAME': name})
    monkeypatch.setattr(
      do_ride, 'recorded_origin_url', lambda _tree: 'https://example.test/repository.git'
    )
    declaration = MagicMock()
    monkeypatch.setattr(do_ride, 'create_bro', lambda _name: declaration)
    monkeypatch.setattr(do_ride, '_install_credential_hooks', lambda: None)
    harness = MagicMock()
    if harness_effect is None:
      harness.run_session.return_value = harness_code
    else:
      harness.run_session.side_effect = harness_effect
    run = do_ride.SessionRun(
      name='w',
      repo='/repo' if repo else None,
      harness='bro',
      hold='attended',
      llm=None,
      resolved_llm=_spec().resolved_llm,
      solo=False,
      resume=False,
      bro='dev',
      prompt=None,
      activity_file=tmp_path / 'session' / 'activity',
    )
    return do_ride.run_session(harness, run), harness, declaration

  def test_the_session_environment_precedes_the_harness_run(self, monkeypatch, tmp_path):
    code, harness, declaration = self._run(monkeypatch, tmp_path, harness_code=7)
    assert code == 7
    assert os.environ['RIDE_WORKSPACE'] == 'w'
    assert os.environ['RIDE_REPO'] == '/repo'
    assert os.environ['RIDE_REPO_URL'] == 'https://example.test/repository.git'
    assert os.environ['RIDE_BRO'] == 'dev'
    assert os.environ['GIT_AUTHOR_NAME'] == 'dev'
    assert os.environ['BRO_HOLD'] == 'attended'
    assert os.environ['RIDE_RUNNER_PID'] == str(os.getpid())
    declaration.provision_workspace.assert_called_once_with(tmp_path)
    harness.run_session.assert_called_once()

  def test_detached_session_skips_persona_provisioning(self, monkeypatch, tmp_path):
    os.environ['RIDE_REPO'] = '/ambient'
    os.environ['RIDE_REPO_URL'] = 'https://ambient.example/repository.git'
    _, _, declaration = self._run(monkeypatch, tmp_path, repo=False)
    assert 'RIDE_REPO' not in os.environ
    assert 'RIDE_REPO_URL' not in os.environ
    declaration.provision_workspace.assert_not_called()

  def test_party_member_skips_persona_provisioning(self, monkeypatch, tmp_path):
    _, _, declaration = self._run(monkeypatch, tmp_path, party_member=True)
    declaration.provision_workspace.assert_not_called()

  def test_process_record_exists_during_the_run_and_is_removed_after(self, monkeypatch, tmp_path):
    seen: list[dict] = []
    process_path = tmp_path / 'session' / do_ride.PROCESS_FILENAME

    def capture(_run):
      seen.append(json.loads(process_path.read_text()))
      return 0

    code, _, _ = self._run(monkeypatch, tmp_path, harness_effect=capture)
    assert code == 0
    assert seen[0]['pid'] == os.getpid()
    assert seen[0]['start_time'].startswith(('linux-ticks:', 'ps:'))
    assert not process_path.exists()

  def test_the_session_is_marked_active_at_its_start_and_its_end(self, monkeypatch, tmp_path):
    activity_file = tmp_path / 'session' / 'activity'

    def age_the_start_mark(_run):
      os.utime(activity_file, (1000.0, 1000.0))
      return 0

    self._run(monkeypatch, tmp_path, harness_effect=age_the_start_mark)
    assert activity_file.stat().st_mtime > 1000.0

  def test_the_harness_marks_where_the_host_reads(self, tmp_path):
    workspace = Workspace.create('w', None, Isolation.UNBOXED)
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

    def record_start(store, command):
      started.append(command)
      return MagicMock()

    monkeypatch.setattr(watches.Store, 'start', record_start)

    def capture(_run):
      assert started == [watches.SESSION_WATCH_COMMAND]
      return 0

    self._run(monkeypatch, tmp_path, harness_effect=capture)

  def test_skips_the_session_watch_when_the_rule_does_not_admit_it(self, monkeypatch, tmp_path):
    monkeypatch.setattr(watches, 'session_watch_admitted', lambda: False)
    with patch.object(watches.Store, 'start') as start:
      self._run(monkeypatch, tmp_path)
    start.assert_not_called()

  def test_session_exit_stops_its_watch_producers(self, monkeypatch, tmp_path):
    monkeypatch.setattr(watches, 'session_watch_admitted', lambda: False)
    identities = []

    def start_watch(_run):
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

    def capture(_run):
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
  def test_preprovisioned_state_only_needs_the_installations_plugin_seed(
    self, monkeypatch, tmp_path
  ):
    config = tmp_path / 'claude'
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(config))
    monkeypatch.setenv('RIDE_ISOLATION', 'boxed')
    with (
      patch('ride.claude.harness.provision_unboxed_claude_dir') as provision,
      patch('ride.claude.harness.seed_session_plugins') as seed,
    ):
      get_harness('claude').prepare_session(MagicMock(name='w'))
    provision.assert_not_called()
    seed.assert_called_once_with(config, container=True)


class TestRequestedExitStatus:
  def test_the_requested_status_outranks_the_harness_exit(self, monkeypatch, tmp_path):
    monkeypatch.setattr(os, 'kill', lambda pid, sig: None)

    def request(_run):
      workspace_session.terminate_session(4)
      return 0

    code, _, _ = TestRunSession()._run(monkeypatch, tmp_path, harness_effect=request)
    assert code == 4
