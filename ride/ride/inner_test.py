import dataclasses
import os
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

import bro.workspace.session as workspace_session
import ride.cli as ride_cli
import ride.inner as ride_inner
from bro.launch.broxy import START_SESSION_BROXY_ENV
from ride.harness import get_harness
from ride.session_test import _spec


@pytest.fixture(autouse=True)
def isolated_environ():
  """run_in_place exports the session environment (git identity, RIDE_BRO, the
  hold, the runner pid) into the live process environment; snapshot-restore it
  so no test here leaks them into the rest of the suite."""
  with patch.dict(os.environ, {}, clear=False):
    yield


def _inner_argv(spec) -> list[str]:
  harness = get_harness(spec.harness)
  return ride_inner.inner_command(spec, harness_flags=harness.inner_flags(spec))


class TestInnerCommand:
  def test_drops_machinery_flags_and_carries_the_rest(self):
    spec = _spec(
      host=True,
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
    assert _inner_argv(spec) == [
      'ride', 'along', '--in-place', '--workspace', 'w', '--harness', 'claude', '--repo', str(spec.repo),
      '--hold', 'attended', '--llm', '::xhigh+fast', 'dev', 'do it', '--', '--foo',
    ]  # fmt: skip

  def test_resume_and_raw_carried(self):
    spec = _spec(resume=True, bro='dev', raw=True)
    assert _inner_argv(spec) == [
      'ride', 'along', '--in-place', '--workspace', 'w', '--harness', 'claude',
      '--resume', '--raw', '--repo', str(spec.repo), '--hold', 'attended', 'dev',
    ]  # fmt: skip

  def test_the_bro_harness_re_enters_the_same_way(self):
    spec = dataclasses.replace(
      _spec(solo=True, bro='dev', prompt='go'), harness='bro', harness_options={}
    )
    assert _inner_argv(spec) == [
      'ride', 'solo', '--in-place', '--workspace', 'w', '--harness', 'bro', '--repo', str(spec.repo),
      '--hold', 'attended', 'dev', 'go',
    ]  # fmt: skip


def _outer_spec(argv: list[str]):
  with patch('ride.cli.start_session', return_value=0) as start:
    assert ride_cli.main(argv) == 0
  return start.call_args.args[0]


def _inner_spec(argv: list[str]):
  with patch('ride.inner.run_in_place', return_value=0) as run:
    assert ride_cli.main(argv) == 0
  return run.call_args.args[1]


class TestInPlaceDispatch:
  @pytest.mark.parametrize('harness_name', ['claude', 'bro'])
  def test_in_place_runs_the_named_harness(self, harness_name):
    with patch('ride.inner.run_in_place', return_value=0) as run:
      assert (
        ride_cli.main(
          ['ride', 'along', '--in-place', '--workspace', 'w', '--harness', harness_name, 'dev']
        )
        == 0
      )
    harness, spec = run.call_args.args
    assert harness.name == harness_name
    assert spec.name == 'w'
    assert not spec.solo

  def test_in_place_rejects_outer_machinery(self):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'along', '--in-place', '--workspace', 'w', '--host', 'dev'])


class TestRunInPlace:
  def test_the_session_environment_precedes_the_harness_run(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ride_inner, 'bro_git_identity_env', lambda name: {'GIT_AUTHOR_NAME': name})
    declaration = MagicMock()
    monkeypatch.setattr(ride_inner, 'create_bro', lambda _name: declaration)
    harness = MagicMock()
    harness.run_in_place.return_value = 7
    assert ride_inner.run_in_place(harness, _spec(bro='dev')) == 7
    assert os.environ['RIDE_BRO'] == 'dev'
    assert os.environ['GIT_AUTHOR_NAME'] == 'dev'
    declaration.provision_workspace.assert_called_once_with(tmp_path)
    harness.run_in_place.assert_called_once()

  def test_the_hold_and_kill_target_overwrite_an_ambient_pair(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ride_inner, 'bro_git_identity_env', lambda _name: {})
    monkeypatch.setattr(ride_inner, 'create_bro', lambda _name: MagicMock())
    harness = MagicMock()
    harness.run_in_place.return_value = 0
    os.environ.update({'BRO_HOLD': 'unattended', 'RIDE_RUNNER_PID': '1'})
    assert ride_inner.run_in_place(harness, _spec(hold='attended')) == 0
    assert os.environ['BRO_HOLD'] == 'attended'
    assert os.environ['RIDE_RUNNER_PID'] == str(os.getpid())

  def test_detached_session_skips_persona_workspace_provisioning(self, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ride_inner, 'bro_git_identity_env', lambda _name: {})
    declaration = MagicMock()
    monkeypatch.setattr(ride_inner, 'create_bro', lambda _name: declaration)
    harness = MagicMock()
    harness.run_in_place.return_value = 0
    assert ride_inner.run_in_place(harness, dataclasses.replace(_spec(), repo=None)) == 0
    declaration.provision_workspace.assert_not_called()


