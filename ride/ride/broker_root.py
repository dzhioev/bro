"""Broker-root composition over registered worker types."""

from __future__ import annotations

import contextlib
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, cast

from bro.artifact import GET, MINT, SHARE
from bro.authority.rules import Mission
from bro.base import log
from bro.broker.dispatcher import PING, Broker, ping_handler
from bro.broker.spawn import LaunchSpec, Spawner
from bro.broker.transports.tcp import LOCAL_HOST, TcpServerTransport
from bro.quest import BRO, LAUNCH
from bro.worker_types import WorkerType, installed_types
from bro.workspace.paths import CONTAINER_ARTIFACTS_ROOT, launch_dir
from ride.artifacts import ArtifactControl, ArtifactStore, JobArtifacts
from ride.authority_reading import Reading
from ride.bro_worker import BroFacts, Placement, SummonSpawner
from ride.launch_control import LaunchControl
from ride.operator import OperatorControl
from ride.peer_facts import PeerFacts, WorkerFacts
from ride.runtime_bundle import RuntimeBundle
from ride.worker_container import WorkerContainerSpawner
from ride.workspace.docker import ContainerRuntimeResolver, bridge_gateway
from ride.workspace.model import Workspace
from ride.workspace.spawn import (
  DockerLaunchSpec,
  DockerSpawner,
  ExecSpawner,
  PartyMembers,
  ProcessLaunchSpec,
  ProcessSpawner,
)


@dataclass
class RideHost:
  workspace: Workspace
  peers: PeerFacts
  artifacts: ArtifactControl
  journal: Any
  session_env: dict[str, str]
  runtime_bundle: RuntimeBundle
  summon_spawner: SummonSpawner
  reading: Reading


def broker_bind_hosts() -> list[str]:
  """Every address a session's channels must answer on."""
  gateway = bridge_gateway()
  if gateway is None or gateway == LOCAL_HOST:
    return [LOCAL_HOST]
  with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
    try:
      probe.bind((gateway, 0))
    except OSError:
      log.verbose('docker bridge gateway %s is not an address of this host', gateway)
      return [LOCAL_HOST]
  return [LOCAL_HOST, gateway]


def run_root_via_broker(
  launch: LaunchSpec,
  *,
  workspace: Workspace,
  bro: str,
  mission: Mission,
  session_env: Mapping[str, str] = MappingProxyType({}),
  container_runtime: ContainerRuntimeResolver,
  runtime_bundle: RuntimeBundle,
  types: Mapping[str, type[WorkerType]] | None = None,
) -> int:
  """Run a root peer and serve launch through the installed worker types."""
  reading = cast(Reading, mission.reading)
  party_members = PartyMembers()
  docker_spawner = DockerSpawner(host_log=workspace.host_log, party_members=party_members)
  process_spawner = ProcessSpawner(host_log=workspace.host_log, party_members=party_members)
  exec_spawner = ExecSpawner(party_members)
  root_extension = BroFacts(
    bro=bro,
    placement=Placement('start', workspace.isolation),
  )
  facts = PeerFacts(
    WorkerFacts(
      type=BRO,
      workspace=workspace.name,
      tree=workspace.tree,
      record=mission,
      artifact_view=(
        PurePosixPath(CONTAINER_ARTIFACTS_ROOT) if isinstance(launch, DockerLaunchSpec) else None
      ),
      extension=root_extension,
    ),
    root_tree=workspace.tree,
    root_path=workspace.path,
  )
  artifacts = ArtifactStore(workspace, root_boxed=isinstance(launch, DockerLaunchSpec))
  if isinstance(launch, DockerLaunchSpec):
    root_spawner: Spawner = docker_spawner
  elif isinstance(launch, ProcessLaunchSpec):
    root_spawner = process_spawner
  else:
    raise TypeError(f'unsupported root launch {type(launch).__name__}')
  summon_spawner = SummonSpawner(
    docker_spawner,
    process_spawner,
    exec_spawner,
    runtime_bundle,
    container_runtime,
    facts,
    artifacts,
  )
  worker_container_spawner = WorkerContainerSpawner(
    docker_spawner,
    container_runtime,
    facts,
    artifacts,
  )
  artifact_control = ArtifactControl(artifacts, facts)
  facade = Broker(
    TcpServerTransport(broker_bind_hosts()),
    job_output=JobArtifacts(artifacts, facts),
  )
  operator = OperatorControl(
    facade, root=workspace.name, runtime=runtime_bundle.reference, peers=facts
  )
  facts.bind_journal(facade.journal)
  host = RideHost(
    workspace=workspace,
    peers=facts,
    artifacts=artifact_control,
    journal=facade.journal,
    reading=reading,
    session_env=dict(session_env),
    runtime_bundle=runtime_bundle,
    summon_spawner=summon_spawner,
  )
  classes = installed_types() if types is None else dict(types)
  for name, worker_type in classes.items():
    if worker_type.name != name:
      raise ValueError(f'worker type mapping key {name!r} names class {worker_type.name!r}')
  worker_types = {name: worker_type(host) for name, worker_type in classes.items()}
  control = LaunchControl(
    ride=workspace.name,
    types=worker_types,
    peers=facts,
    journal=facade.journal,
    audit_file=launch_dir() / f'{workspace.name}.jsonl',
    runtime_bundle=runtime_bundle,
    session_env=session_env,
    worker_container_spawner=worker_container_spawner,
    reading=reading,
  )
  facade.on(PING, ping_handler)
  facade.on(LAUNCH, control.handle)
  facade.on('authority', control.authority)
  operator.on('operator.commit', control.commit)
  operator.on('operator.claim', control.claim)
  facade.on(MINT, artifact_control.mint)
  facade.on(GET, artifact_control.get)
  facade.on(SHARE, artifact_control.share)
  facade.subscribe(facts.observe_journal)
  facade.subscribe(control.audit_event)
  facade.subscribe(control.observe_journal)
  for worker_type in worker_types.values():
    for subscriber in worker_type.subscribers():
      facade.subscribe(subscriber)
  with contextlib.closing(artifacts):
    return facade.run(
      launch,
      root_spawner,
      type=BRO,
      end_on_sigterm=True,
      lifetime=operator.lifetime,
      timeout=None
      if mission.descriptor.values['timeout'] == 'none'
      else cast(int, mission.descriptor.values['timeout']),
    )
