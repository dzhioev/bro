import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime

from bro.broker import brotocol
from bro.broker.client import Client
from bro.monitor import health
from ride.claude import statusline


def _iso(seconds: float) -> str:
  return datetime.fromtimestamp(seconds, UTC).isoformat()


def _quest(
  request_id: str,
  state: str,
  *,
  target: str = 'reviewer',
  trail_id: str | None = None,
  manual: bool = False,
  at: float | None = None,
  outcome: str | None = None,
  reason: str | None = None,
) -> dict:
  at = time.time() if at is None else at
  quest = {
    'id': request_id,
    'kind': 'summon',
    'parent': 'ROOT',
    'args': {'target': target, 'prompt': 'work', **({'manual': True} if manual else {})},
    'state': state,
  }
  if state in ('accepted', 'started'):
    quest['accepted_at'] = _iso(at)
    if state == 'started':
      quest['started_at'] = _iso(at)
  else:
    quest['ended_at'] = _iso(at)
  if trail_id is not None:
    quest['trail_id'] = trail_id
  if outcome is not None:
    quest['outcome'] = outcome
  if reason is not None:
    quest['reason'] = reason
  return quest


def _render(monkeypatch, tmp_path, *, recording=None, quests=None, detached=False) -> str:
  monkeypatch.delenv('RIDE_WORKSPACE', raising=False)
  monkeypatch.delenv('RIDE_REPO', raising=False)
  if detached:
    monkeypatch.setenv('RIDE_WORKSPACE', 'ws')
  monkeypatch.setattr(health, 'health_path', lambda: tmp_path / 'health.json')
  if recording is not None:
    health.write(recording, interval=3)
  monkeypatch.setattr(statusline, '_query_summons', lambda: list(quests or []))
  return statusline.render_statusline()


class TestRenderedStatusline:
  def test_failing_recording_is_red(self, monkeypatch, tmp_path):
    output = _render(monkeypatch, tmp_path, recording='error')
    assert '⚠ session recording FAILING' in output
    assert '\033[1;31m' in output

  def test_healthy_empty_session_is_blank(self, monkeypatch, tmp_path):
    assert _render(monkeypatch, tmp_path, recording='ok') == ''

  def test_detached_session_shows_the_attachment_state(self, monkeypatch, tmp_path):
    assert 'no repository attached' in _render(monkeypatch, tmp_path, detached=True)

  def test_active_summon_shows_target_trail_and_age(self, monkeypatch, tmp_path):
    quest = _quest('R1', 'started', trail_id='T1', at=time.time() - 185)
    assert '⚡ summoning reviewer 3m (trail T1)' in _render(monkeypatch, tmp_path, quests=[quest])

  def test_accepted_summon_without_a_trail_uses_acceptance_age(self, monkeypatch, tmp_path):
    quest = _quest('R1', 'accepted', at=time.time() - 5)
    assert '⚡ summoning reviewer 5s (no trail yet)' in _render(
      monkeypatch, tmp_path, quests=[quest]
    )

  def test_unlaunched_manual_summon_shows_the_wait(self, monkeypatch, tmp_path):
    quest = _quest('TOK-1', 'accepted', manual=True, at=time.time() - 65)
    assert '⚡ awaiting manual reviewer launch 1m' in _render(monkeypatch, tmp_path, quests=[quest])

  def test_launched_manual_summon_shows_like_any_active_one(self, monkeypatch, tmp_path):
    quest = _quest('TOK-1', 'started', manual=True, trail_id='T1', at=time.time() - 5)
    assert '⚡ summoning reviewer 5s (trail T1)' in _render(monkeypatch, tmp_path, quests=[quest])

  def test_recent_terminal_outcome_shows(self, monkeypatch, tmp_path):
    quest = _quest('R1', 'ended', outcome='ok', at=time.time() - 30)
    assert '✓ summon reviewer: ok' in _render(monkeypatch, tmp_path, quests=[quest])

  def test_failed_outcome_includes_reason(self, monkeypatch, tmp_path):
    quest = _quest('R1', 'ended', outcome='failed', reason='timeout', at=time.time() - 30)
    assert '✗ summon reviewer: failed:timeout' in _render(monkeypatch, tmp_path, quests=[quest])

  def test_stale_outcome_drops_off(self, monkeypatch, tmp_path):
    quest = _quest('R1', 'ended', outcome='ok', at=time.time() - 2000)
    assert _render(monkeypatch, tmp_path, quests=[quest]) == ''

  def test_query_error_keeps_the_other_sections(self, monkeypatch, tmp_path):
    def fail():
      raise ConnectionError('gone')

    monkeypatch.setattr(statusline, '_query_summons', fail)
    monkeypatch.setattr(health, 'health_path', lambda: tmp_path / 'health.json')
    monkeypatch.setenv('RIDE_WORKSPACE', 'ws')
    monkeypatch.delenv('RIDE_REPO', raising=False)
    assert 'no repository attached' in statusline.render_statusline()

  def test_sections_join_on_one_line(self, monkeypatch, tmp_path):
    quest = _quest('R1', 'started', trail_id='T1')
    output = _render(monkeypatch, tmp_path, recording='error', quests=[quest])
    assert '\n' not in output
    assert ' · ' in output


