import asyncio
import contextlib
import shlex
import threading
import time
from pathlib import Path
from typing import ClassVar, Optional
from unittest.mock import MagicMock

import pytest

import bro.mcp as mcp
import bro.native.harness as bro_harness
import ride.session as ride_session
from bro import brash_policy
from bro.brash import REFUSED_STATUS
from bro.bro import AnswerDelivered, BaseBro, BroRaised
from bro.inbox import Inbox
from bro.jobs import Job, Registry
from bro.llm.llms.openai import LLMSpec
from bro.llm.mcp import MCPServer, Tool
from bro.monitor import SESSION_DIR_ENV, trail_pointer, workspace_party_dir, workspace_session_dir
from bro.native import dev_mcp
from bro.workspace.paths import CONTAINER_PARTY_DIR, CONTAINER_SESSION_DIR
from ride.do_ride import SessionRun
from ride.runtime_bundle import RuntimeBundle
from ride.session import ScopedLaunch, SessionSpec
from ride.workspace.docker import ContainerRuntime, ContainerRuntimeResolver
from ride.workspace.metadata import Isolation
from ride.workspace.model import Workspace
from ride.workspace.store import ScopedSecrets


def _materialize_store(_store, directory: Path) -> Path:
  """The empty scoped store a host launch materializes."""
  directory.mkdir(parents=True, exist_ok=True)
  (directory / 'creds.json').write_text('{}')
  return directory


def _spec(**overrides) -> SessionSpec:
  values = {
    'name': 'w',
    'harness': 'bro',
    'workspace_pinned': True,
    'isolation': Isolation.BOXED,
    'drop': False,
    'hold': 'attended',
    'cred': [],
    'grant': [],
    'revoke': [],
    'llm': None,
    'resolved_llm': LLMSpec().dump(),
    'solo': False,
    'resume': False,
    'no_trails': False,
    'into': None,
    'bro': 'dev',
    'prompt': 'start here',
    'subject': 'start here',
  }
  values.update(overrides)
  return SessionSpec(**values)


_ACTIVITY_FILE = Path('/session/activity')


def _run(**overrides) -> SessionRun:
  spec = _spec(**overrides)
  return SessionRun(
    name=spec.name,
    repo=spec.repo,
    harness=spec.harness,
    hold=spec.hold,
    llm=spec.llm,
    resolved_llm=spec.resolved_llm,
    solo=spec.solo,
    resume=spec.resume,
    bro=spec.bro,
    prompt=spec.prompt,
    activity_file=_ACTIVITY_FILE,
  )


def _runtime_bundle(tmp_path: Path) -> RuntimeBundle:
  return RuntimeBundle(tmp_path / 'runtime-bundle', '3.12')


def _container_runtime() -> ContainerRuntimeResolver:
  return ContainerRuntimeResolver.fixed(
    ContainerRuntime('runtime-image', 'bundle-hash', 'runtime-image')
  )


def _scope(**overrides) -> ScopedLaunch:
  values = {
    'scoped': ScopedSecrets({'openai'}, {'trails'}),
    'launch': {'bro': {'bros': frozenset({'reviewer'}), 'party': frozenset({'boxed'})}},
    'store': {'creds.json': b'{}'},
  }
  values.update(overrides)
  return ScopedLaunch(**values)


@pytest.fixture(autouse=True)
def local_trails(monkeypatch):
  # keep the launch composition off the machine's own trails credential
  monkeypatch.setattr(ride_session, 'local_trails_mounts', lambda scoped: ())


def test_prepare_session_has_no_native_state_side_effects():
  bro_harness.BRO.prepare_session(MagicMock())


def test_native_harness_can_end_every_session():
  assert bro_harness.BRO.can_end_session()


@pytest.mark.asyncio
async def test_native_harness_delivers_an_answer_to_its_runner():
  with pytest.raises(AnswerDelivered) as exception:
    await bro_harness.BRO.end_session('the verdict', 'ok')
  assert exception.value.answer == 'the verdict'


@pytest.mark.asyncio
async def test_native_harness_raises_to_its_runner():
  with pytest.raises(BroRaised) as exception:
    await bro_harness.BRO.end_session('missing api key', 'raised')
  assert exception.value.reason == 'missing api key'


