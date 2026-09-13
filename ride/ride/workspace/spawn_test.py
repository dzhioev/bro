import asyncio
import json
import os
import signal
import sys
import textwrap
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import ride.workspace.docker as workspace_docker
import ride.workspace.spawn as workspace_spawn
from bro.broker.transports.tcp import Endpoint
from bro.monitor import PROCESS_FILENAME, party_member_dir, workspace_session_dir
from bro.workspace.paths import workspace_dir
from ride.workspace.docker import CONTAINER_BROKER_HOST, MemberExec
from ride.workspace.metadata import Isolation
from ride.workspace.model import Workspace


def _throwaway(name: str, project) -> Workspace:
  return Workspace.create(name, project, Isolation.BOXED, throwaway=True)


def _exit_record(tmp_path) -> str:
  return (workspace_dir('broker-CH') / 'exit').read_text()


class TestDockerLaunchSpec:
  def test_defaults(self):
    launch = workspace_docker.Launch(
      name='broker-X',
      command=['x'],
      env={},
      secrets=(),
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
      tty=False,
      forward_env=False,
      image='runtime-image',
      runtime_bundle_hash='bundle-hash',
      extra_mounts=('/existing:/mount',),
    )
    channel = workspace_spawn.Provisioned(
      channel='X', host_endpoint=Endpoint(port=7321, token='tk')
    )
    adapted = workspace_spawn._broker_launch(launch, channel, 'X-1')
    assert adapted.env == {
      'RIDE_BRO': 'dev',
      'BROKER_UPSTREAM': 'tcp://tk@host.docker.internal:7321',
      'BROKER_QUEST': 'X-1',
    }
    assert adapted.extra_mounts == ('/existing:/mount',)
    assert adapted.tty is False
    assert adapted.forward_env is False
    assert launch.env == {'RIDE_BRO': 'dev'}
    assert launch.extra_mounts == ('/existing:/mount',)


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
    return workspace_spawn._DockerChild(
      'cid', process, ring_bytes, None, 'broker-CH', workspace_spawn.PartyMembers()
    )

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
      'cid',
      process,
      workspace_spawn.DEFAULT_RING_BYTES,
      child_workspace,
      child_workspace.name,
      workspace_spawn.PartyMembers(),
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
    provisioned = workspace_spawn.Provisioned(
      channel='CH', host_endpoint=Endpoint(port=7321, token='tk')
    )
    return await workspace_spawn.ProcessSpawner().spawn(launch, provisioned, 'X-1')

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
    assert env['BROKER_UPSTREAM'] == 'tcp://tk@127.0.0.1:7321'
    assert env['BROKER_QUEST'] == 'X-1'
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
    provisioned = workspace_spawn.Provisioned(
      channel='CH', host_endpoint=Endpoint(port=7321, token='tk')
    )
    handle = await workspace_spawn.ProcessSpawner().spawn(launch, provisioned, 'X-1')
    assert isinstance(handle, workspace_spawn._HeadlessProcess)
    assert await handle.wait() == 0


