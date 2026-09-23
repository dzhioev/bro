"""Generic host control for the broker's ``launch`` request kind."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from bro.artifact import is_ref
from bro.base import log
from bro.broker.brotocol import TALK_RIGHTS, Talk
from bro.worker_types import (
  ArtifactDenied,
  Container,
  Expect,
  Job,
  LaunchDenied,
  LaunchRequest,
  PeerDescription,
  Spawn,
  UnattributablePeer,
  WorkerType,
  type_name,
)
from ride import pending_launch
from ride.peer_facts import WorkerFacts
from ride.worker_container import WorkerContainerLaunch

if TYPE_CHECKING:
  from bro.broker.brotocol import Message
  from bro.broker.dispatcher import Dispatcher
  from bro.broker.journal import Event, Journal, Record
  from bro.broker.runtime import Peer
  from bro.broker.spawn import Spawner

_COMMON_FIELDS = frozenset({'type', 'timeout', 'share', 'talk', 'manual'})


def _json_object(value: Any, subject: str) -> dict[str, Any]:
  if not isinstance(value, Mapping):
    raise LaunchDenied(f'{subject} must be a JSON object')
  raw = dict(value)
  if not all(isinstance(key, str) for key in raw):
    raise LaunchDenied(f'{subject} must have string keys')
  try:
    rendered = json.loads(json.dumps(raw, ensure_ascii=False))
  except (TypeError, ValueError) as error:
    raise LaunchDenied(f'{subject} is not JSON-serializable: {error}') from error
  if not isinstance(rendered, dict):
    raise LaunchDenied(f'{subject} must be a JSON object')
  return rendered


class LaunchControl:
  """Validate common launch arguments and lower a type's selected run."""

  def __init__(
    self,
    *,
    ride: str,
    types: Mapping[str, WorkerType],
    peers: Any,
    journal: Journal,
    audit_file: Path,
    runtime_bundle: Any,
    session_env: Mapping[str, str],
    worker_container_spawner: Spawner,
  ):
    self._ride = ride
    self._types = dict(types)
    self._peers = peers
    self._journal = journal
    self._audit_file = audit_file
    self._runtime_bundle = runtime_bundle
    self._session_env = dict(session_env)
    self._worker_container_spawner = worker_container_spawner
    self._audit_fields: dict[str, dict[str, Any]] = {}
    self._owners: dict[str, dict[str, str]] = {}

  def handle(self, context: Dispatcher, peer: Peer, message: Message) -> None:
    requested_type = message.args.get('type')
    try:
      owner = self._peers.resolve(context, peer)
    except UnattributablePeer as error:
      self._deny(context, peer, str(error), self._usable_type(requested_type))
      return
    try:
      request, worker_type = self._request(message, owner)
      talk = worker_type.talk(request)
      run = worker_type.launch(request)
      if request.share and isinstance(run, (Job, Expect)):
        raise LaunchDenied(f"worker type {request.type!r} cannot honor 'share' for this run")
      if isinstance(run, Job) and talk:
        raise LaunchDenied(f'worker type {request.type!r} selected a job with chat rights')
      if request.manual != isinstance(run, Expect):
        expectation = 'requires' if isinstance(run, Expect) else 'does not support'
        raise LaunchDenied(f'worker type {request.type!r} {expectation} a manual launch')
      if isinstance(run, Expect):
        run = replace(
          run,
          pending=_json_object(run.pending, 'an expected worker pending value'),
        )
      fields = _json_object(worker_type.audit_fields(run.extension), 'worker audit fields')
    except LaunchDenied as error:
      self._deny(context, peer, str(error), self._usable_type(requested_type))
      return

    permits = run.permits
    self._peers.add(
      request.id,
      WorkerFacts(
        type=request.type,
        workspace=None,
        permits=permits,
        expected=isinstance(run, Expect),
        extension=run.extension,
      ),
    )
    self._audit_fields[request.id] = fields
    self._owners[request.id] = self._peers.attribution_for_mission(context.journal, owner.mission)
    if isinstance(run, Spawn):
      context.spawn(
        run.launch,
        run.spawner,
        peer,
        type=request.type,
        talk=talk,
        timeout=request.timeout,
      )
      return
    if isinstance(run, Job):
      context.job(
        run.command,
        peer,
        type=request.type,
        timeout=request.timeout,
      )
      return
    if isinstance(run, Container):
      context.spawn(
        WorkerContainerLaunch(
          type=request.type,
          spec=run.spec,
          owner_workspace=request.owner.workspace,
          share=request.share,
        ),
        self._worker_container_spawner,
        peer,
        type=request.type,
        talk=talk,
        timeout=request.timeout,
      )
      return
    context.expect(
      peer,
      type=request.type,
      talk=talk,
      ready=lambda provisioned: self._ready(request, run, talk, provisioned),
    )

  def _request(self, message: Message, owner: PeerDescription) -> tuple[LaunchRequest, WorkerType]:
    args = message.args
    unknown_type = args.get('type')
    if not isinstance(unknown_type, str):
      raise LaunchDenied("launch needs a string 'type'")
    try:
      name = type_name(unknown_type)
    except ValueError as error:
      raise LaunchDenied(str(error)) from error
    worker_type = self._types.get(name)
    if worker_type is None:
      available = ', '.join(sorted(self._types)) or '(none)'
      raise LaunchDenied(f'unknown worker type {name!r}; installed types: {available}')

    timeout_value = args.get('timeout', worker_type.default_timeout)
    if timeout_value is not None and (
      not isinstance(timeout_value, (int, float))
      or isinstance(timeout_value, bool)
      or not math.isfinite(timeout_value)
      or timeout_value <= 0
    ):
      raise LaunchDenied("launch 'timeout' must be a positive number of seconds")
    timeout = None if timeout_value is None else float(timeout_value)

    share_value = args.get('share', [])
    if not isinstance(share_value, list) or not all(is_ref(value) for value in share_value):
      raise LaunchDenied("launch 'share' must be a list of artifact refs")
    for ref in share_value:
      try:
        worker_type.host.artifacts.resolve(ref, owner)
      except ArtifactDenied as error:
        raise LaunchDenied(f'cannot share artifact(s) the owner cannot reach: {ref}') from error

    talk_value = args.get('talk', [])
    if (
      not isinstance(talk_value, list)
      or not all(isinstance(right, str) and right in TALK_RIGHTS for right in talk_value)
      or len(talk_value) != len(set(talk_value))
    ):
      raise LaunchDenied("launch 'talk' must be a list of distinct talk rights")
    if 'talk' in args and not worker_type.widens_talk:
      raise LaunchDenied(f'worker type {name!r} does not accept talk widening')

    manual_value = args.get('manual', False)
    if not isinstance(manual_value, bool) or ('manual' in args and manual_value is not True):
      raise LaunchDenied("launch 'manual' must be true when present")
    if manual_value and not worker_type.manual:
      raise LaunchDenied(f'worker type {name!r} does not support manual launch')
    if manual_value:
      refused = [key for key in ('timeout', 'share') if key in args]
      if refused:
        raise LaunchDenied(
          f'a manual launch cannot name {", ".join(sorted(refused))}; drop the field(s)'
        )

    owned_args = {key: value for key, value in args.items() if key not in _COMMON_FIELDS}
    return (
      LaunchRequest(
        id=message.request_id,
        type=name,
        args=owned_args,
        owner=owner,
        requested_talk=cast(Talk, frozenset(talk_value)),
        timeout=timeout,
        share=tuple(share_value),
        manual=manual_value,
      ),
      worker_type,
    )

  def _ready(self, request: LaunchRequest, run: Expect, talk: Talk, provisioned: Any) -> None:
    pending_launch.write(
      pending_launch.PendingLaunch(
        token=request.id,
        runtime=self._runtime_bundle.reference,
        port=provisioned.host_endpoint.port,
        channel_token=provisioned.host_endpoint.token,
        type=request.type,
        talk=tuple(sorted(talk)),
        owner_tree=str(request.owner.tree),
        env=dict(self._session_env),
        extension=run.pending,
      )
    )

  def observe_journal(self, event: Event, record: Record) -> None:
    if event.transition == 'ended' and self._is_expected(event.mission):
      pending_launch.discard(event.mission)
    if event.transition in ('ended', 'denied'):
      self._audit_fields.pop(event.mission, None)
      self._owners.pop(event.mission, None)

  def audit_event(self, event: Event, record: Record) -> None:
    entry: dict[str, Any] = {'ride': self._ride, **event.view()}
    if record.parent is not None:
      owner = self._owners.get(event.mission)
      if owner is None:
        owner = self._peers.attribution_for_mission(self._journal, record.parent)
      entry['owner'] = owner
    entry['type'] = record.type
    entry['published_ports'] = []
    try:
      facts = self._peers.for_mission(event.mission)
    except UnattributablePeer:
      facts = None
    if facts is not None:
      entry['published_ports'] = [list(pair) for pair in facts.published_ports]
    fields = self._audit_fields.get(event.mission)
    if fields:
      entry['extension'] = fields
    try:
      self._audit_file.parent.mkdir(parents=True, exist_ok=True)
      with self._audit_file.open('a') as audit:
        audit.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except OSError as error:
      log.warning('could not append launch audit record to %s: %s', self._audit_file, error)

  def _is_expected(self, mission: str) -> bool:
    try:
      return self._peers.for_mission(mission).expected
    except UnattributablePeer:
      return False

  def _deny(
    self,
    context: Dispatcher,
    peer: Peer,
    reason: str,
    requested_type: str | None,
  ) -> None:
    error = f'launch denied: {reason}'
    log.warning('launch: %s: %s', self._ride, error)
    context.deny(peer, error, type=requested_type)

  @staticmethod
  def _usable_type(value: object) -> str | None:
    if not isinstance(value, str):
      return None
    try:
      return type_name(value)
    except ValueError:
      return None