def test_check_runtime_starts_the_sibling_native_command_without_path(monkeypatch, tmp_path):
  executable = tmp_path / 'venv' / 'bin' / 'bro'
  executable.parent.mkdir(parents=True)
  executable.touch()
  run = MagicMock()
  monkeypatch.setattr(bro_harness.spawn, 'console_script', lambda name: str(executable))
  monkeypatch.setenv('PATH', '/usr/bin:/bin')
  monkeypatch.setattr(bro_harness.subprocess, 'run', run)

  bro_harness.BRO.check_runtime()

  run.assert_called_once_with([str(executable), '--help'], check=True)


class TestNativeArgv:
  """The argv the bro harness runner spawns."""

  def _argv(self, spec, monkeypatch) -> list[str]:
    spawned: list[list[str]] = []
    monkeypatch.setattr(bro_harness.spawn, 'console_script', lambda name: '/venv/bin/bro')
    monkeypatch.setattr('ride.do_ride.run_agent', lambda argv: spawned.append(argv) or 0)
    assert bro_harness.BRO.run_session(spec) == 0
    return spawned[0]

  def _session_dir(self, monkeypatch, tmp_path) -> Path:
    workspace = Workspace.create('w', tmp_path, Isolation.BOXED)
    session = workspace_session_dir(workspace.path)
    monkeypatch.setenv(SESSION_DIR_ENV, str(session))
    return session

  def test_chat_composes_the_native_argv(self, monkeypatch):
    run = _run(llm='openai:fable:high+fast')
    assert self._argv(run, monkeypatch) == [
      '/venv/bin/bro',
      'chat',
      '--llm',
      'openai:fable:high+fast',
      '--hold',
      'attended',
      '--activity-file',
      str(_ACTIVITY_FILE),
      '--',
      'dev',
      'start here',
    ]

  def test_a_prompt_that_reads_as_a_flag_reaches_bro_run_as_its_input(self, monkeypatch):
    import bro.run as bro_run

    argv = self._argv(_run(solo=True, hold='unattended', prompt='--hold=guided'), monkeypatch)
    runs: list[tuple[str, str]] = []

    class _Runner:
      def __init__(self, bro, *, activity_file) -> None:
        del bro, activity_file

      async def run(self, text: str, **options) -> None:
        runs.append((text, options['hold']))

    monkeypatch.setattr('bro.launch.run.create_bro_for_run', lambda name, selection: MagicMock())
    monkeypatch.setattr('bro.launch.run._ask_observer', lambda name: None)
    monkeypatch.setattr('bro.launch.run.Runner', _Runner)
    monkeypatch.setattr('bro.launch.broxy.session_broxy', contextlib.nullcontext)
    bro_run.main(argv)

    assert runs == [('--hold=guided', 'unattended')]

  def test_resume_carries_the_session_trail_and_recorded_recipe(self, monkeypatch, tmp_path):
    session = self._session_dir(monkeypatch, tmp_path)
    trail_pointer.write(session / trail_pointer.FILENAME, 'trail-1')
    argv = self._argv(_run(resume=True, prompt=None), monkeypatch)
    assert argv[:2] == ['/venv/bin/bro', 'chat'] and argv[-2:] == ['--', 'dev']
    assert argv[argv.index('--continue-trail') + 1] == 'trail-1'
    assert '"type":"openai"' in argv[argv.index('--continue-llm') + 1]

  def test_resume_without_a_published_pointer_fails(self, monkeypatch, tmp_path, caplog):
    self._session_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(bro_harness.spawn, 'console_script', lambda name: '/venv/bin/bro')
    assert bro_harness.BRO.run_session(_run(resume=True)) == 1
    assert 'no bro harness trail recorded' in caplog.text

  def test_missing_native_distribution_fails_before_spawn(self, monkeypatch, caplog):
    run_agent = MagicMock()
    monkeypatch.setattr(
      bro_harness.spawn,
      'console_script',
      MagicMock(side_effect=FileNotFoundError('native missing')),
    )
    monkeypatch.setattr('ride.do_ride.run_agent', run_agent)

    assert bro_harness.BRO.run_session(_run()) == 1
    assert 'native missing' in caplog.text
    run_agent.assert_not_called()