class TestProcessChildWorkspaceCleanup:
  def _launch(self, tmp_path, code: str) -> workspace_spawn.ProcessLaunchSpec:
    project = tmp_path / 'project'
    project.mkdir()
    workspace = Workspace.create(
      'broker-CH',
      project,
      Isolation.UNBOXED,
      throwaway=True,
    )
    workspace.tree.mkdir(parents=True)
    private_store = tmp_path / 'private-store'
    private_store.mkdir()
    return workspace_spawn.ProcessLaunchSpec(
      command=[sys.executable, '-c', code],
      cwd=str(workspace.tree),
      env={},
      interactive=False,
      capture_output=True,
      workspace=workspace.name,
      cleanup_directory=str(private_store),
    )

  async def _spawn(self, launch):
    channel = workspace_spawn.Provisioned(
      channel='CH',
      host_endpoint=Endpoint(port=7321, token='tk'),
    )
    return await workspace_spawn.ProcessSpawner().spawn(launch, channel, 'X-1')

  @pytest.mark.asyncio
  async def test_clean_exit_removes_workspace_and_store(self, tmp_path):
    launch = self._launch(tmp_path, 'pass')
    assert launch.cleanup_directory is not None

    handle = await self._spawn(launch)

    assert await handle.wait() == 0
    assert not Path(launch.cleanup_directory).exists()
    with pytest.raises(ValueError, match='broker-CH'):
      Workspace.open('broker-CH')

  @pytest.mark.asyncio
  async def test_pre_spawn_failure_removes_store(self, tmp_path):
    private_store = tmp_path / 'private-store'
    private_store.mkdir()
    launch = workspace_spawn.ProcessLaunchSpec(
      command=['missing-command'],
      cwd=str(tmp_path),
      env={},
      interactive=False,
      capture_output=True,
      workspace='missing-workspace',
      cleanup_directory=str(private_store),
    )

    with pytest.raises(ValueError, match='missing-workspace'):
      await self._spawn(launch)

    assert not private_store.exists()

  @pytest.mark.asyncio
  async def test_pre_spawn_cleanup_failure_is_raised(self, monkeypatch, tmp_path):
    private_store = tmp_path / 'private-store'
    private_store.mkdir()
    launch = workspace_spawn.ProcessLaunchSpec(
      command=['missing-command'],
      cwd=str(tmp_path),
      env={},
      interactive=False,
      capture_output=True,
      workspace='missing-workspace',
      cleanup_directory=str(private_store),
    )

    def fail_cleanup(path):
      del path
      raise OSError('cleanup refused')

    monkeypatch.setattr(workspace_spawn.shutil, 'rmtree', fail_cleanup)

    with pytest.raises(OSError, match='cleanup refused'):
      await self._spawn(launch)

    assert private_store.is_dir()

  @pytest.mark.asyncio
  async def test_post_exit_cleanup_failure_is_raised(self, monkeypatch, tmp_path):
    launch = self._launch(tmp_path, 'pass')
    assert launch.cleanup_directory is not None
    private_store = Path(launch.cleanup_directory)
    real_rmtree = workspace_spawn.shutil.rmtree

    def fail_private_store(path, *args, **kwargs):
      if Path(path) == private_store:
        raise OSError('cleanup refused')
      return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(workspace_spawn.shutil, 'rmtree', fail_private_store)
    handle = await self._spawn(launch)

    with pytest.raises(OSError, match='cleanup refused'):
      await handle.wait()

    assert private_store.is_dir()
    assert Workspace.open('broker-CH').isolation is Isolation.UNBOXED

  @pytest.mark.asyncio
  async def test_failure_keeps_workspace_removes_store_and_captures_output(self, tmp_path):
    launch = self._launch(
      tmp_path,
      'import sys; print("failure detail"); raise SystemExit(3)',
    )

    handle = await self._spawn(launch)

    assert await handle.wait() == 3
    assert 'failure detail' in handle.output_tail()
    assert Workspace.open('broker-CH').isolation is Isolation.UNBOXED
    assert _exit_record(tmp_path) == '3'
    assert launch.cleanup_directory is not None
    assert not Path(launch.cleanup_directory).exists()

  @pytest.mark.asyncio
  async def test_kill_graces_the_leader_then_forces_the_process_group(self, monkeypatch, tmp_path):
    monkeypatch.setattr(workspace_spawn, '_PROCESS_TERM_GRACE', 0.1)
    child_ready = tmp_path / 'child-ready'
    child_stopped = tmp_path / 'child-stopped'
    parent_ready = tmp_path / 'parent-ready'
    parent_stopped = tmp_path / 'parent-stopped'
    child_code = f"""
import signal
import time
from pathlib import Path

def stop(*_args):
  Path({str(child_stopped)!r}).touch()
  raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
Path({str(child_ready)!r}).touch()
time.sleep(30)
"""
    parent_code = f"""
import signal
import subprocess
import sys
import time
from pathlib import Path

def stop(*_args):
  Path({str(parent_stopped)!r}).touch()
  raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
subprocess.Popen([sys.executable, '-c', {child_code!r}])
while not Path({str(child_ready)!r}).exists():
  time.sleep(0.01)
Path({str(parent_ready)!r}).touch()
time.sleep(30)
"""
    launch = self._launch(tmp_path, parent_code)
    handle = await self._spawn(launch)
    assert isinstance(handle, workspace_spawn._ProcessChild)
    async with asyncio.timeout(5):
      while not parent_ready.exists():
        await asyncio.sleep(0.01)

    await handle.kill()

    assert parent_stopped.is_file()
    assert not child_stopped.exists()
    assert await handle.wait() == 0
    assert _exit_record(tmp_path) == 'killed'

  @pytest.mark.asyncio
  async def test_kill_keeps_workspace_and_removes_store(self, tmp_path):
    launch = self._launch(tmp_path, 'import time; time.sleep(30)')
    handle = await self._spawn(launch)

    await handle.kill()

    assert await handle.wait() != 0
    assert Workspace.open('broker-CH').isolation is Isolation.UNBOXED
    assert _exit_record(tmp_path) == 'killed'
    assert launch.cleanup_directory is not None
    assert not Path(launch.cleanup_directory).exists()


