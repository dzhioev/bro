from unittest.mock import MagicMock

import pytest

import ride.claude.harness as claude_harness
from bro.bro import RAISE_EXIT_STATUS


def test_claude_harness_can_end_only_a_managed_session(monkeypatch):
  assert not claude_harness.CLAUDE.can_end_session()

  monkeypatch.setenv('RIDE_RUNNER_PID', '4242')
  assert claude_harness.CLAUDE.can_end_session()


@pytest.mark.asyncio
async def test_claude_harness_records_an_answer_and_terminates(monkeypatch):
  monkeypatch.setenv('RIDE_RUNNER_PID', '4242')
  channel = MagicMock()
  terminate_session = MagicMock()
  monkeypatch.setattr(claude_harness.RunLifecycle, 'from_env', lambda: channel)
  monkeypatch.setattr(claude_harness, 'terminate_session', terminate_session)

  await claude_harness.CLAUDE.end_session('the verdict', 'ok')

  channel.completed.assert_called_once_with('the verdict', 'ok')
  channel.close.assert_called_once_with()
  terminate_session.assert_called_once_with(status=0)


@pytest.mark.asyncio
async def test_claude_harness_spares_the_session_when_an_answer_cannot_be_delivered(monkeypatch):
  monkeypatch.setenv('RIDE_RUNNER_PID', '4242')
  terminate_session = MagicMock()
  monkeypatch.setattr(claude_harness.RunLifecycle, 'from_env', lambda: None)
  monkeypatch.setattr(claude_harness, 'terminate_session', terminate_session)

  with pytest.raises(RuntimeError, match='cannot reach the summoner'):
    await claude_harness.CLAUDE.end_session('the verdict', 'ok')

  terminate_session.assert_not_called()


@pytest.mark.asyncio
async def test_claude_harness_records_a_raise_and_terminates(monkeypatch):
  monkeypatch.setenv('RIDE_RUNNER_PID', '4242')
  channel = MagicMock()
  terminate_session = MagicMock()
  monkeypatch.setattr(claude_harness.RunLifecycle, 'from_env', lambda: channel)
  monkeypatch.setattr(claude_harness, 'terminate_session', terminate_session)

  await claude_harness.CLAUDE.end_session('missing api key', 'raised')

  channel.completed.assert_called_once_with('missing api key', 'raised')
  channel.close.assert_called_once_with()
  terminate_session.assert_called_once_with(status=RAISE_EXIT_STATUS)


@pytest.mark.asyncio
async def test_claude_harness_raises_without_a_channel_and_terminates(monkeypatch):
  monkeypatch.setenv('RIDE_RUNNER_PID', '4242')
  terminate_session = MagicMock()
  monkeypatch.setattr(claude_harness.RunLifecycle, 'from_env', lambda: None)
  monkeypatch.setattr(claude_harness, 'terminate_session', terminate_session)

  await claude_harness.CLAUDE.end_session('no tool fits', 'raised')

  terminate_session.assert_called_once_with(status=RAISE_EXIT_STATUS)


@pytest.mark.asyncio
async def test_claude_harness_terminates_when_raise_delivery_fails(monkeypatch):
  monkeypatch.setenv('RIDE_RUNNER_PID', '4242')
  channel = MagicMock()
  channel.completed.side_effect = ConnectionError('channel closed')
  terminate_session = MagicMock()
  monkeypatch.setattr(claude_harness.RunLifecycle, 'from_env', lambda: channel)
  monkeypatch.setattr(claude_harness, 'terminate_session', terminate_session)

  with pytest.raises(ConnectionError, match='channel closed'):
    await claude_harness.CLAUDE.end_session('broker down', 'raised')

  terminate_session.assert_called_once_with(status=RAISE_EXIT_STATUS)


def test_claude_resolution_refuses_an_api_recipe():
  from bro.llm.providers import LLMSelectionError

  with pytest.raises(LLMSelectionError, match='not openai'):
    claude_harness.CLAUDE.resolve_llm('openai:', 'bro')


def test_claude_resolution_keeps_its_default_recipe():
  from bro.llm.llms.claude_code import LLMSpec

  assert claude_harness.CLAUDE.resolve_llm(None, 'bro') == LLMSpec()


def test_claude_bundle_provisioning_copies_a_verified_executable(monkeypatch, tmp_path):
  import hashlib

  from ride.claude import provisioning

  binary = tmp_path / 'cached'
  binary.write_bytes(b'engine')
  binary.chmod(0o755)
  binary.with_suffix('.sha256').write_text(hashlib.sha256(binary.read_bytes()).hexdigest())
  requests = []

  def cached(version, platform):
    requests.append((version, platform))
    return binary

  monkeypatch.setattr(provisioning.claude_release, 'cached_binary', cached)
  root = tmp_path / 'bundle'
  root.mkdir()

  files = claude_harness.CLAUDE.provision_bundle(root, ('linux', 'x86_64', 'glibc'))
  carried = provisioning.claude_release.verified_binary(root / 'claude' / 'claude')
  assert carried.read_bytes() == binary.read_bytes()
  assert set(files) == {
    str(path.relative_to(root)) for path in (carried, carried.with_suffix('.sha256'))
  }
  assert requests == [(provisioning.claude_code_version(), 'linux-x64')]


def test_claude_bootstrap_does_not_download_inside_the_provisioned_image(monkeypatch):
  probe = MagicMock()
  monkeypatch.setattr(claude_harness.CLAUDE, 'check_runtime', probe)
  monkeypatch.setenv('RIDE_IN_CONTAINER', '1')

  claude_harness.CLAUDE.setup_runtime()
  probe.assert_not_called()
  monkeypatch.delenv('RIDE_IN_CONTAINER')
  claude_harness.CLAUDE.setup_runtime()
  probe.assert_called_once_with()
