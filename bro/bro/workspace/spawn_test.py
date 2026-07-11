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

import cw.constants
import cw.docker
import cw.paths
import cw.secrets
import cw.spawn
import cw.summon
import cw.workspace


class TestDockerLaunchSpec:
  def test_default_ring_bytes_is_64_kib(self):
    assert cw.spawn.DEFAULT_RING_BYTES == 64 * 1024

  def test_defaults(self):
    launch = cw.spawn.DockerLaunchSpec(command=['x'], env={}, secrets=(), attached=False)
    assert launch.ring_bytes == cw.spawn.DEFAULT_RING_BYTES
    assert launch.bro is None
    assert launch.name is None
    assert launch.optional_secrets == ()
    # child-safe defaults: no host docker socket, no ambient CW_BRO leak
    assert launch.docker_sock is False
    assert launch.forward_bro is False


class TestRingBuffer:
  def test_under_cap_keeps_everything(self):
    ring = cw.spawn._RingBuffer(100)
    ring.write(b'hello')
    ring.write(b' world')
    assert ring.tail() == b'hello world'

  def test_over_cap_keeps_last_bytes(self):
    ring = cw.spawn._RingBuffer(4)
    ring.write(b'abcdefgh')
    assert ring.tail() == b'efgh'

  def test_trims_across_writes(self):
    ring = cw.spawn._RingBuffer(4)
    ring.write(b'abc')
    ring.write(b'de')
    assert ring.tail() == b'bcde'

  def test_single_write_larger_than_cap(self):
    ring = cw.spawn._RingBuffer(3)
    ring.write(b'abcdefg')
    assert ring.tail() == b'efg'

  def test_exact_cap(self):
    ring = cw.spawn._RingBuffer(4)
    ring.write(b'abcd')
    assert ring.tail() == b'abcd'

  def test_negative_cap_rejected(self):
    with pytest.raises(ValueError):
      cw.spawn._RingBuffer(-1)