class TestMemberTrailAdoption:
  def _record_member_trail(self, records: Path) -> str:
    from bro.trails.local import LocalStore
    from bro.trails.model import BlazeRequest

    store = LocalStore(records / 'trails')
    # a joined member's trail is summoned by its summoner's, which — for a
    # service-backed or --no-trails first session — lives in another backend, not
    # this local store; adoption must carry that external pointer
    trail_id = store.blaze(
      BlazeRequest(
        harness='bro',
        bro='dev',
        version='test',
        interactive=False,
        surface='ask',
        native={'llm': {'type': 'echo', 'model': 'echo'}},
        summoned_by={'trail_id': 'service-parent', 'step_id': 0},
        body={'records': [{'kind': 'system_prompt', 'body': 'prompt'}]},
      )
    )['id']
    store.end_trail(trail_id, 'ok')
    return trail_id

  def _host_store(self, monkeypatch, tmp_path):
    from bro.trails.local import LocalStore
    from bro.trails.store import local_root

    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'data'))
    monkeypatch.delenv('RIDE_IN_CONTAINER', raising=False)
    monkeypatch.delenv('RIDE_TRAILS_ROOT', raising=False)
    return LocalStore(local_root())

  @pytest.mark.asyncio
  async def test_clean_exit_adopts_the_member_trail_and_removes_its_records(
    self, monkeypatch, tmp_path
  ):
    host = self._host_store(monkeypatch, tmp_path)
    records = tmp_path / 'workspace' / 'party' / 'broker-CH'
    records.mkdir(parents=True)
    trail_id = self._record_member_trail(records)
    party_members = workspace_spawn.PartyMembers()

    await workspace_spawn._settle_member(party_members, MagicMock(), 'party-workspace', records, 0)

    # the member's own copy is gone with its records, but the trail survived into
    # the ride's host store, discoverable there like any other, its external
    # summon provenance preserved
    assert not records.exists()
    adopted = host.get_trail(trail_id)
    assert adopted['id'] == trail_id
    assert adopted['summoned_by']['trail_id'] == 'service-parent'

  @pytest.mark.asyncio
  async def test_failed_exit_keeps_records_but_still_adopts_the_trail(self, monkeypatch, tmp_path):
    host = self._host_store(monkeypatch, tmp_path)
    records = tmp_path / 'workspace' / 'party' / 'broker-CH'
    records.mkdir(parents=True)
    trail_id = self._record_member_trail(records)

    await workspace_spawn._settle_member(
      workspace_spawn.PartyMembers(), MagicMock(), 'party-workspace', records, 3
    )

    assert records.is_dir()  # kept for inspection on failure
    assert not (records / 'trails').exists()  # the trail moved to the host store
    assert host.get_trail(trail_id)['id'] == trail_id

  @pytest.mark.asyncio
  async def test_a_member_that_did_not_record_locally_needs_no_adoption(
    self, monkeypatch, tmp_path
  ):
    self._host_store(monkeypatch, tmp_path)
    records = tmp_path / 'workspace' / 'party' / 'broker-CH'
    (records / 'session').mkdir(parents=True)

    await workspace_spawn._settle_member(
      workspace_spawn.PartyMembers(), MagicMock(), 'party-workspace', records, 0
    )

    assert not records.exists()


