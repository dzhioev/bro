import io
import json
import subprocess
import sys

import pytest

import ride.claude.stop_guard as stop_guard
from bro.broker.environment import BROKER_CHANNEL
from bro.quest import LiveMission

_CHILD = LiveMission('01m-child', 'bro', 'bro-eyebro')
_OTHER = LiveMission('01m-other', 'benchmark', 'benchmark')


def _task(command: str, *, status: str = 'running', task_id: str = 'task-1') -> dict:
  return {'id': task_id, 'type': 'shell', 'status': status, 'command': command}


def _payload(*tasks: dict, stop_hook_active: bool = False) -> dict:
  return {
    'hook_event_name': 'Stop',
    'stop_hook_active': stop_hook_active,
    'background_tasks': list(tasks),
  }


class TestNotice:
  def test_missions_in_flight_without_a_watch_are_held_and_named(self):
    reason = stop_guard.notice(
      _payload(_task('sleep 30')), [_CHILD, _OTHER], 'full', summoned=False
    )

    assert reason is not None
    assert reason.startswith('2 missions in flight and no `quest watch` armed')
    assert 'Arm `Monitor` on exactly `quest watch`, persistent' in reason
    assert reason.endswith('quest 01m-child to bro-eyebro\nmission 01m-other: benchmark')

  def test_a_raw_session_is_told_to_poll_instead_of_arming_a_monitor(self):
    reason = stop_guard.notice(_payload(), [_CHILD], 'raw', summoned=False)

    assert reason is not None
    assert reason.startswith('1 mission in flight and no `quest watch` armed')
    assert '`bro::quest_cancel`' in reason
    assert 'Monitor' not in reason

  def test_a_watch_with_nothing_in_flight_is_held_with_its_task_named(self):
    payload = _payload(_task('quest watch', task_id='bf8'))

    reason = stop_guard.notice(payload, [], 'full', summoned=False)

    assert reason is not None
    assert reason.startswith('`quest watch` is armed (task bf8) with no mission in flight')
    assert 'Stop it with `TaskStop` and end the turn' in reason

  def test_a_summoned_session_with_an_idle_watch_is_told_to_answer(self):
    payload = _payload(_task('quest watch', task_id='bf8'))

    reason = stop_guard.notice(payload, [], 'full', summoned=True)

    assert reason is not None
    assert 'Deliver your result with `bro::answer`, which ends the session' in reason
    assert 'TaskStop' not in reason

  def test_a_watch_over_missions_in_flight_is_the_wait_and_passes(self):
    assert (
      stop_guard.notice(_payload(_task('quest watch')), [_CHILD], 'full', summoned=False) is None
    )

  def test_nothing_armed_and_nothing_in_flight_passes(self):
    assert stop_guard.notice(_payload(_task('sleep 30')), [], 'full', summoned=False) is None

  def test_a_watch_that_is_no_longer_running_does_not_count(self):
    payload = _payload(_task('quest watch', status='killed'))
    reason = stop_guard.notice(payload, [_CHILD], 'full', summoned=False)

    assert reason is not None
    assert reason.startswith('1 mission in flight and no `quest watch` armed')

  def test_the_stop_after_a_held_one_stands_whatever_is_live(self):
    payload = _payload(_task('quest watch'), stop_hook_active=True)

    assert stop_guard.notice(payload, [], 'full', summoned=False) is None

  def test_a_payload_without_the_task_list_is_refused(self):
    payload = {'hook_event_name': 'Stop', 'stop_hook_active': False}

    with pytest.raises(ValueError, match='no background_tasks list'):
      stop_guard.notice(payload, [], 'full', summoned=False)


class TestMain:
  def _run(self, monkeypatch, capsys, payload: dict, *, surface: str = 'full') -> str:
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(payload)))
    assert stop_guard.main(['stop_guard', surface]) == 0
    return capsys.readouterr().out

  def test_a_held_turn_end_is_a_block_decision_on_stdout(self, monkeypatch, capsys):
    monkeypatch.setenv(BROKER_CHANNEL, 'tcp://token@127.0.0.1:1')
    monkeypatch.setattr(stop_guard, 'live_missions', lambda: [_CHILD])

    out = self._run(monkeypatch, capsys, _payload())

    decision = json.loads(out)
    assert decision['decision'] == 'block'
    assert decision['reason'].endswith('quest 01m-child to bro-eyebro')

  def test_a_standing_turn_end_prints_nothing(self, monkeypatch, capsys):
    monkeypatch.setenv(BROKER_CHANNEL, 'tcp://token@127.0.0.1:1')
    monkeypatch.setattr(stop_guard, 'live_missions', lambda: [_CHILD])

    assert self._run(monkeypatch, capsys, _payload(_task('quest watch'))) == ''

  def test_without_a_broker_channel_the_journal_is_not_read(self, monkeypatch, capsys):
    monkeypatch.delenv(BROKER_CHANNEL, raising=False)

    def unreachable():
      raise AssertionError('no channel, no query')

    monkeypatch.setattr(stop_guard, 'live_missions', unreachable)

    out = self._run(monkeypatch, capsys, _payload(_task('quest watch', task_id='w1')))

    assert json.loads(out)['reason'].startswith('`quest watch` is armed (task w1)')

  def test_an_unknown_surface_is_refused(self, monkeypatch):
    monkeypatch.setattr(sys, 'stdin', io.StringIO('{}'))
    with pytest.raises(ValueError, match="unknown session surface 'tui'"):
      stop_guard.main(['stop_guard', 'tui'])


def test_a_failure_lets_the_turn_end_stand_with_the_reason_on_stderr():
  completed = subprocess.run(
    [sys.executable, '-m', 'ride.claude.stop_guard', 'full'],
    input=json.dumps({'hook_event_name': 'Stop', 'stop_hook_active': False}),
    capture_output=True,
    text=True,
    env={'PATH': '', 'PYTHONPATH': ':'.join(sys.path)},
    check=False,
  )

  assert completed.returncode == stop_guard.FAILURE_STATUS
  assert completed.stdout == ''
  assert 'no background_tasks list' in completed.stderr
