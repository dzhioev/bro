import asyncio
import json
import os
import signal
import sys
import textwrap
import threading
from unittest.mock import MagicMock

import pytest

import ride.workspace.docker as workspace_docker
import ride.workspace.spawn as workspace_spawn
from bro.workspace.paths import workspace_dir
from ride.workspace.metadata import WorkspaceKind
from ride.workspace.model import Workspace


def _throwaway(name: str, project) -> Workspace:
  return Workspace.create(name, project, WorkspaceKind.CONTAINER, throwaway=True)


def _exit_record(tmp_path) -> str:
  return (workspace_dir(tmp_path / 'proj', 'broker-CH') / 'exit').read_text()


class TestDockerLaunchSpec:
  def test_defaults(self):
    launch = workspace_docker.Launch(
      name='broker-X',
      command=['x'],
      env={},
      secrets=(),
      docker_sock=False,
      tty=False,
      forward_env=False,
      image='runtime-image',
      runtime_bundle_hash='bundle-hash',
    )
    spec = workspace_spawn.DockerLaunchSpec(launch)
    assert spec.ring_bytes == workspace_spawn.DEFAULT_RING_BYTES == 64 * 1024


class TestBrokerLaunch:
  def test_adds_channel_without_changing_the_neutral_launch(self):
    launch = workspace_docker.Launch(
      name='broker-X',
      command=['broker', 'recv'],
      env={'RIDE_BRO': 'dev'},
      secrets=(),
      docker_sock=False,
      tty=False,
      forward_env=False,
      image='runtime-image',
      runtime_bundle_hash='bundle-hash',
      extra_mounts=('/existing:/mount',),
    )
    channel = workspace_spawn.Provisioned(channel='X', host_endpoint='/host/sock.sock')
    adapted = workspace_spawn._broker_launch(launch, channel)
    assert adapted.env == {'RIDE_BRO': 'dev', 'BROKER_CHANNEL': 'unix:/run/broker.sock'}
    assert adapted.extra_mounts == (
      '/existing:/mount',
      '/host/sock.sock:/run/broker.sock',
    )
    assert adapted.tty is False
    assert adapted.forward_env is False
    assert launch.env == {'RIDE_BRO': 'dev'}
    assert launch.extra_mounts == ('/existing:/mount',)


class TestRingBuffer:
  def test_under_cap_keeps_everything(self):
    ring = workspace_spawn._RingBuffer(100)
    ring.write(b'hello')
    ring.write(b' world')
    assert ring.tail() == b'hello world'

  def test_over_cap_keeps_last_bytes(self):
    ring = workspace_spawn._RingBuffer(4)
    ring.write(b'abcdefgh')
    assert ring.tail() == b'efgh'

  def test_trims_across_writes(self):
    ring = workspace_spawn._RingBuffer(4)
    ring.write(b'abc')
    ring.write(b'de')
    assert ring.tail() == b'bcde'

  def test_single_write_larger_than_cap(self):
    ring = workspace_spawn._RingBuffer(3)
    ring.write(b'abcdefg')
    assert ring.tail() == b'efg'

  def test_exact_cap(self):
    ring = workspace_spawn._RingBuffer(4)
    ring.write(b'abcd')
    assert ring.tail() == b'abcd'

  def test_negative_cap_rejected(self):
    with pytest.raises(ValueError):
      workspace_spawn._RingBuffer(-1)