class TestProcessChildPartyRecords:
  def _launch(self, tmp_path, code: str) -> workspace_spawn.ProcessLaunchSpec:
    records = tmp_path / 'workspace' / 'party' / 'broker-CH'
    records.mkdir(parents=True)
    private_store = tmp_path / 'private-store'
    private_store.mkdir()
    return workspace_spawn.ProcessLaunchSpec(
      command=[sys.executable, '-c', code],
      cwd=str(tmp_path),
      env={},
      interactive=False,
      capture_output=True,
      party_workspace='party-workspace',
      records_directory=str(records),
      cleanup_directory=str(private_store),
    )

  async def _spawn(self, launch):
    channel = workspace_spawn.Provisioned(
      channel='CH', host_endpoint=Endpoint(port=7321, token='tk')
    )
    return await workspace_spawn.ProcessSpawner().spawn(launch, channel, 'X-1')

  @pytest.mark.asyncio
  async def test_started_party_exit_kills_members_before_removing_its_workspace(
    self, tmp_path, caplog
  ):
    project = tmp_path / 'project'
    project.mkdir()
    workspace = Workspace.create('broker-owner', project, Isolation.UNBOXED, throwaway=True)
    workspace.tree.mkdir(parents=True)
    owner_store = tmp_path / 'owner-store'
    owner_store.mkdir()
    member_store = tmp_path / 'member-store'
    member_store.mkdir()
    records = party_member_dir(workspace.path, 'broker-member')
    records.mkdir(parents=True)
    spawner = workspace_spawn.ProcessSpawner()
    channel = workspace_spawn.Provisioned(
      channel='CH', host_endpoint=Endpoint(port=7321, token='tk')
    )
    owner = await spawner.spawn(
      workspace_spawn.ProcessLaunchSpec(
        command=[sys.executable, '-c', 'pass'],
        cwd=str(workspace.tree),
        env={},
        interactive=False,
        capture_output=True,
        workspace=workspace.name,
        cleanup_directory=str(owner_store),
      ),
      channel,
      'owner-quest',
    )
    member = await spawner.spawn(
      workspace_spawn.ProcessLaunchSpec(
        command=[sys.executable, '-c', 'import time; time.sleep(30)'],
        cwd=str(workspace.tree),
        env={},
        interactive=False,
        capture_output=True,
        party_workspace=workspace.name,
        records_directory=str(records),
        cleanup_directory=str(member_store),
      ),
      channel,
      'member-quest',
    )

    with caplog.at_level('WARNING'):
      assert await owner.wait() == 0

    assert await member.wait() != 0
    assert 'killing joined member broker-member' in caplog.text
    with pytest.raises(ValueError, match='broker-owner'):
      Workspace.open(workspace.name)

  @pytest.mark.asyncio
  async def test_concurrent_owner_kill_and_wait_finish_member_teardown_once(self, tmp_path):
    project = tmp_path / 'concurrent-project'
    project.mkdir()
    workspace = Workspace.create('broker-concurrent', project, Isolation.UNBOXED, throwaway=True)
    workspace.tree.mkdir(parents=True)
    owner_store = tmp_path / 'concurrent-owner-store'
    owner_store.mkdir()
    member_store = tmp_path / 'concurrent-member-store'
    member_store.mkdir()
    records = party_member_dir(workspace.path, 'broker-member')
    records.mkdir(parents=True)
    ready = tmp_path / 'member-ready'
    observed = tmp_path / 'workspace-observed'
    member_code = f"""
import signal
import time
from pathlib import Path

def stop(*_args):
  time.sleep(0.2)
  if Path({str(workspace.path)!r}).is_dir():
    Path({str(observed)!r}).touch()
  raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
Path({str(ready)!r}).touch()
time.sleep(30)
"""
    spawner = workspace_spawn.ProcessSpawner()
    channel = workspace_spawn.Provisioned(
      channel='CH', host_endpoint=Endpoint(port=7321, token='tk')
    )
    owner = await spawner.spawn(
      workspace_spawn.ProcessLaunchSpec(
        command=[sys.executable, '-c', 'import time; time.sleep(30)'],
        cwd=str(workspace.tree),
        env={},
        interactive=False,
        capture_output=True,
        workspace=workspace.name,
        cleanup_directory=str(owner_store),
      ),
      channel,
      'owner-quest',
    )
    member = await spawner.spawn(
      workspace_spawn.ProcessLaunchSpec(
        command=[sys.executable, '-c', member_code],
        cwd=str(workspace.tree),
        env={},
        interactive=False,
        capture_output=True,
        party_workspace=workspace.name,
        records_directory=str(records),
        cleanup_directory=str(member_store),
      ),
      channel,
      'member-quest',
    )
    async with asyncio.timeout(5):
      while not ready.exists():
        await asyncio.sleep(0.01)

    await asyncio.gather(owner.kill(), owner.wait())

    assert await member.wait() == 0
    assert observed.is_file()
    assert Workspace.open(workspace.name).path == workspace.path

  @pytest.mark.asyncio
  async def test_member_teardown_failure_preserves_the_owner_workspace(self, monkeypatch, tmp_path):
    project = tmp_path / 'failed-teardown-project'
    project.mkdir()
    workspace = Workspace.create(
      'broker-failed-teardown', project, Isolation.UNBOXED, throwaway=True
    )
    workspace.tree.mkdir(parents=True)
    owner_store = tmp_path / 'failed-teardown-owner-store'
    owner_store.mkdir()
    member_store = tmp_path / 'failed-teardown-member-store'
    member_store.mkdir()
    records = party_member_dir(workspace.path, 'broker-member')
    records.mkdir(parents=True)
    spawner = workspace_spawn.ProcessSpawner()
    channel = workspace_spawn.Provisioned(
      channel='CH', host_endpoint=Endpoint(port=7321, token='tk')
    )
    owner = await spawner.spawn(
      workspace_spawn.ProcessLaunchSpec(
        command=[sys.executable, '-c', 'pass'],
        cwd=str(workspace.tree),
        env={},
        interactive=False,
        capture_output=True,
        workspace=workspace.name,
        cleanup_directory=str(owner_store),
      ),
      channel,
      'owner-quest',
    )
    member = await spawner.spawn(
      workspace_spawn.ProcessLaunchSpec(
        command=[sys.executable, '-c', 'import time; time.sleep(30)'],
        cwd=str(workspace.tree),
        env={},
        interactive=False,
        capture_output=True,
        party_workspace=workspace.name,
        records_directory=str(records),
        cleanup_directory=str(member_store),
      ),
      channel,
      'member-quest',
    )

    async def fail_cleanup(directory):
      del directory
      raise OSError('cleanup refused')

    monkeypatch.setattr(member, '_remove_directory', fail_cleanup)
    with pytest.raises(ExceptionGroup, match='could not stop every member'):
      await owner.wait()

    assert Workspace.open(workspace.name).path == workspace.path
    assert owner_store.is_dir()

  @pytest.mark.asyncio
  async def test_clean_exit_removes_member_records_and_private_store(self, tmp_path):
    launch = self._launch(tmp_path, 'pass')
    handle = await self._spawn(launch)
    assert await handle.wait() == 0
    assert launch.records_directory is not None
    assert not Path(launch.records_directory).exists()
    assert launch.cleanup_directory is not None
    assert not Path(launch.cleanup_directory).exists()

  @pytest.mark.asyncio
  async def test_failure_keeps_member_records_and_removes_private_store(self, tmp_path):
    launch = self._launch(tmp_path, 'raise SystemExit(3)')
    handle = await self._spawn(launch)
    assert await handle.wait() == 3
    assert launch.records_directory is not None
    assert Path(launch.records_directory).is_dir()
    assert launch.cleanup_directory is not None
    assert not Path(launch.cleanup_directory).exists()

  @pytest.mark.asyncio
  async def test_kill_keeps_member_records_and_removes_private_store(self, tmp_path):
    launch = self._launch(tmp_path, 'import time; time.sleep(30)')
    handle = await self._spawn(launch)
    await handle.kill()
    assert launch.records_directory is not None
    assert Path(launch.records_directory).is_dir()
    assert launch.cleanup_directory is not None
    assert not Path(launch.cleanup_directory).exists()


