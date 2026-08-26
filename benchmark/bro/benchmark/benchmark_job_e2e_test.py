"""live check of the same graded trial started the way a managed session starts it.

What this adds to the direct harbor run is the whole `benchmark` kind seam — its
checks, the command it builds, the run directory the job fills, and the result
the CLI turns back into an exit status.

The broker is the real one over its real transport, driving the real CLI as its
root peer. Only the artifact store a session collects into is stood in for: a
job's answer is the run directory either way, and `ride/ride/artifacts.py` owns
the collection that turns one into a ref.

It builds a real bundle, drives the host docker daemon and spends real tokens,
so it stays out of the gate's roster and skips without the repository's token
opt-in:

  BRO_LLM_TESTS=1 uv run --directory benchmark pytest bro/benchmark/benchmark_job_e2e_test.py
"""

import asyncio
import contextlib
import os
import shutil
import sys
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import cast, override

from bro.benchmark.bundle import build, default_root, workspace_root
from bro.benchmark.e2e_test_helper import LIVE_TRIAL, assert_graded_run, one_task_config
from bro.broker.dispatcher import Broker, Dispatcher
from bro.broker.job import OUTPUT_DIRECTORY
from bro.broker.runtime import Peer
from bro.broker.spawn import ChildHandle, LaunchSpec, RingBuffer, Spawner
from bro.broker.transport import Provisioned
from bro.broker.transports.tcp import LOCAL_HOST, TcpServerTransport
from bro.kinds import ArtifactResolver, KindContext
from bro.local.benchmark_job import BENCHMARK, benchmark_kind

pytestmark = LIVE_TRIAL

BENCHMARK_JOB = Path(sys.executable).with_name('benchmark-job')
# the run's own bound: long enough for the one task, short enough that a wedged
# job ends the test rather than the day
JOB_TIMEOUT_SEC = 3600
_RING_BYTES = 65536


@dataclass(frozen=True)
class _SessionCommand(LaunchSpec):
  argv: tuple[str, ...]


class _SessionHandle(ChildHandle):
  def __init__(self, process: asyncio.subprocess.Process):
    self._process = process
    self._output = RingBuffer(_RING_BYTES)
    self._drain = asyncio.create_task(self._drain_output())

  async def _drain_output(self) -> None:
    assert self._process.stdout is not None  # carries stderr too (merged at spawn)
    while True:
      chunk = await self._process.stdout.read(4096)
      if len(chunk) == 0:
        return
      self._output.write(chunk)

  @override
  async def wait(self) -> int:
    code = await self._process.wait()
    await self._drain  # output fully captured before the tail is read
    return code

  @override
  async def kill(self) -> None:
    if self._process.returncode is None:
      self._process.kill()
    await self._process.wait()
    await self._drain

  @override
  def output_tail(self) -> str:
    return self._output.tail().decode('utf-8', errors='replace')


class _SessionSpawner(Spawner):
  """launches the session command on the channel the broker provisioned for it,
  the way a session's own launcher hands one to the harness it runs."""

  def __init__(self) -> None:
    self.handle: _SessionHandle | None = None

  @override
  async def spawn(self, launch: LaunchSpec, channel: Provisioned, exchange: str) -> ChildHandle:
    assert isinstance(launch, _SessionCommand)
    process = await asyncio.create_subprocess_exec(
      *launch.argv,
      env={
        **os.environ,
        'BROKER_CHANNEL': channel.host_endpoint.address(LOCAL_HOST),
        'BROKER_EXCHANGE': exchange,
      },
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.STDOUT,
    )
    self.handle = _SessionHandle(process)
    return self.handle

  def output(self) -> str:
    return '' if self.handle is None else self.handle.output_tail()


class _RunDirectories:
  """the `JobOutput` the session's artifact store stands in for here: it keeps
  each run where the test can read it, and answers with the directory itself as
  the ref the CLI prints.

  Collection takes its own copy the way the artifact store does, because the
  dispatcher removes the run directory once it has been collected."""

  def __init__(self, root: Path):
    self._root = root
    self._opened = 0
    self.runs: list[Path] = []

  def open(self) -> Path:
    directory = self._root / f'{self._opened}.staging'
    self._opened += 1
    directory.mkdir(parents=True)
    return directory

  async def collect(self, directory: Path, context: Dispatcher, requester: Peer) -> dict:
    collected = self._root / str(len(self.runs))
    shutil.copytree(directory, collected)
    self.runs.append(collected)
    return {'ref': str(collected)}


@contextlib.contextmanager
def _config_in_the_tree(tree: Path) -> Generator[str]:
  """the narrowed config where the kind accepts one: a file inside the workspace
  tree, named relative to it — the same spelling an operator passes."""
  directory = tree / 'var' / 'benchmark' / 'e2e'
  directory.mkdir(parents=True, exist_ok=True)
  try:
    yield str(one_task_config(directory).relative_to(tree))
  finally:
    shutil.rmtree(directory, ignore_errors=True)


def test_a_session_starts_the_trial_over_its_broker_channel(tmp_path):
  tree = workspace_root()
  build(tree, default_root(tree))
  runs = _RunDirectories(tmp_path / 'runs')
  spawner = _SessionSpawner()
  broker = Broker(TcpServerTransport([LOCAL_HOST]), spawner, job_output=runs)
  broker.on(
    BENCHMARK,
    benchmark_kind(
      KindContext(
        workspace_tree=tree,
        artifacts=cast(ArtifactResolver, object()),
        credential_scope=frozenset(),
      )
    ),
  )

  with _config_in_the_tree(tree) as config:
    exit_code = broker.run(
      _SessionCommand(
        (str(BENCHMARK_JOB), 'start', '-c', config, '--timeout', str(JOB_TIMEOUT_SEC))
      )
    )

  assert exit_code == 0, spawner.output()
  [run] = runs.runs
  assert_graded_run(run / OUTPUT_DIRECTORY)