def test_query_summons_reads_the_channel_once(monkeypatch):
  class FakeClient:
    def __enter__(self):
      return self

    def __exit__(self, *_exception_info):
      return False

    def call(self, kind, args, timeout):
      assert (kind, args, timeout) == ('query', {}, statusline._QUERY_TIMEOUT)
      return brotocol.result(
        'QUERY',
        'ok',
        value={
          'quests': [
            _quest('S1', 'started'),
            {'id': 'B1', 'kind': 'benchmark', 'state': 'started'},
          ]
        },
      )

  monkeypatch.setenv('BROKER_CHANNEL', 'tcp://unused@127.0.0.1:1')
  monkeypatch.setattr(Client, 'from_env', lambda: FakeClient())
  assert [quest['id'] for quest in statusline._query_summons()] == ['S1']


def test_settings_command_hides_stale_projection(monkeypatch, tmp_path):
  session = tmp_path / 'session'
  state = session / 'claude'
  state.mkdir(parents=True)
  (state / 'statusline').write_text('projected line\n')
  monkeypatch.setenv('RIDE_SESSION_DIR', str(session))

  (state / 'statusline.pid').write_text(str(os.getpid()))
  live = subprocess.run(
    statusline.statusline_command(), shell=True, capture_output=True, text=True, env=os.environ
  )
  assert live.stdout == 'projected line\n'

  (state / 'statusline.pid').write_text('99999999')
  stale = subprocess.run(
    statusline.statusline_command(), shell=True, capture_output=True, text=True, env=os.environ
  )
  assert stale.stdout == ''


def test_start_projector_clears_stale_state_and_returns_a_stoppable_process(monkeypatch, tmp_path):
  monkeypatch.setenv('RIDE_SESSION_DIR', str(tmp_path / 'session'))
  output_path, pid_path, _, _ = statusline._paths()
  output_path.parent.mkdir(parents=True)
  output_path.write_text('stale')
  pid_path.write_text('999999')

  stopped = threading.Event()

  class FakeProcess:
    pid = 123
    return_code = None

    def poll(self):
      return self.return_code

    def terminate(self):
      self.return_code = 0
      stopped.set()

    def wait(self, timeout: float | None = None):
      if not stopped.wait(timeout):
        assert timeout is not None
        raise subprocess.TimeoutExpired('projector', timeout)
      return self.return_code

    def kill(self):
      self.return_code = -9
      stopped.set()

  process = FakeProcess()

  def start(_argv, **_kwargs):
    assert not output_path.exists()
    assert not pid_path.exists()
    output_path.write_text('')
    pid_path.write_text(str(process.pid))
    return process

  monkeypatch.setattr(statusline.spawn, 'console_script', lambda name: name)
  monkeypatch.setattr(statusline.spawn, 'popen', start)
  projector = statusline.start_statusline_projector(os.environ)
  projector.stop()
  assert process.return_code == 0
  assert not output_path.exists()
  assert not pid_path.exists()


def test_monitor_reaps_an_exited_projector_and_removes_its_projection(monkeypatch, tmp_path):
  monkeypatch.setenv('RIDE_SESSION_DIR', str(tmp_path / 'session'))
  output_path, pid_path, _, _ = statusline._paths()
  output_path.parent.mkdir(parents=True)
  output_path.write_text('stale line\n')
  process = subprocess.Popen([sys.executable, '-c', 'pass'])
  pid_path.write_text(str(process.pid))
  liveness_read_fd, liveness_write_fd = os.pipe()
  os.close(liveness_read_fd)

  projector = statusline._monitor_projector(process, output_path, pid_path, liveness_write_fd)
  projector.monitor.join(timeout=5)

  assert process.returncode == 0
  assert not projector.monitor.is_alive()
  assert not output_path.exists()
  assert not pid_path.exists()
  rendered = subprocess.run(
    statusline.statusline_command(), shell=True, capture_output=True, text=True, env=os.environ
  )
  assert rendered.stdout == ''


def _terminate_pid(process_id: int) -> None:
  with contextlib.suppress(ProcessLookupError):
    os.kill(process_id, signal.SIGTERM)


def test_abrupt_parent_death_retires_the_old_projector_before_resume(monkeypatch, tmp_path):
  session = tmp_path / 'session'
  marker = tmp_path / 'old-projector.pid'
  monkeypatch.setenv('RIDE_SESSION_DIR', str(session))
  environment = dict(os.environ)
  code = (
    'import os; from pathlib import Path; '
    'from ride.claude.statusline import start_statusline_projector; '
    'projector = start_statusline_projector(os.environ); '
    f'Path({str(marker)!r}).write_text(str(projector.process.pid)); '
    'os._exit(0)'
  )
  parent = subprocess.run([sys.executable, '-c', code], env=environment, timeout=10)
  assert parent.returncode == 0
  old_process_id = int(marker.read_text())
  output_path, pid_path, _, _ = statusline._paths()
  with contextlib.ExitStack() as cleanup:
    cleanup.callback(_terminate_pid, old_process_id)
    resumed = statusline.start_statusline_projector(environment)
    cleanup.callback(resumed.stop)
    assert resumed.process.pid != old_process_id
    assert pid_path.read_text().strip() == str(resumed.process.pid)
    assert output_path.is_file()