def _member_stub_code(record: Path) -> str:
  """a stand-in for the exec'd member: writes the process record the in-container
  wrapper would, exits 0 on TERM, and lingers."""
  return f"""
import json, os, signal, sys, time
from pathlib import Path

fields = Path(f'/proc/{{os.getpid()}}/stat').read_text().rsplit(')', 1)[1].split()
record = {{'pid': os.getpid(), 'start_time': f'linux-ticks:{{fields[19]}}'}}
Path({str(record)!r}).write_text(json.dumps(record))
signal.signal(signal.SIGTERM, lambda *_args: sys.exit(0))
time.sleep(30)
"""


class TestExecMember:
  def _records(self, tmp_path) -> Path:
    records = tmp_path / 'workspace' / 'party' / 'broker-M'
    workspace_session_dir(records).mkdir(parents=True)
    return records

  def _launch(self, records: Path) -> workspace_spawn.ExecLaunchSpec:
    return workspace_spawn.ExecLaunchSpec(
      launch=MemberExec(
        container='cid-party',
        member='broker-M',
        command=['do-ride', 'solo'],
        env={'RIDE_BRO': 'dev'},
        secrets=set(),
      ),
      party_workspace='party-ws',
      records_directory=str(records),
    )

  def _fake_prepare(self, monkeypatch, code: str) -> dict:
    captured: dict = {}

    def prepare(member_exec: MemberExec) -> list[str]:
      captured['launch'] = member_exec
      return [sys.executable, '-c', code]

    monkeypatch.setattr(workspace_spawn, 'prepare_member_exec', prepare)
    return captured

  def _fake_kill(self, monkeypatch) -> list:
    calls: list = []

    def kill_argv(container: str, pid: int, ticks: str, signal_name: str) -> list[str]:
      calls.append((container, pid, ticks, signal_name))
      return ['kill', '-s', signal_name, str(pid)]

    monkeypatch.setattr(workspace_spawn, 'member_kill_argv', kill_argv)
    return calls

  async def _spawn(
    self, launch: workspace_spawn.ExecLaunchSpec, spawner: workspace_spawn.ExecSpawner
  ) -> workspace_spawn._ExecChild:
    channel = workspace_spawn.Provisioned(
      channel='CH', host_endpoint=Endpoint(port=7321, token='tk')
    )
    child = await spawner.spawn(launch, channel, 'X-1')
    assert isinstance(child, workspace_spawn._ExecChild)
    return child

  @pytest.mark.asyncio
  async def test_spawn_env_carries_the_channel_under_the_container_host(
    self, monkeypatch, tmp_path
  ):
    records = self._records(tmp_path)
    captured = self._fake_prepare(monkeypatch, 'pass')
    child = await self._spawn(self._launch(records), workspace_spawn.ExecSpawner())
    assert await child.wait() == 0
    channel = workspace_spawn.Provisioned(
      channel='CH', host_endpoint=Endpoint(port=7321, token='tk')
    )
    assert captured['launch'].env == {
      'RIDE_BRO': 'dev',
      'BROKER_UPSTREAM': channel.host_endpoint.address(CONTAINER_BROKER_HOST),
      'BROKER_QUEST': 'X-1',
    }
    assert not records.exists()  # a clean exit removes the member records

  @pytest.mark.asyncio
  async def test_spawn_completes_once_the_member_record_is_seen(self, monkeypatch, tmp_path):
    records = self._records(tmp_path)
    record_path = workspace_session_dir(records) / PROCESS_FILENAME
    self._fake_prepare(monkeypatch, _member_stub_code(record_path))
    kills = self._fake_kill(monkeypatch)
    child = await self._spawn(self._launch(records), workspace_spawn.ExecSpawner())
    # the handshake completed inside spawn: the record is there, the client lives
    assert record_path.is_file()
    assert child._process.returncode is None

    await child.kill()

    record = json.loads(record_path.read_text())
    ticks = record['start_time'].removeprefix('linux-ticks:')
    assert kills == [('cid-party', record['pid'], ticks, 'TERM')]
    assert await child.wait() == 0
    assert records.is_dir()  # a killed member's records are kept

  @pytest.mark.asyncio
  async def test_kill_finds_no_target_once_the_record_is_gone(self, monkeypatch, tmp_path):
    records = self._records(tmp_path)
    self._fake_prepare(monkeypatch, 'pass')  # exits without leaving a record
    kills = self._fake_kill(monkeypatch)
    child = await self._spawn(self._launch(records), workspace_spawn.ExecSpawner())
    await child.kill()
    assert kills == []

  @pytest.mark.asyncio
  async def test_spawn_into_an_ended_party_is_refused(self, monkeypatch, tmp_path):
    records = self._records(tmp_path)
    self._fake_prepare(monkeypatch, 'pass')
    party_members = workspace_spawn.PartyMembers()
    await party_members.end('party-ws')
    with pytest.raises(ValueError, match='ended before the member started'):
      await self._spawn(self._launch(records), workspace_spawn.ExecSpawner(party_members))

  @pytest.mark.asyncio
  async def test_boxed_party_exit_kills_its_exec_members(self, monkeypatch, tmp_path, caplog):
    records = self._records(tmp_path)
    record_path = workspace_session_dir(records) / PROCESS_FILENAME
    self._fake_prepare(monkeypatch, _member_stub_code(record_path))
    self._fake_kill(monkeypatch)
    party_members = workspace_spawn.PartyMembers()
    launch = workspace_spawn.ExecLaunchSpec(
      launch=MemberExec(
        container='cid-party',
        member='broker-M',
        command=['do-ride', 'solo'],
        env={},
        secrets=set(),
      ),
      party_workspace='broker-owner',
      records_directory=str(records),
    )
    member = await self._spawn(launch, workspace_spawn.ExecSpawner(party_members))
    owner_process = await asyncio.create_subprocess_exec(
      sys.executable,
      '-c',
      'pass',
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.STDOUT,
    )
    owner = workspace_spawn._DockerChild(
      'cid-owner',
      owner_process,
      workspace_spawn.DEFAULT_RING_BYTES,
      None,
      'broker-owner',
      party_members,
    )

    with caplog.at_level('WARNING'):
      assert await owner.wait() == 0

    assert 'killing joined member broker-M' in caplog.text
    assert await member.wait() == 0
    assert records.is_dir()