class TestRequestedExitStatus:
  def _run(self, monkeypatch, tmp_path, harness_code: int) -> int:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('RIDE_SESSION_DIR', str(tmp_path / 'session'))
    monkeypatch.setattr(ride_inner, 'bro_git_identity_env', lambda _name: {})
    monkeypatch.setattr(ride_inner, 'create_bro', lambda _name: MagicMock())
    harness = MagicMock()
    harness.run_in_place.return_value = harness_code
    return ride_inner.run_in_place(harness, _spec(bro='dev'))

  def test_the_requested_status_outranks_the_harness_exit(self, monkeypatch, tmp_path):
    harness = MagicMock()
    harness.run_in_place.side_effect = lambda _spec: workspace_session.terminate_session(4) or 0
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('RIDE_SESSION_DIR', str(tmp_path / 'session'))
    monkeypatch.setenv('RIDE_RUNNER_PID', str(os.getpid()))
    monkeypatch.setattr(os, 'kill', lambda pid, sig: None)
    monkeypatch.setattr(ride_inner, 'bro_git_identity_env', lambda _name: {})
    monkeypatch.setattr(ride_inner, 'create_bro', lambda _name: MagicMock())
    assert ride_inner.run_in_place(harness, _spec(bro='dev')) == 4

  def test_the_harness_exit_stands_when_nothing_asked(self, monkeypatch, tmp_path):
    assert self._run(monkeypatch, tmp_path, 3) == 3

  def test_an_earlier_runs_request_does_not_carry_over(self, monkeypatch, tmp_path):
    session = tmp_path / 'session'
    session.mkdir()
    (session / workspace_session.FILENAME).write_text('9')
    assert self._run(monkeypatch, tmp_path, 0) == 0


class TestSessionBroxy:
  def _channel_seen_by_the_harness(
    self, monkeypatch, tmp_path, environment: dict, broxy
  ) -> Optional[str]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ride_inner, 'bro_git_identity_env', lambda _name: {})
    monkeypatch.setattr(ride_inner, 'create_bro', lambda _name: MagicMock())
    seen: list[Optional[str]] = []
    harness = MagicMock()
    harness.run_in_place.side_effect = lambda _spec: (
      seen.append(os.environ.get('BROKER_CHANNEL')) or 0
    )
    with (
      patch.dict(os.environ, environment, clear=False),
      patch('bro.launch.broxy._start_session_broxy', return_value=broxy),
    ):
      assert ride_inner.run_in_place(harness, _spec(bro='dev')) == 0
    return seen[0]

  def test_rewrites_the_channel_for_the_session_and_stops_the_broxy(self, monkeypatch, tmp_path):
    broxy = MagicMock()
    broxy.address = 'tcp://broxy-token@127.0.0.1:8'
    environment = {'BROKER_UPSTREAM': 'tcp://up-token@127.0.0.1:9', START_SESSION_BROXY_ENV: '1'}
    channel = self._channel_seen_by_the_harness(monkeypatch, tmp_path, environment, broxy)
    assert channel == broxy.address
    broxy.stop.assert_called_once()

  def test_unsets_the_channel_when_the_broxy_cannot_start(self, monkeypatch, tmp_path):
    environment = {'BROKER_UPSTREAM': 'tcp://up-token@127.0.0.1:9', START_SESSION_BROXY_ENV: '1'}
    assert self._channel_seen_by_the_harness(monkeypatch, tmp_path, environment, None) is None

  def test_an_entrypoint_owned_channel_passes_through(self, monkeypatch, tmp_path):
    # a container carries no BRO_START_SESSION_BROXY signal — only a host launch
    # sets it — so the entrypoint's channel reaches the session untouched
    broxy = MagicMock()
    environment = {'BROKER_CHANNEL': 'tcp://broxy-token@127.0.0.1:8'}
    channel = self._channel_seen_by_the_harness(monkeypatch, tmp_path, environment, broxy)
    assert channel == 'tcp://broxy-token@127.0.0.1:8'
    broxy.stop.assert_not_called()


class TestHoldRoundTrip:
  """the inner argv cannot carry --host, so the outer-resolved hold must reach the
  inner parse explicitly rather than through re-derivation."""

  @pytest.mark.parametrize(
    ('solo', 'host', 'resolved'),
    [
      (False, False, 'attended'),
      (False, True, 'guided'),
      (True, False, 'unattended'),
      (True, True, 'unattended'),
    ],
  )
  def test_the_outer_resolved_hold_survives_the_inner_parse(self, solo, host, resolved):
    verb = 'solo' if solo else 'along'
    argv = ['ride', verb, '--workspace', 'w', '--harness', 'claude']
    if host:
      argv.append('--host')
    argv.append('dev')
    if solo:
      argv.append('go')
    outer = _outer_spec(argv)
    assert outer.hold == resolved
    assert _inner_spec(_inner_argv(outer)).hold == resolved

  def test_an_explicit_guided_hold_survives_the_inner_parse(self):
    outer = _outer_spec(
      ['ride', 'along', '--workspace', 'w', '--harness', 'claude', '--hold', 'guided', 'dev']
    )
    assert _inner_spec(_inner_argv(outer)).hold == 'guided'
