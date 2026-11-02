"""Generic host control for the broker's ``launch`` request kind."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from bro.artifact import is_ref
from bro.authority.patterns import Descriptor
from bro.authority.records import dump_authority, dump_request, dump_values
from bro.authority.rules import Admission, Mission, Prepared
from bro.base import credentials, log
from bro.base.lulid import lulid
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
from bro.workspace.paths import is_workspace_name
from ride import pending_launch
from ride.authority_launch import mission_view, request as authority_request
from ride.authority_reading import Reading
from ride.peer_facts import WorkerFacts
from ride.worker_container import WorkerContainerLaunch

if TYPE_CHECKING:
  from bro.broker.brotocol import Message
  from bro.broker.dispatcher import Dispatcher
  from bro.broker.journal import Event, Journal, Record
  from bro.broker.runtime import Peer
  from bro.broker.spawn import Spawner


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
    reading: Reading,
  ):
    self.reading = reading
    self._ride = ride
    self._types = dict(types)
    self._peers = peers
    self._journal = journal
    self._audit_file = audit_file
    self._runtime_bundle = runtime_bundle
    self._session_env = dict(session_env)
    self._worker_container_spawner = worker_container_spawner
    self._prepared: dict[str, Prepared] = {}
    self._commits: dict[str, dict[str, Any]] = {}
    self._audit_fields: dict[str, dict[str, Any]] = {}
    self._audit_passes: dict[str, tuple[str, ...]] = {}
    self._refusals: dict[str, dict[str, str]] = {}
    self._owners: dict[str, dict[str, str]] = {}
    from ride.credential_overrides import CredentialOverrides

    self.credential_overrides = CredentialOverrides(peers, journal, reading)

  def handle(self, context: Dispatcher, peer: Peer, message: Message) -> None:
    requested_type = message.args.get('type')
    raw_passes = message.args.get('pass', [])
    if isinstance(raw_passes, list) and all(isinstance(value, str) for value in raw_passes):
      self._audit_passes[message.request_id] = tuple(raw_passes)
    try:
      owner = self._peers.resolve(context, peer)
    except UnattributablePeer as error:
      self._deny(context, peer, str(error), self._usable_type(requested_type), message.request_id)
      return
    try:
      request, worker_type = self._request(message, owner)
      talk = worker_type.talk(request)
      if isinstance(request.record, Prepared):
        self._prepared[request.id] = request.record
      facts = WorkerFacts(
        type=request.type, workspace=None, record=request.record, expected=request.manual
      )
      self._peers.add(request.id, facts)
      run = worker_type.launch(request)
      if request.passes and isinstance(run, Job):
        raise LaunchDenied(f"worker type {request.type!r} cannot honor 'pass' for this run")
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
    except (LaunchDenied, ValueError) as error:
      self._deny(context, peer, str(error), self._usable_type(requested_type), message.request_id)
      return

    facts.extension = run.extension
    facts.expected = isinstance(run, Expect)
    self._audit_fields[request.id] = fields
    self._audit_passes[request.id] = request.passes
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
          passes=request.passes,
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

    if owner.mission_record is None:
      raise LaunchDenied('starter check: requesting mission has no authority record')
    try:
      requested, content = authority_request(
        type(worker_type),
        {key: value for key, value in args.items() if key != 'type'},
        self.reading,
        owner.mission_record.authority,
      )
      if manual_value:
        record = self.reading.rules.prepare(requested, owner.mission_record.authority, self.reading)
        values = requested.values
      else:

        def depth(descriptor: Descriptor) -> None:
          if owner.depth + 1 > self.reading.summon_depth:
            raise ValueError(f'chain exceeds {self.reading.summon_depth}')

        record = self.reading.rules.start(
          requested, owner.mission_record.authority, self.reading, (Admission('depth cap', depth),)
        )
        values = record.descriptor.values
    except ValueError as error:
      raise LaunchDenied(str(error)) from error
    timeout_value = values.get('timeout', 'none')
    passes = (
      tuple(
        credentials.storage_name(key.removeprefix('cred.'), cast(str, value))
        + ('+' if value == '' else '')
        for key, value in values.items()
        if key.startswith('cred.')
      )
      if name != 'bro'
      else ()
    )
    return (
      LaunchRequest(
        id=message.request_id,
        type=name,
        args=dict(content),
        owner=owner,
        requested_talk=cast(Talk, values.get('talk', frozenset())),
        timeout=None if timeout_value == 'none' else cast(int, timeout_value),
        passes=passes,
        share=tuple(share_value),
        manual=manual_value,
        record=record,
      ),
      worker_type,
    )

  def authority(self, context: Dispatcher, peer: Peer, message: Message) -> None:
    try:
      owner = self._peers.resolve(context, peer)
      if message.args or owner.mission_record is None:
        raise ValueError('authority check: mission record required without arguments')
      context.reply(peer, {'outcome': 'ok', 'value': mission_view(owner.mission_record)})
    except (ValueError, UnattributablePeer) as error:
      context.reply(peer, {'outcome': 'denied', 'error': str(error)})

  def override(self, context: Dispatcher, peer: Peer, message: Message) -> None:
    self.credential_overrides.handle(context, peer, message)

  def commit(self, context: Dispatcher, peer: Peer, message: Message) -> None:
    from ride.authority_launch import credentials_for
    from ride.authority_scope import checked_recipe

    try:
      if set(message.args) != {'token', 'fields'}:
        raise ValueError('commit check: token and fields required')
      token = message.args['token']
      facts = self._peers.for_mission(token)
      prepared = self._prepared.get(token, facts.record)
      if not isinstance(prepared, Prepared):
        raise ValueError('commit check: token has no prepared request')
      journal_record = context.journal.records[token]
      if journal_record.state != 'accepted' or pending_launch.claimed_workspace(token) is not None:
        raise ValueError('commit check: token is not pending')
      starter = self._peers.for_mission(journal_record.parent).record
      if not isinstance(starter, Mission):
        raise ValueError('commit check: starter record missing')
      if not isinstance(message.args['fields'], Mapping):
        raise ValueError('commit check: fields: expected an object')
      fields = dict(message.args['fields'])
      fields['target'] = prepared.request.values['target']
      fields['harness'] = self.reading.resolve_harness(
        fields.get('harness'), target=cast(str, prepared.request.values['target'])
      )
      mapped, _ = authority_request(
        type(self._types[prepared.request.type]),
        fields,
        self.reading,
        self.reading.operator(),
        forward=False,
      )
      if mapped.gift.permissions() or mapped.cap.permissions():
        raise ValueError('commit check: authority entries belong to the prepared request')

      def depth(descriptor: Descriptor) -> None:
        if len(context.journal.ancestry(token)) > self.reading.summon_depth:
          raise ValueError(f'chain exceeds {self.reading.summon_depth}')

      from ride.authority_launch import credential_values

      combined = credential_values(prepared.request.values, mapped.values)
      for name in ('add', 'drop'):
        if name in fields:
          combined[name] = mapped.values[name]
      removed = prepared.request.values.keys() - combined.keys()
      original = replace(
        prepared.request,
        values={
          name: value for name, value in prepared.request.values.items() if name not in removed
        },
      )
      operator_values = dict(mapped.values)
      for name in ('add', 'drop'):
        if name in combined and (
          name in mapped.values or combined[name] != prepared.request.values.get(name)
        ):
          operator_values[name] = combined[name]
      prepared = replace(prepared, request=original)
      mission = self.reading.rules.commit(
        prepared,
        operator_values,
        starter.authority,
        self.reading.operator(),
        (Admission('depth cap', depth),),
      )
      if mission.descriptor.values['timeout'] != 'none':
        raise ValueError(
          'execution check: bro.timeout: finite manual deadlines require a supervised start'
        )
      resolved = checked_recipe(
        mission.descriptor,
        self.reading,
        None if fields.get('llm') is None else (fields['llm'], '--llm'),
      )
      scoped = credentials_for(mission)
      value = {
        **mission_view(mission),
        'request': dump_request(mission.request, mission.descriptor),
        'required': sorted(scoped.required),
        'optional': sorted(scoped.optional),
        'resolved_recipe': resolved.dump(),
      }
      previous = self._commits.get(token)
      value['commit'] = (
        previous['commit']
        if previous is not None
        and {name: item for name, item in previous.items() if name != 'commit'} == value
        else lulid()
      )
      self._commits[token] = value
      facts.record = mission
      context.reply(peer, {'outcome': 'ok', 'value': value})
    except (ValueError, KeyError, UnattributablePeer, credentials.SecretNotFound) as error:
      context.reply(peer, {'outcome': 'denied', 'error': str(error)})

  def claim(self, context: Dispatcher, peer: Peer, message: Message) -> None:
    try:
      if set(message.args) != {'token', 'commit', 'workspace'}:
        raise ValueError('claim check: token, commit, and workspace required')
      token, commit, workspace = (message.args[name] for name in ('token', 'commit', 'workspace'))
      if not isinstance(token, str) or token not in self._commits:
        raise ValueError('claim check: token: no successful commit')
      if commit != self._commits[token]['commit']:
        raise ValueError(f'claim check: {token}: commit was superseded')
      if not isinstance(workspace, str) or not is_workspace_name(workspace):
        raise ValueError('claim check: workspace: invalid name')
      if context.journal.records[token].state != 'accepted':
        raise ValueError(f'claim check: {token}: mission is not pending')
      facts = self._peers.for_mission(token)
      if not isinstance(facts.record, Mission):
        raise ValueError(f'claim check: {token}: admitted mission record missing')
      claimed = pending_launch.claimed_workspace(token)
      if claimed is None:
        pending_launch.claim(token, workspace=workspace)
        self._peers.for_mission(token).workspace = workspace
      elif claimed != workspace:
        raise ValueError(f'claim check: {token}: already claimed by {claimed}')
      context.journal.records[token].talk = cast(Talk, facts.record.descriptor.values['talk'])
      context.reply(peer, {'outcome': 'ok', 'value': {'commit': commit, 'workspace': workspace}})
    except (ValueError, KeyError, UnattributablePeer, pending_launch.UnknownToken) as error:
      context.reply(peer, {'outcome': 'denied', 'error': str(error)})

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
      self._audit_passes.pop(event.mission, None)
      self._owners.pop(event.mission, None)
      self._refusals.pop(event.mission, None)
      self._prepared.pop(event.mission, None)
      self._commits.pop(event.mission, None)

  def audit_event(self, event: Event, record: Record) -> None:
    entry: dict[str, Any] = {'ride': self._ride, **event.view()}
    if record.parent is not None:
      owner = self._owners.get(event.mission)
      if owner is None:
        owner = self._peers.attribution_for_mission(self._journal, record.parent)
      entry['owner'] = owner
    entry['type'] = record.type
    entry['pass'] = list(self._audit_passes.get(event.mission, ()))
    entry['published_ports'] = []
    entry.update(self._refusals.get(event.mission, {}))
    try:
      facts = self._peers.for_mission(event.mission)
    except UnattributablePeer:
      facts = None
    if facts is not None:
      entry['published_ports'] = [list(pair) for pair in facts.published_ports]
      if isinstance(facts.record, Mission):
        mission = facts.record
        entry['descriptor'] = {
          'type': mission.descriptor.type,
          'values': dump_values(mission.descriptor.values),
        }
        entry['gift'] = dump_authority(mission.request.gift, mission.descriptor)
        entry['cap'] = dump_authority(mission.request.cap, mission.descriptor)
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
    request_id: str,
  ) -> None:
    rule = reason.partition(': ')[0]
    if not rule.endswith((' rule', ' check')):
      reason = f'request check: {requested_type or "launch"}: {reason}'
    rule, separator, remainder = reason.partition(': ')
    if separator:
      atom = remainder.partition(': ')[0]
      self._refusals[request_id] = {'rule': rule, 'atom': atom}
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