class TestCompositeSpawner:
  class _Recording(workspace_spawn.Spawner):
    def __init__(self):
      self.spawned: list = []

    async def spawn(self, launch, channel, quest) -> workspace_spawn.ChildHandle:
      self.spawned.append(launch)
      return MagicMock()

  @pytest.mark.asyncio
  async def test_dispatches_on_launch_spec_type(self):
    docker, process = self._Recording(), self._Recording()
    composite = workspace_spawn.CompositeSpawner(
      {workspace_spawn.DockerLaunchSpec: docker, workspace_spawn.ProcessLaunchSpec: process}
    )
    channel = workspace_spawn.Provisioned(
      channel='CH', host_endpoint=Endpoint(port=7321, token='tk')
    )
    docker_launch = workspace_spawn.DockerLaunchSpec(
      workspace_docker.Launch(
        name='broker-CH',
        command=['x'],
        env={},
        secrets=(),
        tty=False,
        forward_env=False,
        image='runtime-image',
        runtime_bundle_hash='bundle-hash',
      )
    )
    process_launch = workspace_spawn.ProcessLaunchSpec(command=['x'], cwd='/', env={})
    await composite.spawn(docker_launch, channel, 'X-1')
    await composite.spawn(process_launch, channel, 'X-1')
    assert docker.spawned == [docker_launch]
    assert process.spawned == [process_launch]

  @pytest.mark.asyncio
  async def test_unregistered_type_raises(self):
    composite = workspace_spawn.CompositeSpawner({})
    channel = workspace_spawn.Provisioned(
      channel='CH', host_endpoint=Endpoint(port=7321, token='tk')
    )
    launch = workspace_spawn.ProcessLaunchSpec(command=['x'], cwd='/', env={})
    with pytest.raises(ValueError, match='ProcessLaunchSpec'):
      await composite.spawn(launch, channel, 'X-1')