class TestContainerSession:
  def test_composes_the_bro_run_launch(self, monkeypatch, tmp_path):
    workspace = Workspace.create('w', tmp_path, Isolation.BOXED)
    captured: dict = {}
    monkeypatch.setattr(ride_session, 'find_container_id', lambda _tree: None)

    def run(launch, run_workspace, **kwargs):
      captured['launch'] = launch
      captured['workspace'] = run_workspace
      captured.update(kwargs)
      return 7

    monkeypatch.setattr(ride_session, 'run_started_party', run)
    spec = _spec(solo=True, hold='unattended')
    assert (
      ride_session._launch_session(
        spec,
        workspace,
        'abc123',
        _scope(),
        human_env={},
        runtime_bundle=_runtime_bundle(tmp_path),
        container_runtime=_container_runtime(),
      )
      == 7
    )
    launch = captured['launch']
    assert launch.command == [
      'do-ride', 'solo', '--workspace', 'w', '--harness', 'bro',
      '--hold', 'unattended', '--', 'dev', 'start here',
    ]  # fmt: skip
    assert launch.env == {
      'RIDE_BRO': 'dev',
      'RIDE_COMMAND': spec.ride_command,
      'RIDE_ISOLATION': 'boxed',
      'RIDE_BRANCH': 'workspace-w',
      'RIDE_BASE_SHA': 'abc123',
      ride_session.RUNTIME_ENV: str(_runtime_bundle(tmp_path).host_root),
      ride_session.INSTALL_DIRECTORY_ENV: ride_session.CONTAINER_INSTALL_DIRECTORY,
      ride_session.RESOLVED_LLM_ENV: ride_session.encode_resolved_llm(spec.resolved_llm),
      'RIDE_SESSION_DIR': str(CONTAINER_SESSION_DIR),
    }
    assert launch.base_ref == 'abc123'
    assert not launch.tty
    assert captured['workspace'] is workspace
    assert captured['launch_scope'] == _scope().launch

  def test_no_trails_disables_recording_in_the_container_env(self, monkeypatch, tmp_path):
    workspace = Workspace.create('w', tmp_path, Isolation.BOXED)
    captured: dict = {}
    monkeypatch.setattr(ride_session, 'find_container_id', lambda _tree: None)
    monkeypatch.setattr(
      ride_session,
      'run_started_party',
      lambda launch, *_a, **_k: captured.update(launch=launch) or 0,
    )
    spec = _spec(no_trails=True)
    assert (
      ride_session._launch_session(
        spec,
        workspace,
        'abc123',
        _scope(),
        human_env={},
        runtime_bundle=_runtime_bundle(tmp_path),
        container_runtime=_container_runtime(),
      )
      == 0
    )
    assert captured['launch'].env['TRAILS_DISABLED'] == '1'
    # the session state dir is not trails data — it stays mounted
    assert captured['launch'].extra_mounts == (
      f'{workspace_session_dir(workspace.path)}:{CONTAINER_SESSION_DIR}',
      f'{workspace_party_dir(workspace.path)}:{CONTAINER_PARTY_DIR}',
    )

  def test_resume_refuses_without_a_broker_published_pointer(self, caplog, tmp_path):
    workspace = Workspace.create('w', tmp_path, Isolation.BOXED)
    assert (
      ride_session._launch_session(
        _spec(resume=True, prompt=None),
        workspace,
        'abc123',
        _scope(),
        human_env={},
        runtime_bundle=_runtime_bundle(tmp_path),
        container_runtime=_container_runtime(),
      )
      == 1
    )
    assert 'no trail pointer was published' in caplog.text

  def test_fresh_session_clears_a_stale_pointer(self, monkeypatch, tmp_path):
    workspace = Workspace.create('w', tmp_path, Isolation.BOXED)
    pointer = trail_pointer.session_pointer(workspace.path)
    trail_pointer.write(pointer, 'stale')
    monkeypatch.setattr(ride_session, 'find_container_id', lambda _tree: None)
    monkeypatch.setattr(ride_session, 'run_started_party', lambda *_a, **_k: 0)
    assert (
      ride_session._launch_session(
        _spec(),
        workspace,
        'abc123',
        _scope(),
        human_env={},
        runtime_bundle=_runtime_bundle(tmp_path),
        container_runtime=_container_runtime(),
      )
      == 0
    )
    assert not pointer.exists()

  def test_a_refused_second_launch_leaves_the_active_pointer_alone(self, monkeypatch, tmp_path):
    workspace = Workspace.create('w', tmp_path, Isolation.BOXED)
    pointer = trail_pointer.session_pointer(workspace.path)
    trail_pointer.write(pointer, 'live')
    monkeypatch.setattr(ride_session, 'find_container_id', lambda _tree: 'active')
    assert (
      ride_session._launch_session(
        _spec(),
        workspace,
        'abc123',
        _scope(),
        human_env={},
        runtime_bundle=_runtime_bundle(tmp_path),
        container_runtime=_container_runtime(),
      )
      == 1
    )
    assert trail_pointer.read(pointer) == 'live'


