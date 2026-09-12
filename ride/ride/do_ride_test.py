import dataclasses
import json
import os
from unittest.mock import MagicMock, patch

import pytest

import bro.workspace.session as workspace_session
import ride.do_ride as do_ride
from ride.harness import get_harness
from ride.session_test import _spec
from ride.workspace.metadata import Isolation


@pytest.fixture(autouse=True)
def isolated_environ():
  with patch.dict(os.environ, {'RIDE_ISOLATION': 'boxed'}, clear=False):
    yield


def _command(spec) -> list[str]:
  harness = get_harness(spec.harness)
  return do_ride.command(spec, harness_flags=harness.session_flags(spec))


def _parsed_run(argv: list[str]) -> do_ride.SessionRun:
  args, arguments = do_ride._parse(argv)
  _, run = do_ride._session_run(args, arguments)
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
      arguments=['--foo'],
    )
    assert _command(spec) == [
      'do-ride', 'along', '--workspace', 'w', '--harness', 'claude', '--repo', str(spec.repo),
      '--hold', 'attended', '--llm', '::xhigh+fast', 'dev', 'do it', '--', '--foo',
    ]  # fmt: skip

  def test_resume_and_harness_flags_are_carried(self):
    spec = _spec(resume=True, bro='dev', raw=True)
    assert _command(spec) == [
      'do-ride', 'along', '--workspace', 'w', '--harness', 'claude', '--resume', '--raw',
      '--repo', str(spec.repo), '--hold', 'attended', 'dev',
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

  def test_the_bro_harness_uses_the_same_executable(self):
    spec = dataclasses.replace(
      _spec(solo=True, bro='dev', prompt='go'), harness='bro', harness_options={}
    )
    assert _command(spec) == [
      'do-ride', 'solo', '--workspace', 'w', '--harness', 'bro', '--repo', str(spec.repo),
      '--hold', 'attended', 'dev', 'go',
    ]  # fmt: skip


class TestParser:
  @pytest.mark.parametrize('harness_name', ['claude', 'bro'])
  def test_runs_the_named_harness(self, harness_name, monkeypatch, tmp_path):
    monkeypatch.setenv('RIDE_SESSION_DIR', str(tmp_path / 'session'))
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

  def test_forwarded_arguments_require_the_separator(self):
    run = _parsed_run(
      [
        'do-ride',
        'solo',
        '--workspace',
        'w',
        '--harness',
        'bro',
        '--hold',
        'unattended',
        'dev',
        'go',
        '--',
        '--fork',
      ]
    )
    assert run.arguments == ['--fork']


class TestRunSession:
  def _run(self, monkeypatch, tmp_path, *, repo=True, harness_code=0, harness_effect=None):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('RIDE_SESSION_DIR', str(tmp_path / 'session'))
    monkeypatch.setattr(do_ride, 'bro_git_identity_env', lambda name: {'GIT_AUTHOR_NAME': name})
    declaration = MagicMock()
    monkeypatch.setattr(do_ride, 'create_bro', lambda _name: declaration)
    monkeypatch.setattr(do_ride, '_install_credential_hooks', lambda: None)
    monkeypatch.setattr(do_ride, '_prepare_claude_state', lambda _run: None)
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
      arguments=[],
      harness_options={},
    )
    return do_ride.run_session(harness, run), harness, declaration

  def test_the_session_environment_precedes_the_harness_run(self, monkeypatch, tmp_path):
    code, harness, declaration = self._run(monkeypatch, tmp_path, harness_code=7)
    assert code == 7
    assert os.environ['RIDE_WORKSPACE'] == 'w'
    assert os.environ['RIDE_REPO'] == '/repo'
    assert os.environ['RIDE_BRO'] == 'dev'
    assert os.environ['GIT_AUTHOR_NAME'] == 'dev'
    assert os.environ['BRO_HOLD'] == 'attended'
    assert os.environ['RIDE_RUNNER_PID'] == str(os.getpid())
    declaration.provision_workspace.assert_called_once_with(tmp_path)
    harness.run_session.assert_called_once()

  def test_detached_session_skips_persona_provisioning(self, monkeypatch, tmp_path):
    os.environ['RIDE_REPO'] = '/ambient'
    _, _, declaration = self._run(monkeypatch, tmp_path, repo=False)
    assert 'RIDE_REPO' not in os.environ
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
      patch('ride.claude.claude_config.provision_unboxed_claude_dir') as provision,
      patch('ride.claude.claude_config.seed_session_plugins') as seed,
    ):
      do_ride._prepare_claude_state(MagicMock(harness='claude'))
    provision.assert_not_called()
    seed.assert_called_once_with(config, container=True)

  def test_non_claude_harness_does_nothing(self):
    with patch('ride.claude.claude_config.seed_session_plugins') as seed:
      do_ride._prepare_claude_state(MagicMock(harness='bro'))
    seed.assert_not_called()


class TestRequestedExitStatus:
  def test_the_requested_status_outranks_the_harness_exit(self, monkeypatch, tmp_path):
    monkeypatch.setattr(os, 'kill', lambda pid, sig: None)

    def request(_run):
      workspace_session.terminate_session(4)
      return 0

    code, _, _ = TestRunSession()._run(monkeypatch, tmp_path, harness_effect=request)
    assert code == 4
