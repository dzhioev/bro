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