class TestUnboxedSession:
  def test_builder_prepares_the_native_snapshot_runner(self, monkeypatch, tmp_path):
    workspace = Workspace.create('w', tmp_path, Isolation.UNBOXED)
    root = tmp_path / 'runtime-bundle'
    (root / 'host' / 'venv' / 'bin').mkdir(parents=True)
    (root / 'host' / 'bin').mkdir()
    (root / 'host' / '.complete').touch()
    (root / 'host' / 'venv' / 'bin' / 'do-ride').touch()
    runtime_bundle = RuntimeBundle(root, '3.12')
    monkeypatch.setattr(ride_session, 'ensure_clone', lambda *_args: True)
    monkeypatch.setattr(ride_session, 'rev_parse_commit', lambda tree, ref: 'treehead')
    monkeypatch.setattr(ride_session, 'provision_workspace', lambda *_args: True)
    monkeypatch.setattr(ride_session, 'materialize_scoped_store', _materialize_store)

    launch = ride_session.started_party_launch(
      _spec(isolation=Isolation.UNBOXED, repo=str(tmp_path)),
      workspace,
      workspace.repository,
      None,
      _scope(),
      human_env={},
      runtime_bundle=runtime_bundle,
      container_runtime=_container_runtime(),
      env={},
      credential_directory=workspace.path / 'credentials',
      install_directory=workspace.path / 'environment',
    )

    assert isinstance(launch, ride_session.ProcessLaunch)
    assert launch.command == [
      str(runtime_bundle.host_venv / 'bin' / 'do-ride'),
      'along',
      '--workspace',
      'w',
      '--harness',
      'bro',
      '--repo',
      str(tmp_path),
      '--hold',
      'attended',
      '--',
      'dev',
      'start here',
    ]
    assert launch.env['BRO_INSTALL_KINDS'] == ''
    assert launch.env['RIDE_BASE_SHA'] == 'treehead'


def _declaring(*layers: mcp.ToolLayer) -> BaseBro:
  class Declaring(BaseBro):
    name = 'declaring'
    description = 'd'
    tools: ClassVar = list(layers)

    def __init__(self):
      super().__init__(system_prompt='')

  return Declaring()


class TestServe:
  @pytest.mark.asyncio
  async def test_files_mount_the_dev_toolset(self):
    servers = _declaring(mcp.files()).assemble(harness=bro_harness.BRO, include_raise=False)
    [dev] = [server for server in servers if server.namespace == dev_mcp.toolset.namespace]
    assert {tool.name for tool in await dev.list_tools()} == set(dev_mcp.toolset.tool_names)

  @pytest.mark.asyncio
  async def test_read_only_files_mount_only_its_read_only_part(self):
    servers = _declaring(mcp.files(write=False)).assemble(
      harness=bro_harness.BRO, include_raise=False
    )
    [dev] = [server for server in servers if server.namespace == dev_mcp.toolset.namespace]
    assert {tool.name for tool in await dev.list_tools()} == set(dev_mcp.READ_ONLY)

  def test_no_files_mount_no_file_tools(self):
    servers = _declaring().assemble(harness=bro_harness.BRO, include_raise=False)
    assert dev_mcp.toolset.namespace not in {server.namespace for server in servers}

  def test_web_and_delegation_are_left_unserved(self):
    reach = _declaring(mcp.files(), mcp.brash(mcp.ANY), mcp.web(), mcp.delegation()).reach()
    assert bro_harness.BRO.serve(reach).unserved == ('web', 'delegation')