class TestBrokerCreateArgv:
  @pytest.fixture
  def build_argv(self, monkeypatch, tmp_path):
    monkeypatch.setattr(cw.docker.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(cw.docker, '_seed_claude_json', lambda d, h, **k: tmp_path / '.claude.json')

    def build(**launch_kwargs):
      launch_kwargs.setdefault('attached', False)
      launch = cw.spawn.DockerLaunchSpec(
        command=['broker', 'recv'], env={}, secrets=(), **launch_kwargs
      )
      return cw.spawn._broker_create_argv(
        launch, '/host/sock.sock', 'broker-X', tmp_path / 'proj', tmp_path / 'sess', 'tag'
      )

    return build

  def test_non_tty(self, build_argv):
    # no -it: a headless supervised child gets no pty
    argv = build_argv()
    assert '-it' not in argv
    assert argv[:2] == ['docker', 'create']

  def test_socket_bind_mounted(self, build_argv):
    argv = build_argv()
    assert '/host/sock.sock:/run/broker.sock' in argv
    assert argv[argv.index('/host/sock.sock:/run/broker.sock') - 1] == '-v'

  def test_broker_channel_env(self, build_argv):
    argv = build_argv()
    assert 'BROKER_CHANNEL=unix:/run/broker.sock' in argv
    assert argv[argv.index('BROKER_CHANNEL=unix:/run/broker.sock') - 1] == '-e'

  def test_bro_role_stamped_into_cw_bro(self, build_argv):
    assert 'CW_BRO=pm' in build_argv(bro='pm')

  def test_no_cw_bro_when_role_absent(self, build_argv):
    assert not any(a.startswith('CW_BRO=') for a in build_argv())

  def test_host_cw_bro_not_forwarded(self, build_argv, monkeypatch):
    # forward_bro=False: the calling session's ambient CW_BRO must not leak into a
    # spawned child — the role arrives only via launch.bro.
    monkeypatch.setenv('CW_BRO', 'ppp-dev')
    assert 'CW_BRO' not in build_argv()

  def test_attached_allocates_tty(self, build_argv):
    assert '-it' in build_argv(attached=True)

  def test_docker_sock_follows_spec(self, build_argv):
    mount = '/var/run/docker.sock:/var/run/docker.sock'
    assert mount not in build_argv()
    assert mount in build_argv(docker_sock=True)

  def test_forward_bro_forwards_ambient_cw_bro(self, build_argv, monkeypatch):
    monkeypatch.setenv('CW_BRO', 'ppp-dev')
    assert 'CW_BRO' in build_argv(attached=True, forward_bro=True)

  def test_child_gets_no_ambient_env_forwards(self, build_argv, monkeypatch):
    # a spawned child's environment is the LaunchSpec snapshot only — forwarding
    # would bake the launcher's task/git/terminal facts into it
    monkeypatch.setenv('CW_TASK_ID', 'task-1')
    monkeypatch.setenv('TERM', 'tmux-256color')
    argv = build_argv()
    assert 'CW_TASK_ID' not in argv
    assert 'TERM' not in argv

  def test_attached_root_keeps_ambient_env_forwards(self, build_argv, monkeypatch):
    # the attached root is the launcher's own session, so the forwards are its own
    monkeypatch.setenv('CW_TASK_ID', 'task-1')
    assert 'CW_TASK_ID' in build_argv(attached=True)


class TestHostLogRedirect:
  def test_noop_when_stderr_is_not_a_tty(self, tmp_path):
    # pytest's captured fds are pipes, so the gate sees no terminal
    redirect = cw.spawn._HostLogRedirect(tmp_path / 'log' / 's.log')
    redirect.flip()
    os.write(2, b'stays on stderr\n')
    redirect.restore()
    assert not (tmp_path / 'log' / 's.log').exists()

  def test_flip_routes_both_fds_and_restore_returns_them(self, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(os, 'isatty', lambda fd: True)
    host_log = tmp_path / 'log' / 'c:ws.log'
    redirect = cw.spawn._HostLogRedirect(host_log)
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
    redirect = cw.spawn._HostLogRedirect(tmp_path / 's.log')
    redirect.flip()
    redirect.restore()
    assert not any('session host log' in record.message for record in caplog.records)

  def test_pointer_line_counts_only_the_current_span(self, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(os, 'isatty', lambda fd: True)
    host_log = tmp_path / 's.log'
    host_log.write_text('previous session line\n')
    redirect = cw.spawn._HostLogRedirect(host_log)
    redirect.flip()
    os.write(2, b'fresh line\n')
    redirect.restore()
    assert any('(1 line this session)' in record.message for record in caplog.records)

  def test_restore_without_flip_is_a_noop(self, tmp_path):
    cw.spawn._HostLogRedirect(tmp_path / 's.log').restore()


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

    monkeypatch.setattr(cw.spawn, '_force_remove', fake_remove)
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
    root = cw.spawn._AttachedRoot('cid', process)
    assert signal.getsignal(signal.SIGINT) is not signal.default_int_handler
    root._forward_sigint()
    assert await root.wait() == 42
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler

  @pytest.mark.asyncio
  async def test_forward_after_exit_is_noop(self):
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', 'pass')
    root = cw.spawn._AttachedRoot('cid', process)
    assert await root.wait() == 0
    root._forward_sigint()  # process gone; must not raise

  @pytest.mark.asyncio
  async def test_wait_removes_the_container(self, removed):
    # the client can die while the container lives (sig-proxy is off on a tty attach),
    # so client exit must always be followed by container teardown
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', 'pass')
    root = cw.spawn._AttachedRoot('cid', process)
    await root.wait()
    assert removed == ['cid']

  @pytest.mark.asyncio
  async def test_output_tail_is_empty(self):
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', 'pass')
    root = cw.spawn._AttachedRoot('cid', process)
    await root.wait()
    assert root.output_tail() == ''

  @pytest.mark.asyncio
  async def test_host_output_redirected_for_the_attached_span(self, tmp_path, monkeypatch):
    monkeypatch.setattr(os, 'isatty', lambda fd: True)
    host_log = tmp_path / 'c:ws.log'
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', 'pass')
    root = cw.spawn._AttachedRoot('cid', process, host_log=host_log)
    os.write(2, b'mid-session line\n')
    await root.wait()
    os.write(2, b'post-session line\n')
    content = host_log.read_text()
    assert 'mid-session line' in content
    assert 'post-session line' not in content


class TestDockerChildCapture:
  async def _child(self, code: str, ring_bytes: int) -> cw.spawn._DockerChild:
    # the same stream wiring DockerSpawner uses: stderr merged into the stdout pipe
    process = await asyncio.create_subprocess_exec(
      sys.executable,
      '-c',
      code,
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.STDOUT,
    )
    return cw.spawn._DockerChild('cid', process, ring_bytes, workspace=None)

  @pytest.mark.asyncio
  async def test_tail_combines_stdout_and_stderr(self):
    code = 'import sys; print("out-line"); print("err-line", file=sys.stderr)'
    child = await self._child(code, cw.spawn.DEFAULT_RING_BYTES)
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
  def _workspace(self, monkeypatch, tmp_path, removed: list) -> cw.workspace.ContainerWorkspace:
    workspace = cw.workspace.ContainerWorkspace('broker-CH', tmp_path / 'proj')
    monkeypatch.setattr(workspace, 'remove', lambda: removed.append(workspace.name))
    return workspace

  async def _child(self, workspace) -> cw.spawn._DockerChild:
    process = await asyncio.create_subprocess_exec(
      sys.executable,
      '-c',
      'pass',
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.STDOUT,
    )
    return cw.spawn._DockerChild('cid', process, cw.spawn.DEFAULT_RING_BYTES, workspace)

  @pytest.mark.asyncio
  async def test_wait_removes_throwaway_workspace(self, monkeypatch, tmp_path):
    removed: list = []
    child = await self._child(self._workspace(monkeypatch, tmp_path, removed))
    assert await child.wait() == 0
    assert removed == ['broker-CH']

  @pytest.mark.asyncio
  async def test_kill_removes_throwaway_workspace(self, monkeypatch, tmp_path):
    async def fake_remove(container_id):
      pass

    monkeypatch.setattr(cw.spawn, '_force_remove', fake_remove)
    removed: list = []
    child = await self._child(self._workspace(monkeypatch, tmp_path, removed))
    await child.kill()
    assert removed == ['broker-CH']
    # the timeout path kills, then the attach exits: wait() must not remove again
    await child.wait()
    assert removed == ['broker-CH']

  @pytest.mark.asyncio
  async def test_removal_failure_warns_instead_of_raising(self, monkeypatch, tmp_path):
    workspace = cw.workspace.ContainerWorkspace('broker-CH', tmp_path / 'proj')

    def boom():
      raise RuntimeError('root-owned files')

    monkeypatch.setattr(workspace, 'remove', boom)
    warnings: list = []
    monkeypatch.setattr(cw.spawn.log, 'warning', lambda msg, *args: warnings.append(msg % args))
    child = await self._child(workspace)
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
    handle = cw.spawn._AttachedProcess(await self._interruptible())
    assert signal.getsignal(signal.SIGINT) is not signal.default_int_handler
    handle._forward_sigint()
    assert await handle.wait() == 42
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler

  @pytest.mark.asyncio
  async def test_kill_terminates_a_live_process(self):
    process = await asyncio.create_subprocess_exec(
      sys.executable, '-c', 'import time; time.sleep(30)'
    )
    handle = cw.spawn._AttachedProcess(process)
    await handle.kill()
    assert await handle.wait() == -signal.SIGKILL

  @pytest.mark.asyncio
  async def test_kill_after_exit_is_noop(self):
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', 'pass')
    handle = cw.spawn._AttachedProcess(process)
    assert await handle.wait() == 0
    await handle.kill()  # process gone; must not raise

  @pytest.mark.asyncio
  async def test_output_tail_is_empty(self):
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', 'pass')
    handle = cw.spawn._AttachedProcess(process)
    await handle.wait()
    assert handle.output_tail() == ''


class TestProcessSpawner:
  async def _spawn(self, command, cwd, env) -> cw.spawn.ChildHandle:
    launch = cw.spawn.ProcessLaunchSpec(command=command, cwd=cwd, env=env)
    provisioned = cw.spawn.Provisioned(channel='CH', host_endpoint='/host/CH.sock')
    return await cw.spawn.ProcessSpawner().spawn(launch, provisioned)

  @pytest.mark.asyncio
  async def test_env_is_the_spec_snapshot_plus_broker_channel(self, monkeypatch, tmp_path):
    monkeypatch.setenv('CW_AMBIENT_CANARY', 'leak')
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
    assert 'CW_AMBIENT_CANARY' not in env

  @pytest.mark.asyncio
  async def test_runs_in_cwd_and_propagates_exit_code(self, tmp_path):
    code = 'open("here", "w"); raise SystemExit(7)'
    handle = await self._spawn([sys.executable, '-c', code], str(tmp_path), {})
    assert await handle.wait() == 7
    assert (tmp_path / 'here').is_file()


class TestCompositeSpawner:
  class _Recording(cw.spawn.Spawner):
    def __init__(self):
      self.spawned: list = []

    async def spawn(self, launch, channel) -> cw.spawn.ChildHandle:
      self.spawned.append(launch)
      return MagicMock()

  @pytest.mark.asyncio
  async def test_dispatches_on_launch_spec_type(self):
    docker, process = self._Recording(), self._Recording()
    composite = cw.spawn.CompositeSpawner(
      {cw.spawn.DockerLaunchSpec: docker, cw.spawn.ProcessLaunchSpec: process}
    )
    channel = cw.spawn.Provisioned(channel='CH', host_endpoint='/host/CH.sock')
    docker_launch = cw.spawn.DockerLaunchSpec(command=['x'], env={}, secrets=(), attached=False)
    process_launch = cw.spawn.ProcessLaunchSpec(command=['x'], cwd='/', env={})
    await composite.spawn(docker_launch, channel)
    await composite.spawn(process_launch, channel)
    assert docker.spawned == [docker_launch]
    assert process.spawned == [process_launch]

  @pytest.mark.asyncio
  async def test_unregistered_type_raises(self):
    composite = cw.spawn.CompositeSpawner({})
    channel = cw.spawn.Provisioned(channel='CH', host_endpoint='/host/CH.sock')
    launch = cw.spawn.ProcessLaunchSpec(command=['x'], cwd='/', env={})
    with pytest.raises(ValueError, match='ProcessLaunchSpec'):
      await composite.spawn(launch, channel)


PARENT_WORKSPACE = Path('/var/cw/worktrees/parent')


class TestSummonLowering:
  @pytest.fixture
  def lowering_harness(self, monkeypatch, tmp_path):
    monkeypatch.setattr(cw.spawn, '_project_root', lambda: tmp_path / 'proj')
    monkeypatch.setattr(
      cw.spawn,
      'bro_run_secrets',
      lambda name: cw.secrets.ScopedSecrets(
        required={'aws', 'trails'}, optional={'openai'}, docker_sock=True
      ),
    )
    monkeypatch.setattr(
      cw.spawn,
      'resolve_head',
      lambda root, repository: 'PARENT-SHA' if repository == PARENT_WORKSPACE else None,
    )
    monkeypatch.setattr(
      cw.spawn, 'resolve_ref', lambda root, ref: 'REF-SHA' if ref == 'summon' else None
    )

  def test_lowers_to_the_ask_docker_launch(self, lowering_harness):
    launch = cw.spawn.SummonLaunchSpec(
      target='devoops', prompt='deploy the thing', parent_workspace=PARENT_WORKSPACE
    )
    lowered = cw.spawn._lower_summon(launch)
    assert lowered == cw.spawn.DockerLaunchSpec(
      command=['ask', 'devoops', 'deploy the thing'],
      env={'CW_BASE_REF': 'PARENT-SHA', **cw.constants.bro_git_identity_env()},
      secrets={'aws', 'trails'},
      attached=False,
      optional_secrets={'openai'},
      docker_sock=True,
    )
    # child-safe defaults hold: throwaway workspace, no CW_BRO leak, env snapshot only
    assert lowered.name is None
    assert lowered.forward_bro is False

  def test_into_overrides_the_inherited_base_ref(self, lowering_harness):
    launch = cw.spawn.SummonLaunchSpec(
      target='devoops', prompt='p', parent_workspace=PARENT_WORKSPACE, into='summon'
    )
    assert cw.spawn._lower_summon(launch).env == {
      'CW_BASE_REF': 'REF-SHA',
      **cw.constants.bro_git_identity_env(),
    }

  def test_unresolvable_into_fails_the_spawn(self, lowering_harness):
    launch = cw.spawn.SummonLaunchSpec(
      target='devoops', prompt='p', parent_workspace=PARENT_WORKSPACE, into='nope'
    )
    with pytest.raises(ValueError, match='nope'):
      cw.spawn._lower_summon(launch)

  def test_unreadable_parent_head_fails_the_spawn(self, lowering_harness):
    launch = cw.spawn.SummonLaunchSpec(target='devoops', prompt='p', parent_workspace=Path('/gone'))
    with pytest.raises(ValueError, match="summoner's HEAD"):
      cw.spawn._lower_summon(launch)

  @pytest.mark.asyncio
  async def test_spawner_lowers_off_loop_and_delegates_to_docker(self, lowering_harness):
    class RecordingDocker(cw.spawn.DockerSpawner):
      def __init__(self):
        self.spawned: list = []

      async def spawn(self, launch, channel):
        self.spawned.append((launch, channel))
        return MagicMock()

    docker = RecordingDocker()
    spawner = cw.spawn.SummonSpawner(docker)
    channel = cw.spawn.Provisioned(channel='CH', host_endpoint='/host/CH.sock')
    launch = cw.spawn.SummonLaunchSpec(
      target='devoops', prompt='p', parent_workspace=PARENT_WORKSPACE
    )
    await spawner.spawn(launch, channel)
    [(lowered, lowered_channel)] = docker.spawned
    assert isinstance(lowered, cw.spawn.DockerLaunchSpec)
    assert lowered.command == ['ask', 'devoops', 'p']
    assert lowered_channel is channel

  @pytest.mark.asyncio
  async def test_lowering_failure_propagates_out_of_spawn(self, lowering_harness):
    spawner = cw.spawn.SummonSpawner(cw.spawn.DockerSpawner())
    channel = cw.spawn.Provisioned(channel='CH', host_endpoint='/host/CH.sock')
    launch = cw.spawn.SummonLaunchSpec(
      target='devoops', prompt='p', parent_workspace=PARENT_WORKSPACE, into='nope'
    )
    # the raise crosses to_thread back onto the loop: Dispatcher.spawn turns it
    # into the correlated failed{reason: 'launch'}
    with pytest.raises(ValueError, match='nope'):
      await spawner.spawn(launch, channel)


class TestRunRootViaBroker:
  def test_wires_control_dir_composite_spawner_handlers_and_run(self, monkeypatch, tmp_path):
    captured: dict = {}

    class FakeBroker:
      def __init__(self, transport, spawner, **kwargs):
        captured['transport'] = transport
        captured['spawner'] = spawner
        captured['handlers'] = {}
        captured['observers'] = []

      def on(self, message_type, handler):
        captured['handlers'][message_type] = handler

      def add_delivery_observer(self, observer):
        captured['observers'].append(observer)

      def run(self, launch):
        captured['launch'] = launch
        return 3

    monkeypatch.setattr(cw.spawn, 'Broker', FakeBroker)
    launch = cw.spawn.ProcessLaunchSpec(command=['x'], cwd='/', env={})
    assert cw.spawn.run_root_via_broker(launch, tmp_path / 'proj', session='ws') == 3
    assert captured['transport']._dir == tmp_path / 'proj' / 'var' / 'cw' / 'broker'
    # the composite over both launch modes plus the summon lowering: any root can
    # spawn docker children, summons included
    spawner = captured['spawner']
    assert isinstance(spawner, cw.spawn.CompositeSpawner)
    docker_spawner = spawner._spawners[cw.spawn.DockerLaunchSpec]
    process_spawner = spawner._spawners[cw.spawn.ProcessLaunchSpec]
    assert isinstance(docker_spawner, cw.spawn.DockerSpawner)
    assert isinstance(process_spawner, cw.spawn.ProcessSpawner)
    assert isinstance(spawner._spawners[cw.spawn.SummonLaunchSpec], cw.spawn.SummonSpawner)
    # both attached-capable spawners point at the same per-session host log
    host_log = tmp_path / 'proj' / 'var' / 'cw' / 'log' / 'ws.log'
    assert docker_spawner._host_log == host_log
    assert process_spawner._host_log == host_log
    assert set(captured['handlers']) == {'ping', 'started', 'completed', 'summon'}
    assert captured['handlers']['ping'] is cw.spawn.ping_handler
    # the root's own run lifecycle has no parent peer; the host logs it as the parent
    assert captured['handlers']['started'] is cw.spawn._log_root_started
    assert captured['handlers']['completed'] is cw.spawn._log_root_completed
    # the summon handler and the delivery tap belong to the same per-root control
    control = captured['handlers']['summon'].__self__
    assert isinstance(control, cw.summon.SummonControl)
    assert [observer.__self__ for observer in captured['observers']] == [control]
    assert control._session == 'ws'
    summon_dir = tmp_path / 'proj' / 'var' / 'cw' / 'summon'
    assert control._status_file == summon_dir / 'ws.status.json'
    assert control._audit_file == summon_dir / 'ws.jsonl'
    assert captured['launch'] is launch

  def test_root_lifecycle_handlers_log_trail_and_end_reason(self, caplog):
    from broker.brotocol import Message, Tag
    from broker.dispatcher import Dispatcher

    dispatcher = Dispatcher()
    cw.spawn._log_root_started(
      dispatcher, 'root', Message(type=Tag.STARTED, payload={'trail_id': 't-1'})
    )
    cw.spawn._log_root_completed(
      dispatcher,
      'root',
      Message(type=Tag.COMPLETED, payload={'result': 'ok', 'end_reason': 'terminal'}),
    )
    assert any('root run started (trail t-1)' in record.message for record in caplog.records)
    assert any('root run ended: terminal' in record.message for record in caplog.records)


class TestDockerSpawnerModes:
  @pytest.fixture
  def spawn_harness(self, monkeypatch, tmp_path):
    monkeypatch.setattr(cw.spawn, '_project_root', lambda: tmp_path / 'proj')
    monkeypatch.setattr(cw.spawn, '_image_tag', lambda: 'tag')
    monkeypatch.setattr(cw.spawn, '_ensure_image', lambda tag: None)
    monkeypatch.setattr(cw.spawn, '_ppp_tarball', lambda store: b'TARBALL')
    stores: list = []

    def fake_store(names, optional=()):
      stores.append({'names': names, 'optional': optional})
      return {}

    monkeypatch.setattr(cw.spawn.credentials, 'build_scoped_store', fake_store)
    monkeypatch.setattr(
      cw.spawn, '_broker_create_argv', lambda *a, **k: ['docker', 'create', 'ARGS']
    )
    created: list = []
    threads: list = []

    def fake_create(argv, tarball, name):
      created.append({'argv': argv, 'tarball': tarball, 'name': name})
      threads.append(threading.get_ident())
      return 'cid123'

    monkeypatch.setattr(cw.spawn, '_create_container', fake_create)

    async def fake_remove(container_id):
      pass

    monkeypatch.setattr(cw.spawn, '_force_remove', fake_remove)
    starts: list = []
    real_exec = asyncio.create_subprocess_exec

    def fake_exec(*argv, **kwargs):
      starts.append(list(argv))
      # a real (trivial) child process stands in for the docker client
      return real_exec(sys.executable, '-c', 'pass', **kwargs)

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', fake_exec)
    return {
      'stores': stores,
      'created': created,
      'starts': starts,
      'threads': threads,
      'tmp': tmp_path,
    }

  @pytest.mark.asyncio
  async def test_attached_root_mode(self, spawn_harness):
    launch = cw.spawn.DockerLaunchSpec(
      command=['claude'],
      env={},
      secrets=('github',),
      attached=True,
      name='ws',
      optional_secrets=('openai',),
    )
    provisioned = cw.spawn.Provisioned(channel='CH', host_endpoint='/host/CH.sock')
    handle = await cw.spawn.DockerSpawner().spawn(launch, provisioned)
    try:
      assert isinstance(handle, cw.spawn._AttachedRoot)
      assert handle.output_tail() == ''
      # the named workspace backs the container; no broker-<channel> throwaway
      assert (spawn_harness['tmp'] / 'proj' / 'var' / 'cw' / 'containers' / 'ws').is_dir()
      assert spawn_harness['created'] == [
        {'argv': ['docker', 'create', 'ARGS'], 'tarball': b'TARBALL', 'name': 'ws'}
      ]
      assert spawn_harness['stores'] == [{'names': ('github',), 'optional': ('openai',)}]
      assert spawn_harness['starts'] == [['docker', 'start', '-a', '-i', 'cid123']]
    finally:
      await handle.wait()  # reap + restore the SIGINT handler

  @pytest.mark.asyncio
  async def test_child_mode_derives_workspace_from_channel(self, spawn_harness):
    launch = cw.spawn.DockerLaunchSpec(
      command=['broker', 'recv'], env={}, secrets=(), attached=False
    )
    provisioned = cw.spawn.Provisioned(channel='CH', host_endpoint='/host/CH.sock')
    handle = await cw.spawn.DockerSpawner().spawn(launch, provisioned)
    assert isinstance(handle, cw.spawn._DockerChild)
    assert (spawn_harness['tmp'] / 'proj' / 'var' / 'cw' / 'containers' / 'broker-CH').is_dir()
    assert spawn_harness['starts'] == [['docker', 'start', '-a', 'cid123']]
    assert await handle.wait() == 0

  @pytest.mark.asyncio
  async def test_blocking_prepare_runs_off_the_loop_thread(self, spawn_harness):
    # image ensure / store build / docker create can block for minutes (a mid-session
    # image rebuild); they must run via to_thread, not on the broker loop
    launch = cw.spawn.DockerLaunchSpec(command=['x'], env={}, secrets=(), attached=False)
    provisioned = cw.spawn.Provisioned(channel='CH', host_endpoint='/host/CH.sock')
    handle = await cw.spawn.DockerSpawner().spawn(launch, provisioned)
    assert len(spawn_harness['threads']) == 1
    assert spawn_harness['threads'][0] != threading.get_ident()
    assert await handle.wait() == 0