class TestDockerSpawnerModes:
  @pytest.fixture
  def spawn_harness(self, monkeypatch, tmp_path):
    project = tmp_path / 'proj'
    prepare_threads: list[int] = []
    workspace_threads: list[int] = []
    prepared: list = []

    def fake_prepare(launch):
      prepared.append(launch)
      prepare_threads.append(threading.get_ident())
      return 'cid123'

    monkeypatch.setattr(workspace_spawn, 'prepare_container', fake_prepare)
    ensured = Workspace.ensure

    def fake_ensure(name, repo, kind, **kwargs):
      workspace_threads.append(threading.get_ident())
      workspace = ensured(name, repo, kind, **kwargs)
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
      tty=True,
      forward_env=True,
      image='runtime-image',
      runtime_bundle_hash='bundle-hash',
      optional_secrets=('openai',),
    )
    launch = workspace_spawn.DockerLaunchSpec(docker_launch)
    provisioned = workspace_spawn.Provisioned(
      channel='CH', host_endpoint=Endpoint(port=7321, token='tk')
    )
    handle = await workspace_spawn.DockerSpawner().spawn(launch, provisioned, 'X-1')
    try:
      assert isinstance(handle, workspace_spawn._AttachedRoot)
      assert handle.output_tail() == ''
      prepared = spawn_harness['prepared'][0]
      assert prepared.env['BROKER_UPSTREAM'] == 'tcp://tk@host.docker.internal:7321'
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
      tty=False,
      forward_env=True,
      image='runtime-image',
      runtime_bundle_hash='bundle-hash',
    )
    launch = workspace_spawn.DockerLaunchSpec(docker_launch, capture_output=False)
    provisioned = workspace_spawn.Provisioned(
      channel='CH', host_endpoint=Endpoint(port=7321, token='tk')
    )
    handle = await workspace_spawn.DockerSpawner().spawn(launch, provisioned, 'X-1')
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
      tty=False,
      forward_env=False,
      image='runtime-image',
      runtime_bundle_hash='bundle-hash',
    )
    launch = workspace_spawn.DockerLaunchSpec(docker_launch)
    provisioned = workspace_spawn.Provisioned(
      channel='CH', host_endpoint=Endpoint(port=7321, token='tk')
    )
    handle = await workspace_spawn.DockerSpawner().spawn(launch, provisioned, 'X-1')
    assert isinstance(handle, workspace_spawn._DockerChild)
    assert spawn_harness['prepared'][0].name == 'broker-CH'
    assert spawn_harness['starts'] == [['docker', 'start', '-a', 'cid123']]
    assert await handle.wait() == 0

  @pytest.mark.asyncio
  async def test_blocking_prepare_runs_off_the_loop_thread(self, spawn_harness):
    docker_launch = workspace_docker.Launch(
      name='broker-CH',
      command=['x'],
      env={},
      secrets=(),
      tty=False,
      forward_env=False,
      image='runtime-image',
      runtime_bundle_hash='bundle-hash',
    )
    launch = workspace_spawn.DockerLaunchSpec(docker_launch)
    provisioned = workspace_spawn.Provisioned(
      channel='CH', host_endpoint=Endpoint(port=7321, token='tk')
    )
    handle = await workspace_spawn.DockerSpawner().spawn(launch, provisioned, 'X-1')
    loop_thread = threading.get_ident()
    assert spawn_harness['prepare_threads'][0] != loop_thread
    assert spawn_harness['workspace_threads'][0] != loop_thread
    assert await handle.wait() == 0