class _ShellBro(BaseBro):
  name = 'job-tools'
  description = 'd'
  tools: ClassVar = [mcp.brash(mcp.ANY)]

  def __init__(self):
    super().__init__(system_prompt='')


class _NativeRun:
  def __init__(self, brash_policy: Optional[Path] = None):
    self.trail_id = None
    self.current_tool_step_id = None
    self.inbox = Inbox()
    self.registry = Registry(self.inbox)
    self.brash_policy = brash_policy


async def _service_tools(
  declaration: BaseBro, run: Optional[_NativeRun] = None
) -> tuple[MCPServer, dict[str, Tool]]:
  servers = declaration.assemble(
    harness=bro_harness.BRO,
    include_raise=False,
    live_run=run,
  )
  server = next(server for server in servers if server.namespace == 'bro')
  return server, {tool.name: tool for tool in await server.list_tools()}


class TestNativeServiceTools:
  @pytest.mark.asyncio
  async def test_harness_owns_skill_alone_without_declared_shell_reach(self):
    class NoShellBro(BaseBro):
      name = 'no-shell'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

    server, tools = await _service_tools(NoShellBro())

    assert 'skill' in tools
    assert {'job', 'poll', 'kill', 'jobs'}.isdisjoint(tools)
    assert server.tool_universe is not None
    assert 'skill' in server.tool_universe
    assert 'job' not in server.tool_universe

  @pytest.mark.asyncio
  async def test_harness_owns_job_tools_for_a_declared_shell(self):
    run = _NativeRun()
    server, tools = await _service_tools(_ShellBro(), run)
    with contextlib.ExitStack() as stack:
      stack.callback(run.registry.close)
      stack.callback(server.close)
      assert {'watch', 'unwatch', 'job', 'poll', 'kill', 'jobs', 'skill'} <= set(tools)
      mode = tools['job'].parameters['properties']['mode']
      assert set(mode['enum']) == {'fg', 'bg'}
      assert server.tool_universe is not None
      assert {'banner', 'watch', 'job', 'skill'} <= set(server.tool_universe)

  @pytest.mark.asyncio
  async def test_skill_loader_is_empty(self):
    run = _NativeRun()
    server, tools = await _service_tools(_ShellBro(), run)
    with contextlib.ExitStack() as stack:
      stack.callback(run.registry.close)
      stack.callback(server.close)
      skill = tools['skill']

      assert skill.parameters['required'] == ['name']
      assert await skill.call({'name': 'third-party'}) == ''
      with pytest.raises(ValueError, match='exactly one'):
        await skill.call({})
      with pytest.raises(ValueError, match='non-empty'):
        await skill.call({'name': ''})

  @pytest.mark.asyncio
  async def test_a_finite_command_list_runs_job_lines_in_brash(self, tmp_path):
    class ListedBro(BaseBro):
      name = 'listed-brash'
      description = 'd'
      tools: ClassVar = [mcp.brash('printf ...')]

      def __init__(self):
        super().__init__(system_prompt='')

    declaration = ListedBro()
    run = _NativeRun(brash_policy.write(tmp_path, declaration.reach()))
    server, tools = await _service_tools(declaration, run)
    with contextlib.ExitStack() as stack:
      stack.callback(run.registry.close)
      stack.callback(server.close)
      listed = await tools['job'].call({'command': 'printf one | printf two', 'mode': 'fg'})
      assert listed == 'exited (code 0)\ntwo'
      # an escaped trailing space is part of the line's last word
      untouched = await tools['job'].call({'command': "printf '<%s>' x\\ ", 'mode': 'fg'})
      assert untouched == 'exited (code 0)\n<x >'
      refused = await tools['job'].call({'command': 'printf one; cat /dev/null', 'mode': 'fg'})
      assert isinstance(refused, str)
      status, output = refused.split('\n', 1)
      assert status == f'exited (code {REFUSED_STATUS})'
      assert output.startswith("brash: refused 'cat'")

  @pytest.mark.asyncio
  async def test_foreground_job_interrupted_by_other_news_becomes_background(self, tmp_path):
    run = _NativeRun()
    server, tools = await _service_tools(_ShellBro(), run)
    release = tmp_path / 'release-foreground-job'
    command = (
      f'printf early; while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.01; done; printf late'
    )
    with contextlib.ExitStack() as stack:
      stack.callback(run.registry.close)
      stack.callback(server.close)
      call = asyncio.create_task(
        tools['job'].call({'command': command, 'mode': 'fg', 'timeout_seconds': 5})
      )
      async with asyncio.timeout(5):
        while len(run.registry.values()) == 0:
          await asyncio.sleep(0.01)
        [foreground_job] = run.registry.values()
        while foreground_job.status().unread_lines == 0:
          await asyncio.sleep(0.01)
      run.registry.start('true', 'bg')
      async with asyncio.timeout(5):
        while foreground_job.status().mode != 'bg':
          await asyncio.sleep(0.01)

      result = await call
      assert isinstance(result, str)
      assert result.startswith('running\nearly')
      assert "continues in bg mode; read on with poll(id='job-1')" in result
      first = run.inbox.drain()
      assert first is not None and 'job-2' in first.job_ids

      release.touch()
      assert await asyncio.to_thread(run.inbox.wait, time.monotonic() + 5, threading.Event())
      second = run.inbox.drain()
      assert second is not None
      assert f'[job-1 bg `{command}` exited (code 0)]' in second.text
      assert 'late' in second.text
      assert run.inbox.drain() is None

  @pytest.mark.asyncio
  async def test_foreground_timeout_becomes_background_and_names_its_clamp(self, monkeypatch):
    run = _NativeRun()
    server, tools = await _service_tools(_ShellBro(), run)
    with contextlib.ExitStack() as stack:
      stack.callback(run.registry.close)
      stack.callback(server.close)
      monkeypatch.setattr(bro_harness, '_JOB_WAIT_CAP_SECONDS', 0.05)
      result = await tools['job'].call({'command': 'sleep 0.3', 'mode': 'fg', 'timeout_seconds': 5})
      assert isinstance(result, str)
      assert result.startswith('running')
      assert "continues in bg mode; read on with poll(id='job-1')" in result
      assert '[timeout_seconds 5 clamped to 0.05]' in result
      assert run.registry.get('job-1').mode == 'bg'

  @pytest.mark.asyncio
  async def test_exit_during_foreground_settlement_is_consumed_once(self, monkeypatch, tmp_path):
    original = Job.settle_foreground
    release = tmp_path / 'release-settlement'

    def settlement_after_the_exit(job: Job, limit: int) -> tuple[str, bool]:
      release.touch()
      assert job.wait_finished(time.monotonic() + 10, threading.Event())
      return original(job, limit)

    monkeypatch.setattr(Job, 'settle_foreground', settlement_after_the_exit)
    run = _NativeRun()
    server, tools = await _service_tools(_ShellBro(), run)
    command = f'while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.01; done; printf done'
    with contextlib.ExitStack() as stack:
      stack.callback(run.registry.close)
      stack.callback(server.close)
      result = await tools['job'].call({'command': command, 'mode': 'fg', 'timeout_seconds': 0.01})
      assert result == 'exited (code 0)\ndone'
      assert run.inbox.drain() is None

  @pytest.mark.asyncio
  async def test_cancelling_foreground_wait_leaves_a_background_job(self):
    run = _NativeRun()
    server, tools = await _service_tools(_ShellBro(), run)
    with contextlib.ExitStack() as stack:
      stack.callback(run.registry.close)
      stack.callback(server.close)
      call = asyncio.create_task(
        tools['job'].call({'command': 'sleep 30', 'mode': 'fg', 'timeout_seconds': 60})
      )
      async with asyncio.timeout(5):
        while len(run.registry.values()) == 0:
          await asyncio.sleep(0.01)
      call.cancel()
      with pytest.raises(asyncio.CancelledError):
        await call
      [job] = run.registry.values()
      assert job.mode == 'bg'