class TestHostLogRedirect:
  def test_noop_when_stderr_is_not_a_tty(self, tmp_path):
    # pytest's captured fds are pipes, so the gate sees no terminal
    redirect = workspace_spawn._HostLogRedirect(tmp_path / 'log' / 's.log')
    redirect.flip()
    os.write(2, b'stays on stderr\n')
    redirect.restore()
    assert not (tmp_path / 'log' / 's.log').exists()

  def test_flip_routes_both_fds_and_restore_returns_them(self, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(os, 'isatty', lambda fd: True)
    host_log = tmp_path / 'log' / 'c:ws.log'
    redirect = workspace_spawn._HostLogRedirect(host_log)
    redirect.flip()
    os.write(1, b'stdout line\n')
    os.write(2, b'stderr line\n')
    redirect.restore()
    os.write(2, b'after restore\n')
    content = host_log.read_text()
    assert 'stdout line' in content
    assert 'stderr line' in content
    assert 'after restore' not in content
    # the post-restore pointer names the file and counts only this span's lines
    assert any(
      f'session host log: {host_log} (2 lines this session)' in record.message
      for record in caplog.records
    )

  def test_no_pointer_line_when_nothing_was_written(self, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(os, 'isatty', lambda fd: True)
    redirect = workspace_spawn._HostLogRedirect(tmp_path / 's.log')
    redirect.flip()
    redirect.restore()
    assert not any('session host log' in record.message for record in caplog.records)

  def test_pointer_line_counts_only_the_current_span(self, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(os, 'isatty', lambda fd: True)
    host_log = tmp_path / 's.log'
    host_log.write_text('previous session line\n')
    redirect = workspace_spawn._HostLogRedirect(host_log)
    redirect.flip()
    os.write(2, b'fresh line\n')
    redirect.restore()
    assert any('(1 line this session)' in record.message for record in caplog.records)

  def test_restore_without_flip_is_a_noop(self, tmp_path):
    workspace_spawn._HostLogRedirect(tmp_path / 's.log').restore()


# a stand-in for the attached docker client: exits 42 on SIGINT, 0 on a timeout
_INTERRUPTIBLE = textwrap.dedent("""
  import signal, sys, time
  signal.signal(signal.SIGINT, lambda *a: sys.exit(42))
  print('ready', flush=True)
  time.sleep(30)
""")


class TestAttachedRoot:
  @pytest.fixture(autouse=True)
  def removed(self, monkeypatch) -> list:
    removed: list = []

    async def fake_remove(container_id):
      removed.append(container_id)

    monkeypatch.setattr(workspace_spawn, '_force_remove', fake_remove)
    # default: the container exited with the client — the tests below that model a
    # detach override this
    monkeypatch.setattr(workspace_spawn, 'container_running', lambda container_id: False)
    return removed

  async def _spawn_interruptible(self) -> asyncio.subprocess.Process:
    process = await asyncio.create_subprocess_exec(
      sys.executable, '-c', _INTERRUPTIBLE, stdout=asyncio.subprocess.PIPE
    )
    assert process.stdout is not None
    await process.stdout.readline()  # handler installed
    return process

  @pytest.mark.asyncio
  async def test_forwards_sigint_and_restores_handler(self):
    process = await self._spawn_interruptible()
    root = workspace_spawn._AttachedRoot('cid', process)
    assert signal.getsignal(signal.SIGINT) is not signal.default_int_handler
    root._forward_sigint()
    assert await root.wait() == 42
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler

  @pytest.mark.asyncio
  async def test_forward_after_exit_is_noop(self):
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', 'pass')
    root = workspace_spawn._AttachedRoot('cid', process)
    assert await root.wait() == 0
    root._forward_sigint()  # process gone; must not raise

  @pytest.mark.asyncio
  async def test_wait_removes_the_container(self, removed):
    # the client can die while the container lives (sig-proxy is off on a tty attach),
    # so client exit must always be followed by container teardown
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', 'pass')
    root = workspace_spawn._AttachedRoot('cid', process)
    await root.wait()
    assert removed == ['cid']

  @pytest.mark.asyncio
  async def test_output_tail_is_empty(self):
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', 'pass')
    root = workspace_spawn._AttachedRoot('cid', process)
    await root.wait()
    assert root.output_tail() == ''

  @pytest.mark.asyncio
  async def test_detach_suspends_and_reattaches(self, removed, monkeypatch):
    # client exits 0 with the container running: the user hit the detach key. the
    # session suspends, then re-attaches; the second client exit (container gone)
    # ends the session with the client's code
    running = iter([True, False])
    monkeypatch.setattr(workspace_spawn, 'container_running', lambda container_id: next(running))
    suspended: list = []
    monkeypatch.setattr(
      workspace_spawn,
      'suspend_until_continued',
      lambda container_id: suspended.append(container_id),
    )
    attaches: list = []
    real_exec = asyncio.create_subprocess_exec

    def fake_exec(*argv, **kwargs):
      attaches.append(list(argv))
      return real_exec(sys.executable, '-c', 'raise SystemExit(5)', **kwargs)

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', fake_exec)
    process = await real_exec(sys.executable, '-c', 'pass')
    root = workspace_spawn._AttachedRoot('cid', process)
    assert await root.wait() == 5
    assert suspended == ['cid']
    assert attaches == [['docker', 'attach', '--detach-keys=ctrl-z', 'cid']]
    assert removed == ['cid']

  @pytest.mark.asyncio
  async def test_client_death_ends_the_session_without_suspend(self, removed, monkeypatch):
    # a nonzero client exit is never a detach (the detach key exits 0), whatever the
    # container state
    monkeypatch.setattr(workspace_spawn, 'container_running', lambda container_id: True)
    suspended: list = []
    monkeypatch.setattr(
      workspace_spawn,
      'suspend_until_continued',
      lambda container_id: suspended.append(container_id),
    )
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', 'raise SystemExit(3)')
    root = workspace_spawn._AttachedRoot('cid', process)
    assert await root.wait() == 3
    assert suspended == []
    assert removed == ['cid']

  @pytest.mark.asyncio
  async def test_forwarded_interrupt_ends_the_session_not_suspends_it(self, removed, monkeypatch):
    # an interrupted docker client also exits 0 while the container lives on — only
    # the remembered forward tells this apart from a detach
    monkeypatch.setattr(workspace_spawn, 'container_running', lambda container_id: True)
    suspended: list = []
    monkeypatch.setattr(
      workspace_spawn,
      'suspend_until_continued',
      lambda container_id: suspended.append(container_id),
    )
    code = textwrap.dedent("""
      import signal, sys, time
      signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
      print('ready', flush=True)
      time.sleep(30)
    """)
    process = await asyncio.create_subprocess_exec(
      sys.executable, '-c', code, stdout=asyncio.subprocess.PIPE
    )
    assert process.stdout is not None
    await process.stdout.readline()  # handler installed
    root = workspace_spawn._AttachedRoot('cid', process)
    root._forward_sigint()
    assert await root.wait() == 0
    assert suspended == []
    assert removed == ['cid']

  @pytest.mark.asyncio
  async def test_host_output_redirected_for_the_attached_span(self, tmp_path, monkeypatch):
    monkeypatch.setattr(os, 'isatty', lambda fd: True)
    host_log = tmp_path / 'c:ws.log'
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', 'pass')
    root = workspace_spawn._AttachedRoot('cid', process, host_log=host_log)
    os.write(2, b'mid-session line\n')
    await root.wait()
    os.write(2, b'post-session line\n')
    content = host_log.read_text()
    assert 'mid-session line' in content
    assert 'post-session line' not in content


class TestDockerChildCapture:
  async def _child(self, code: str, ring_bytes: int) -> workspace_spawn._DockerChild:
    # the same stream wiring DockerSpawner uses: stderr merged into the stdout pipe
    process = await asyncio.create_subprocess_exec(
      sys.executable,
      '-c',
      code,
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.STDOUT,
    )
    return workspace_spawn._DockerChild('cid', process, ring_bytes, workspace=None)

  @pytest.mark.asyncio
  async def test_tail_combines_stdout_and_stderr(self):
    code = 'import sys; print("out-line"); print("err-line", file=sys.stderr)'
    child = await self._child(code, workspace_spawn.DEFAULT_RING_BYTES)
    assert await child.wait() == 0
    tail = child.output_tail()
    assert 'out-line' in tail
    assert 'err-line' in tail

  @pytest.mark.asyncio
  async def test_tail_keeps_only_the_suffix(self):
    code = 'import sys; sys.stdout.write("x" * 5000 + "THE-END")'
    child = await self._child(code, 16)
    assert await child.wait() == 0
    assert child.output_tail() == 'x' * 9 + 'THE-END'


class TestDockerChildWorkspaceCleanup:
  def _workspace(self, monkeypatch, tmp_path, removed: list) -> Workspace:
    child_workspace = _throwaway('broker-CH', tmp_path / 'proj')
    monkeypatch.setattr(child_workspace, 'remove', lambda: removed.append(child_workspace.name))
    return child_workspace

  async def _child(self, child_workspace, code: str = 'pass') -> workspace_spawn._DockerChild:
    process = await asyncio.create_subprocess_exec(
      sys.executable,
      '-c',
      code,
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.STDOUT,
    )
    return workspace_spawn._DockerChild(
      'cid', process, workspace_spawn.DEFAULT_RING_BYTES, child_workspace
    )

  @pytest.mark.asyncio
  async def test_wait_removes_throwaway_workspace_on_clean_exit(self, monkeypatch, tmp_path):
    removed: list = []
    child = await self._child(self._workspace(monkeypatch, tmp_path, removed))
    assert await child.wait() == 0
    assert removed == ['broker-CH']

  @pytest.mark.asyncio
  async def test_wait_keeps_and_records_the_end_of_a_failed_child(self, monkeypatch, tmp_path):
    removed: list = []
    child = await self._child(
      self._workspace(monkeypatch, tmp_path, removed), code='raise SystemExit(3)'
    )
    assert await child.wait() == 3
    assert removed == []
    assert _exit_record(tmp_path) == '3'

  @pytest.mark.asyncio
  async def test_kill_keeps_and_records_the_throwaway_workspace(self, monkeypatch, tmp_path):
    async def fake_remove(container_id):
      pass

    monkeypatch.setattr(workspace_spawn, '_force_remove', fake_remove)
    removed: list = []
    child = await self._child(self._workspace(monkeypatch, tmp_path, removed))
    await child.kill()
    assert removed == []
    assert _exit_record(tmp_path) == 'killed'
    # the timeout path kills, then the attach exits — here with code 0: the kill's
    # keep decision must hold anyway
    assert await child.wait() == 0
    assert removed == []
    assert _exit_record(tmp_path) == 'killed'

  @pytest.mark.asyncio
  async def test_removal_failure_warns_instead_of_raising(self, monkeypatch, tmp_path):
    child_workspace = _throwaway('broker-CH', tmp_path / 'proj')

    def boom():
      raise RuntimeError('root-owned files')

    monkeypatch.setattr(child_workspace, 'remove', boom)
    warnings: list = []
    monkeypatch.setattr(
      workspace_spawn.log, 'warning', lambda msg, *args: warnings.append(msg % args)
    )
    child = await self._child(child_workspace)
    assert await child.wait() == 0
    assert warnings == ['could not remove broker child workspace broker-CH: root-owned files']


class TestAttachedProcess:
  async def _interruptible(self) -> asyncio.subprocess.Process:
    process = await asyncio.create_subprocess_exec(
      sys.executable, '-c', _INTERRUPTIBLE, stdout=asyncio.subprocess.PIPE
    )
    assert process.stdout is not None
    await process.stdout.readline()  # handler installed
    return process

  @pytest.mark.asyncio
  async def test_forwards_sigint_and_restores_handler(self):
    handle = workspace_spawn._AttachedProcess(await self._interruptible())
    assert signal.getsignal(signal.SIGINT) is not signal.default_int_handler
    handle._forward_sigint()
    assert await handle.wait() == 42
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler

  @pytest.mark.asyncio
  async def test_kill_terminates_a_live_process(self):
    process = await asyncio.create_subprocess_exec(
      sys.executable, '-c', 'import time; time.sleep(30)'
    )
    handle = workspace_spawn._AttachedProcess(process)
    await handle.kill()
    assert await handle.wait() == -signal.SIGKILL

  @pytest.mark.asyncio
  async def test_kill_after_exit_is_noop(self):
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', 'pass')
    handle = workspace_spawn._AttachedProcess(process)
    assert await handle.wait() == 0
    await handle.kill()  # process gone; must not raise

  @pytest.mark.asyncio
  async def test_output_tail_is_empty(self):
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', 'pass')
    handle = workspace_spawn._AttachedProcess(process)
    await handle.wait()
    assert handle.output_tail() == ''


class TestProcessSpawner:
  async def _spawn(self, command, cwd, env) -> workspace_spawn.ChildHandle:
    launch = workspace_spawn.ProcessLaunchSpec(command=command, cwd=cwd, env=env)
    provisioned = workspace_spawn.Provisioned(channel='CH', host_endpoint='/host/CH.sock')
    return await workspace_spawn.ProcessSpawner().spawn(launch, provisioned)

  @pytest.mark.asyncio
  async def test_env_is_the_spec_snapshot_plus_broker_channel(self, monkeypatch, tmp_path):
    monkeypatch.setenv('RIDE_AMBIENT_CANARY', 'leak')
    out = tmp_path / 'env.json'
    code = 'import json, os, sys; json.dump(dict(os.environ), open(sys.argv[1], "w"))'
    handle = await self._spawn(
      [sys.executable, '-c', code, str(out)], str(tmp_path), {'MARKER': 'x'}
    )
    assert await handle.wait() == 0
    env = json.loads(out.read_text())
    assert env['MARKER'] == 'x'
    assert env['BROKER_CHANNEL'] == 'unix:/host/CH.sock'
    # a spawn is a pure function of its LaunchSpec: nothing ambient leaks in
    assert 'RIDE_AMBIENT_CANARY' not in env

  @pytest.mark.asyncio
  async def test_runs_in_cwd_and_propagates_exit_code(self, tmp_path):
    code = 'open("here", "w"); raise SystemExit(7)'
    handle = await self._spawn([sys.executable, '-c', code], str(tmp_path), {})
    assert await handle.wait() == 7
    assert (tmp_path / 'here').is_file()

  @pytest.mark.asyncio
  async def test_headless_process_inherits_streams_without_interactive_handling(self, tmp_path):
    launch = workspace_spawn.ProcessLaunchSpec(
      command=[sys.executable, '-c', 'pass'], cwd=str(tmp_path), env={}, interactive=False
    )
    provisioned = workspace_spawn.Provisioned(channel='CH', host_endpoint='/host/CH.sock')
    handle = await workspace_spawn.ProcessSpawner().spawn(launch, provisioned)
    assert isinstance(handle, workspace_spawn._HeadlessProcess)
    assert await handle.wait() == 0


class TestCompositeSpawner:
  class _Recording(workspace_spawn.Spawner):
    def __init__(self):
      self.spawned: list = []

    async def spawn(self, launch, channel) -> workspace_spawn.ChildHandle:
      self.spawned.append(launch)
      return MagicMock()

  @pytest.mark.asyncio
  async def test_dispatches_on_launch_spec_type(self):
    docker, process = self._Recording(), self._Recording()
    composite = workspace_spawn.CompositeSpawner(
      {workspace_spawn.DockerLaunchSpec: docker, workspace_spawn.ProcessLaunchSpec: process}
    )
    channel = workspace_spawn.Provisioned(channel='CH', host_endpoint='/host/CH.sock')
    docker_launch = workspace_spawn.DockerLaunchSpec(
      workspace_docker.Launch(
        name='broker-CH',
        command=['x'],
        env={},
        secrets=(),
        docker_sock=False,
        tty=False,
        forward_env=False,
        image='runtime-image',
        runtime_bundle_hash='bundle-hash',
      )
    )
    process_launch = workspace_spawn.ProcessLaunchSpec(command=['x'], cwd='/', env={})
    await composite.spawn(docker_launch, channel)
    await composite.spawn(process_launch, channel)
    assert docker.spawned == [docker_launch]
    assert process.spawned == [process_launch]

  @pytest.mark.asyncio
  async def test_unregistered_type_raises(self):
    composite = workspace_spawn.CompositeSpawner({})
    channel = workspace_spawn.Provisioned(channel='CH', host_endpoint='/host/CH.sock')
    launch = workspace_spawn.ProcessLaunchSpec(command=['x'], cwd='/', env={})
    with pytest.raises(ValueError, match='ProcessLaunchSpec'):
      await composite.spawn(launch, channel)


class TestDockerSpawnerModes:
  @pytest.fixture
  def spawn_harness(self, monkeypatch, tmp_path):
    project = tmp_path / 'proj'
    project_threads: list[int] = []
    prepare_threads: list[int] = []
    workspace_threads: list[int] = []

    def project_root():
      project_threads.append(threading.get_ident())
      return project

    monkeypatch.setattr(workspace_spawn, 'project_root', project_root)
    prepared: list = []

    def fake_prepare(launch, prepared_project):
      prepared.append((launch, prepared_project))
      prepare_threads.append(threading.get_ident())
      return 'cid123'

    monkeypatch.setattr(workspace_spawn, 'prepare_container', fake_prepare)
    ensured = Workspace.ensure

    def fake_ensure(name, workspace_project, kind, **kwargs):
      workspace_threads.append(threading.get_ident())
      assert workspace_project == project
      workspace = ensured(name, workspace_project, kind, **kwargs)
      monkeypatch.setattr(workspace, 'remove', lambda: None)
      return workspace

    monkeypatch.setattr(workspace_spawn.Workspace, 'ensure', fake_ensure)

    async def fake_remove(container_id):
      pass

    monkeypatch.setattr(workspace_spawn, '_force_remove', fake_remove)
    monkeypatch.setattr(workspace_spawn, 'container_running', lambda container_id: False)
    starts: list = []
    start_kwargs: list[dict] = []
    real_exec = asyncio.create_subprocess_exec

    def fake_exec(*argv, **kwargs):
      starts.append(list(argv))
      start_kwargs.append(kwargs)
      return real_exec(sys.executable, '-c', 'pass', **kwargs)

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', fake_exec)
    return {
      'prepared': prepared,
      'starts': starts,
      'start_kwargs': start_kwargs,
      'project': project,
      'project_threads': project_threads,
      'prepare_threads': prepare_threads,
      'workspace_threads': workspace_threads,
    }

  @pytest.mark.asyncio
  async def test_attached_root_mode(self, spawn_harness):
    docker_launch = workspace_docker.Launch(
      name='ws',
      command=['claude'],
      env={},
      secrets=('github',),
      docker_sock=True,
      tty=True,
      forward_env=True,
      image='runtime-image',
      runtime_bundle_hash='bundle-hash',
      optional_secrets=('openai',),
    )
    launch = workspace_spawn.DockerLaunchSpec(docker_launch)
    provisioned = workspace_spawn.Provisioned(channel='CH', host_endpoint='/host/CH.sock')
    handle = await workspace_spawn.DockerSpawner().spawn(launch, provisioned)
    try:
      assert isinstance(handle, workspace_spawn._AttachedRoot)
      assert handle.output_tail() == ''
      prepared, project = spawn_harness['prepared'][0]
      assert project == spawn_harness['project']
      assert prepared.env['BROKER_CHANNEL'] == 'unix:/run/broker.sock'
      assert '/host/CH.sock:/run/broker.sock' in prepared.extra_mounts
      assert spawn_harness['starts'] == [
        ['docker', 'start', '-a', '-i', '--detach-keys=ctrl-z', 'cid123']
      ]
    finally:
      await handle.wait()

  @pytest.mark.asyncio
  async def test_headless_root_inherits_separate_streams(self, spawn_harness):
    docker_launch = workspace_docker.Launch(
      name='ws',
      command=['claude', '-p', 'answer'],
      env={},
      secrets=(),
      docker_sock=False,
      tty=False,
      forward_env=True,
      image='runtime-image',
      runtime_bundle_hash='bundle-hash',
    )
    launch = workspace_spawn.DockerLaunchSpec(docker_launch, capture_output=False)
    provisioned = workspace_spawn.Provisioned(channel='CH', host_endpoint='/host/CH.sock')
    handle = await workspace_spawn.DockerSpawner().spawn(launch, provisioned)
    assert isinstance(handle, workspace_spawn._HeadlessRoot)
    assert spawn_harness['starts'] == [['docker', 'start', '-a', 'cid123']]
    assert spawn_harness['start_kwargs'] == [{}]
    assert await handle.wait() == 0

  @pytest.mark.asyncio
  async def test_child_mode_uses_the_described_workspace(self, spawn_harness):
    docker_launch = workspace_docker.Launch(
      name='broker-CH',
      command=['broker', 'recv'],
      env={},
      secrets=(),
      docker_sock=False,
      tty=False,
      forward_env=False,
      image='runtime-image',
      runtime_bundle_hash='bundle-hash',
    )
    launch = workspace_spawn.DockerLaunchSpec(docker_launch)
    provisioned = workspace_spawn.Provisioned(channel='CH', host_endpoint='/host/CH.sock')
    handle = await workspace_spawn.DockerSpawner().spawn(launch, provisioned)
    assert isinstance(handle, workspace_spawn._DockerChild)
    assert spawn_harness['prepared'][0][0].name == 'broker-CH'
    assert spawn_harness['starts'] == [['docker', 'start', '-a', 'cid123']]
    assert await handle.wait() == 0

  @pytest.mark.asyncio
  async def test_blocking_prepare_runs_off_the_loop_thread(self, spawn_harness):
    docker_launch = workspace_docker.Launch(
      name='broker-CH',
      command=['x'],
      env={},
      secrets=(),
      docker_sock=False,
      tty=False,
      forward_env=False,
      image='runtime-image',
      runtime_bundle_hash='bundle-hash',
    )
    launch = workspace_spawn.DockerLaunchSpec(docker_launch)
    provisioned = workspace_spawn.Provisioned(channel='CH', host_endpoint='/host/CH.sock')
    handle = await workspace_spawn.DockerSpawner().spawn(launch, provisioned)
    loop_thread = threading.get_ident()
    assert spawn_harness['project_threads'][0] != loop_thread
    assert spawn_harness['prepare_threads'][0] != loop_thread
    assert spawn_harness['workspace_threads'][0] != loop_thread
    assert await handle.wait() == 0
