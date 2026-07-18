import sys

import workspace.containers
import workspace.docker


class _FakeProc:
  def __init__(self, returncode=0, stdout='', stderr: str | bytes = ''):
    self.returncode = returncode
    self.stdout = stdout
    self.stderr = stderr


class TestAttachInteractive:
  def _harness(self, monkeypatch, codes: list[int], running: list[bool]):
    events: list = []
    code_iterator = iter(codes)
    running_iterator = iter(running)

    def fake_run(argv, *args, **kwargs):
      events.append(argv)
      return _FakeProc(returncode=next(code_iterator))

    monkeypatch.setattr(workspace.containers.subprocess, 'run', fake_run)
    monkeypatch.setattr(
      workspace.containers, 'container_running', lambda cid: next(running_iterator)
    )
    monkeypatch.setattr(
      workspace.containers, 'suspend_until_continued', lambda cid: events.append(('suspend', cid))
    )
    return events

  def test_detach_suspends_and_reattaches(self, monkeypatch):
    events = self._harness(monkeypatch, codes=[0, 4], running=[True])
    assert workspace.containers.attach_interactive('cid123') == 4
    assert events == [
      ['docker', 'start', '-a', '-i', '--detach-keys=ctrl-z', 'cid123'],
      ('suspend', 'cid123'),
      ['docker', 'attach', '--detach-keys=ctrl-z', 'cid123'],
    ]

  def test_container_exit_returns_without_suspend(self, monkeypatch):
    events = self._harness(monkeypatch, codes=[0], running=[False])
    assert workspace.containers.attach_interactive('cid123') == 0
    assert events == [['docker', 'start', '-a', '-i', '--detach-keys=ctrl-z', 'cid123']]

  def test_client_death_returns_without_probing_the_container(self, monkeypatch):
    # a nonzero client exit is never a detach (the detach key exits 0); the running
    # probe must not even run — the daemon may be the very thing that just failed
    events = self._harness(monkeypatch, codes=[130], running=[])
    assert workspace.containers.attach_interactive('cid123') == 130
    assert events == [['docker', 'start', '-a', '-i', '--detach-keys=ctrl-z', 'cid123']]


class TestBrokerGate:
  def test_disabled_by_env(self, monkeypatch):
    # presence-checked: any value disables, and the check precedes any broker import
    monkeypatch.setenv('BROKER_DISABLED', '')
    assert workspace.containers.broker_enabled() is False

  def test_unimportable_broker_degrades(self, monkeypatch):
    monkeypatch.delenv('BROKER_DISABLED', raising=False)
    monkeypatch.setitem(sys.modules, 'broker', None)  # import machinery raises ImportError
    assert workspace.containers.broker_enabled() is False

  def test_enabled_by_default(self, monkeypatch):
    monkeypatch.delenv('BROKER_DISABLED', raising=False)
    assert workspace.containers.broker_enabled() is True

  def test_container_gate_degrades_on_macos(self, monkeypatch):
    # the daemon runs in a VM there — the channel socket can't be bind-mounted
    monkeypatch.delenv('BROKER_DISABLED', raising=False)
    monkeypatch.setattr(sys, 'platform', 'darwin')
    assert workspace.containers.container_broker_enabled() is False

  def test_container_gate_delegates_off_macos(self, monkeypatch):
    monkeypatch.delenv('BROKER_DISABLED', raising=False)
    monkeypatch.setattr(sys, 'platform', 'linux')
    assert workspace.containers.container_broker_enabled() is True
