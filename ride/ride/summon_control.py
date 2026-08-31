"""Host authorization and journal projection for the ``summon`` kind.

``SummonControl.handle`` validates and authorizes each request against the
requesting peer's recorded identity, then binds a spawned or expected Worker
through the Dispatcher primitives. Deterministic refusals use ``Dispatcher.deny``
so answer and journal record are one operation.

The control subscribes to the broker journal for the identity records nested
summons still need, root trail fallback, and manual-token cleanup. A second
subscriber writes every journal event to the human-facing summon audit.
Sessions own their own trail pointers; the host only observes trail marks.

Broker imports stay function-local where the pre-gate launch path requires it.
"""

import json
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from bro.artifact import is_ref
from bro.base import credentials, log
from bro.summon import DEFAULT_HARNESS, DEFAULT_TIMEOUT
from ride import pending_summon
from ride.harness import HARNESS_NAMES, get_harness
from ride.peers import PeerIdentity, Peers, UnattributablePeer
from ride.scope import split_scope_overrides, summoned_credential_scope
from ride.workspace.model import Workspace
from ride.workspace.store import ScopedSecrets

if TYPE_CHECKING:
  from bro.broker.brotocol import Message
  from bro.broker.dispatcher import Dispatcher
  from bro.broker.journal import Event, Record
  from bro.broker.runtime import Peer
  from bro.broker.transport import Provisioned
  from ride.artifacts import ArtifactStore

__all__ = ['SummonControl', 'summon_allow_list']

_PROMPT_HEAD_CHARS = 120
_ARGS_KEYS = frozenset(
  {
    'target',
    'prompt',
    'timeout',
    'into',
    'hold',
    'step_id',
    'index',
    'grant',
    'revoke',
    'share',
    'llm',
    'harness',
    'manual',
  }
)
# fields a manual summon refuses: the user's launch owns the session's shape, and
# there is no host-killable child for a timeout to bound
_LAUNCH_OWNED_KEYS = ('timeout', 'hold', 'llm', 'harness')
# the deepest peer a summon may spawn: the root sits at depth 0, its children at
# 1, grandchildren at 2; a request that would nest deeper is denied — the guard
# against seed cycles recursing through real containers (see module docstring).
_MAX_SUMMON_DEPTH = 2


def summon_allow_list(bro_name: str, *, grant: list[str], revoke: list[str]) -> set[str]:
  """the effective summon allow-list of a session running as `bro_name`:
  `(may_summon ∪ grant) − revoke`.

  the seeds are the bro's MRO-collected `may_summon` defaults; `grant`/`revoke`
  are the per-session `--grant @bro`/`--revoke @bro` overrides, applied
  strictly (`credentials.apply_grant_revoke`). every name involved — seed or
  override — must be a registered bro, checked against `registry.known_names()`
  without importing any target module, so a typo fails the launch immediately
  rather than minutes later as a denied summon."""
  # imported here, not at module level: the registry import pulls the bro class
  # graph, which the pre-gate launch path must not pay for up front
  from bro.registry import create_bro, known_names

  seeds = create_bro(bro_name)._may_summon
  unknown = sorted((set(seeds) | set(grant) | set(revoke)) - known_names())
  if len(unknown) > 0:
    raise ValueError(f'unknown summon target(s): {", ".join(unknown)}; not in the bro registry')
  return credentials.apply_grant_revoke(
    seeds, grant=grant, revoke=revoke, subject='summon allow-list'
  )


def _summoned_scope(
  target: str,
  harness_name: Optional[str],
  llm: Optional[str],
  *,
  attachment: Optional[str],
  grant: Sequence[str] = (),
  revoke: Sequence[str] = (),
) -> ScopedSecrets:
  harness = get_harness(harness_name if harness_name is not None else DEFAULT_HARNESS)
  return summoned_credential_scope(
    target,
    harness.scope_recipe(harness.default_options()),
    attachment=attachment,
    grant=list(grant),
    revoke=list(revoke),
    llm_spec=harness.resolve_llm(llm, target),
  )


def _prompt_head(prompt: str) -> str:
  return ' '.join(prompt.split())[:_PROMPT_HEAD_CHARS]


def _validate(args: dict[str, Any]) -> Optional[str]:
  """the request's shape errors, or None when well-formed. Strict: an unknown key
  is rejected rather than ignored — a typo'd `timout` silently falling back to the
  default would hide the caller's bug."""
  from bro.llm.providers import LLMSelectionError, parse as parse_llm
  from bro.mcp import HOLDS

  unknown = sorted(set(args) - _ARGS_KEYS)
  if len(unknown) > 0:
    return f'unknown summon field(s): {", ".join(unknown)}'
  for key in ('target', 'prompt'):
    value = args.get(key)
    if not isinstance(value, str) or len(value) == 0:
      return f'summon needs a non-empty string {key!r}'
  timeout = args.get('timeout')
  if timeout is not None and (not isinstance(timeout, (int, float)) or timeout <= 0):
    return "summon 'timeout' must be a positive number of seconds"
  into = args.get('into')
  if into is not None and (not isinstance(into, str) or len(into) == 0):
    return "summon 'into' must be a non-empty git ref"
  hold = args.get('hold')
  if hold is not None and hold not in HOLDS:
    return f"summon 'hold' must be one of {', '.join(HOLDS)}"
  step_id = args.get('step_id')
  if step_id is not None and (
    not isinstance(step_id, int) or isinstance(step_id, bool) or step_id < 0
  ):
    return "summon 'step_id' must be a non-negative int"
  index = args.get('index')
  if index is not None and (
    step_id is None or not isinstance(index, int) or isinstance(index, bool) or index < 0
  ):
    return "summon 'index' requires step_id and must be a non-negative int"
  # grant/revoke are checked on presence, not on non-None: unlike the optional
  # fields above they feed a non-optional consumer (the override split), so a
  # null must be a shape error rather than a default
  for key in ('grant', 'revoke'):
    if key in args and (
      not isinstance(args[key], list)
      or not all(isinstance(value, str) and len(value) > 0 for value in args[key])
    ):
      return f'summon {key!r} must be a list of non-empty names'
  if 'share' in args and (
    not isinstance(args['share'], list) or not all(is_ref(value) for value in args['share'])
  ):
    return "summon 'share' must be a list of artifact refs (sha256:<64 hex digits>)"
  llm = args.get('llm')
  if llm is not None:
    if not isinstance(llm, str):
      return "summon 'llm' must be a string"
    try:
      parse_llm(llm)
    except LLMSelectionError as error:
      return f"summon 'llm': {error}"
  harness = args.get('harness')
  if harness is not None and harness not in HARNESS_NAMES:
    return f"summon 'harness' must be one of {', '.join(HARNESS_NAMES)}"
  if 'manual' in args:
    if args['manual'] is not True:
      return "summon 'manual' must be true when present"
    refused = sorted(key for key in _LAUNCH_OWNED_KEYS if key in args)
    if len(refused) > 0:
      return f"a manual summon's launch owns {', '.join(refused)}; drop the field(s)"
    if 'share' in args:
      return "a manual summon's container is not launched by the host, so 'share' cannot be honored"
  return None


@dataclass
class _ActiveSummon:
  request_id: str
  target: str
  prompt_head: str
  summoner: dict[str, Any]  # audit attribution (see _Requester.summoner)
  depth: int  # the spawned child's summon-nesting depth (the root sits at 0)
  allow_list: set[str]  # the spawned child's own effective summon allow-list
  grant: list[str]  # the request's scope overrides, audited as the summoner issued them
  revoke: list[str]
  llm: Optional[str]
  harness: Optional[str]
  share: list[str] = field(default_factory=list)  # artifact refs handed down, audited
  manual: bool = False  # a user-launched child attached over an expected channel
  trail_id: Optional[str] = None


@dataclass(frozen=True)
class _Requester:
  """the summon identity of a requesting peer, resolved per request.

  `summoner` is the attribution the audit entries carry: the root is
  `{'session': <key>}`, a summoned child `{'target': <bro>, 'trail_id': …}`.
  `list_description` names the allow-list in denial messages — the two lists have
  different widening levers (relaunching the session vs the summon that spawned
  the peer, or seeding its bro), and the denial reason should point at the right
  one. `identity` is the peer's workspace attribution (`ride/ride/peers.py`) —
  the name bounds what it may share, the tree is the base-ref inheritance source
  for the children it summons.
  `credentials` reads the peer's own credential scope, the bound on what it may
  grant; it is a thunk because computing a summoned peer's scope is real work
  (bro import + registry read) that only a request carrying a credential grant
  should pay for on the broker loop."""

  allow_list: set[str]
  credentials: Callable[[], ScopedSecrets]
  summoner: dict[str, Any]
  summoned_by: Optional[dict[str, Any]]
  depth: int
  list_description: str
  identity: PeerIdentity


def _credential_refusal(
  requester: _Requester,
  target: str,
  *,
  attachment: Optional[str],
  grant_credentials: list[str],
  harness_name: Optional[str],
  llm: Optional[str],
) -> Optional[str]:
  """why the request's credential widening is refused, or None when it stays
  inside the requester's own scope. Two widenings, one bound: what `grant`
  names outright, and what the requested `harness`/`llm` add on top of the
  target's own default scope, the driving loop they select contributing
  credentials of its own. Only that delta is bounded — the target's declared
  credentials are what its allow-list entry sanctions. A child scope that cannot
  be computed at all is a refusal of its own, carrying the reason.

  Raises `UnattributablePeer` when the requester's own scope cannot be read."""
  widening: set[str] = set()
  if harness_name is not None or llm is not None:
    try:
      requested_scope = _summoned_scope(target, harness_name, llm, attachment=attachment)
      default_scope = _summoned_scope(target, None, None, attachment=attachment)
      widening = (requested_scope.required | requested_scope.optional) - (
        default_scope.required | default_scope.optional
      )
    except ValueError as error:
      return str(error)
  if len(grant_credentials) == 0 and len(widening) == 0:
    return None
  held_scope = requester.credentials()
  held_kinds = held_scope.required | held_scope.optional
  beyond: list[str] = []
  for grant in grant_credentials:
    kind, instance = credentials.parse_name(grant)
    if kind not in held_kinds or (
      instance is not None and held_scope.selection.get(kind, '') != instance
    ):
      beyond.append(grant)
  if len(beyond) > 0:
    return f'cannot grant credential(s) the summoner does not hold: {", ".join(sorted(beyond))}'
  beyond = sorted(widening - held_kinds)
  if len(beyond) > 0:
    requested = ' and '.join(
      f'{key} {value!r}'
      for key, value in (('harness', harness_name), ('llm', llm))
      if value is not None
    )
    return (
      f'the requested {requested} needs credential(s) the summoner does not '
      f'hold: {", ".join(beyond)}'
    )
  return None


class SummonControl:
  """One broker root's summon authorization and journal projections."""

  def __init__(
    self,
    *,
    allow_list: Collection[str],
    credential_scope: ScopedSecrets,
    workspace: Workspace,
    peers: Peers,
    artifacts: 'ArtifactStore',
    audit_file: Path,
  ):
    self._allow_list = set(allow_list)
    self._credential_scope = ScopedSecrets(
      required=set(credential_scope.required),
      optional=set(credential_scope.optional),
      selection=dict(credential_scope.selection),
    )
    self._workspace = workspace
    self._peers = peers
    self._artifacts = artifacts
    self._audit_file = audit_file
    self._root_trail_id: Optional[str] = None
    self._pending: dict[str, _ActiveSummon] = {}
    self._records: dict[str, _ActiveSummon] = {}
    self._denial_summoners: dict[str, Optional[dict[str, Any]]] = {}

  def note_root_trail(self, trail_id: Optional[str]) -> None:
    """Record the root's trail mark as summon-provenance fallback."""
    if trail_id is not None:
      self._root_trail_id = trail_id

  # --- the `summon` request handler (broker loop) -------------------------------

  def handle(self, context: 'Dispatcher', peer: 'Peer', message: 'Message') -> None:
    from ride.spawn import SummonLaunchSpec

    args = message.args
    try:
      requester = self._requester(context, peer)
    except UnattributablePeer as reason:
      self._deny(context, peer, message, None, f'summon denied: {reason}')
      return
    error = _validate(args)
    if error is not None:
      self._deny(context, peer, message, requester.summoner, error)
      return
    if requester.depth + 1 > _MAX_SUMMON_DEPTH:
      self._deny(
        context,
        peer,
        message,
        requester.summoner,
        f'summon denied: summon depth cap ({_MAX_SUMMON_DEPTH}) reached',
      )
      return
    target = args['target']
    if target not in requester.allow_list:
      from bro.registry import known_names

      if target not in known_names():
        error = f'summon denied: unknown bro {target!r}'
      else:
        error = f'summon denied: {target!r} is not in {requester.list_description}'
      self._deny(context, peer, message, requester.summoner, error)
      return
    grant = args.get('grant', [])
    revoke = args.get('revoke', [])
    harness_name = args.get('harness')
    llm = args.get('llm')
    try:
      grant_credentials, grant_bros = split_scope_overrides(grant)
      _, revoke_bros = split_scope_overrides(revoke)
      child_allow_list = summon_allow_list(target, grant=grant_bros, revoke=revoke_bros)
    except ValueError as e:
      self._deny(context, peer, message, requester.summoner, f'summon denied: {e}')
      return
    # a summoner can only hand down what it holds itself: grants are bounded by the
    # requesting peer's own two scopes, so no chain of summons reaches authority the
    # session was not launched with
    beyond = sorted(set(grant_bros) - requester.allow_list)
    if len(beyond) > 0:
      self._deny(
        context,
        peer,
        message,
        requester.summoner,
        f'summon denied: cannot grant summon target(s) the summoner may not '
        f'summon itself: {", ".join(beyond)}',
      )
      return
    try:
      refusal = _credential_refusal(
        requester,
        target,
        attachment=self._workspace.metadata.repo,
        grant_credentials=grant_credentials,
        harness_name=harness_name,
        llm=llm,
      )
    except UnattributablePeer as reason:
      refusal = str(reason)
    if refusal is not None:
      self._deny(context, peer, message, requester.summoner, f'summon denied: {refusal}')
      return
    # a summoner can only hand down refs it may read itself; the denial is as
    # uniform as the read path's — naming a ref that does not exist and naming
    # one outside the requester's reach are indistinguishable
    share = args.get('share', [])
    unreachable = sorted(
      ref for ref in share if not self._artifacts.reachable(ref, requester.identity.workspace)
    )
    if len(unreachable) > 0:
      self._deny(
        context,
        peer,
        message,
        requester.summoner,
        f'summon denied: cannot share artifact(s) the summoner cannot reach: '
        f'{", ".join(unreachable)}',
      )
      return
    prompt = args['prompt']
    summoned_by = requester.summoned_by
    step_id = args.get('step_id')
    if summoned_by is not None and step_id is not None:
      summoned_by = {**summoned_by, 'step_id': step_id}
      if args.get('index') is not None:
        summoned_by['index'] = args['index']
    self._peers.note_summon(context, peer, message.quest_id, manual=args.get('manual', False))
    if args.get('manual', False):
      self._pending[message.quest_id] = _ActiveSummon(
        request_id=message.quest_id,
        target=target,
        prompt_head=_prompt_head(prompt),
        summoner=requester.summoner,
        depth=requester.depth + 1,
        allow_list=child_allow_list,
        grant=list(grant),
        revoke=list(revoke),
        llm=None,
        harness=None,
        manual=True,
      )
      self._expect_manual(
        context,
        peer,
        message,
        requester,
        summoned_by=summoned_by,
        child_allow_list=child_allow_list,
        grant=list(grant),
        revoke=list(revoke),
      )
      return
    timeout = args.get('timeout')
    record = _ActiveSummon(
      request_id=message.quest_id,
      target=target,
      prompt_head=_prompt_head(prompt),
      summoner=requester.summoner,
      depth=requester.depth + 1,
      allow_list=child_allow_list,
      grant=list(grant),
      revoke=list(revoke),
      share=list(share),
      llm=llm,
      harness=harness_name,
    )
    self._pending[message.quest_id] = record
    context.spawn(
      SummonLaunchSpec(
        target=target,
        prompt=prompt,
        parent=requester.identity.workspace,
        repo=self._workspace.repository,
        summoner=summoned_by,
        may_summon=tuple(sorted(child_allow_list)),
        into=args.get('into'),
        hold=args.get('hold'),
        grant=tuple(grant),
        revoke=tuple(revoke),
        share=tuple(share),
        llm=llm,
        harness=harness_name,
      ),
      peer,
      timeout=float(timeout) if timeout is not None else DEFAULT_TIMEOUT,
    )

  def _expect_manual(
    self,
    context: 'Dispatcher',
    peer: 'Peer',
    message: 'Message',
    requester: _Requester,
    *,
    summoned_by: Optional[dict[str, Any]],
    child_allow_list: set[str],
    grant: list[str],
    revoke: list[str],
  ) -> None:
    """register the authorized manual summon as an expected external child:
    provision its channel, write the pending record the token (the request id)
    resolves to, and wait for the user's launch — unbounded by any host timer,
    since the launch is paced by a human and there is no child to kill."""
    args = message.args
    target = args['target']
    prompt = args['prompt']

    def _ready(provisioned: 'Provisioned') -> None:
      # function-local like SummonLaunchSpec in handle (ride/AGENTS.md, "Lazy
      # broker import")
      from bro.broker import brotocol

      pending_summon.write(
        pending_summon.PendingSummon(
          token=message.quest_id,
          protocol_revision=brotocol.PROTOCOL_REVISION,
          port=provisioned.host_endpoint.port,
          channel_token=provisioned.host_endpoint.token,
          target=target,
          prompt=prompt,
          parent_workspace=str(requester.identity.tree),
          may_summon=tuple(sorted(child_allow_list)),
          grant=tuple(grant),
          revoke=tuple(revoke),
          summoner=summoned_by,
          repo=self._workspace.metadata.repo,
          into=args.get('into'),
        ),
      )

    context.expect(peer, timeout=None, ready=_ready)

  def _requester(self, context: 'Dispatcher', peer: 'Peer') -> '_Requester':
    """resolve the requesting peer's summon identity: the root follows the
    session's launch-computed effective allow-list; a summoned child follows the
    list its own summon resolved, attributed through the shared peer registry
    (`ride/ride/peers.py`) plus this control's spawn records. Raises
    `UnattributablePeer` for a peer that cannot be attributed a bro."""
    identity = self._peers.identity(context, peer)
    if peer == context.root:
      return _Requester(
        allow_list=self._allow_list,
        credentials=lambda: self._credential_scope,
        summoner={'session': self._workspace.name},
        summoned_by=self._root_summoned_by(),
        depth=0,
        list_description="this session's summon allow-list",
        identity=identity,
      )
    quest = context.workers.get(peer)
    record = self._records.get(quest) if quest is not None else None
    if record is None:
      raise UnattributablePeer('cannot attribute the requesting peer to a bro')
    return _Requester(
      allow_list=set(record.allow_list),
      credentials=lambda: self._child_credentials(record),
      summoner={'target': record.target, 'trail_id': record.trail_id},
      summoned_by={'trail_id': record.trail_id} if record.trail_id is not None else None,
      depth=record.depth,
      list_description=f"{record.target}'s summon allow-list",
      identity=identity,
    )

  def _child_credentials(self, record: _ActiveSummon) -> ScopedSecrets:
    """the credential scope a summoned child runs with, recomputed from its own
    spawn record."""
    if record.manual:
      # a manual child's actual scope was computed by its own launch, from flags
      # this control never sees — there is no record to recompute it from
      raise UnattributablePeer(
        "a manual child's credential scope is not attributable; grant the "
        'credential at its own launch instead'
      )
    grant, _ = split_scope_overrides(record.grant)
    revoke, _ = split_scope_overrides(record.revoke)
    try:
      return _summoned_scope(
        record.target,
        record.harness,
        record.llm,
        attachment=self._workspace.metadata.repo,
        grant=grant,
        revoke=revoke,
      )
    except ValueError as error:
      raise UnattributablePeer(str(error)) from error

  def _root_summoned_by(self) -> Optional[dict[str, Any]]:
    # Read the session-owned pointer per request because Claude trail segments
    # turn over; the journaled root trail mark is the native/fallback source.
    from bro.monitor.trail_pointer import read, session_pointer

    trail_id = read(session_pointer(self._workspace.path))
    if trail_id is None:
      trail_id = self._root_trail_id
    return {'trail_id': trail_id} if trail_id is not None else None

  def _deny(
    self,
    context: 'Dispatcher',
    peer: 'Peer',
    message: 'Message',
    summoner: Optional[dict[str, Any]],
    error: str,
  ) -> None:
    log.warning('summon: %s: %s', self._workspace.name, error)
    self._denial_summoners[message.quest_id] = summoner
    context.deny(peer, error)

  def observe_journal(self, event: 'Event', journal_record: 'Record') -> None:
    """Maintain summon identity, trail fallback, logging, and manual-token cleanup."""
    if journal_record.kind == 'root':
      if event.transition == 'trail':
        self.note_root_trail(journal_record.trail_id)
        log.info('root run trail %s', journal_record.trail_id)
      elif event.transition == 'ended':
        reason = event.payload.get('reason')
        if reason == 'raised':
          log.warning('root run raised: %s', journal_record.result)
        else:
          log.info('root run ended: %s', event.payload.get('outcome'))
      return
    if journal_record.kind != 'summon':
      return
    if event.transition == 'accepted':
      record = self._pending.pop(event.quest, None)
      if record is None:
        log.warning('summon: accepted quest %s has no authorized identity record', event.quest)
        return
      self._records[event.quest] = record
      action = 'expecting a manual launch' if record.manual else 'spawning'
      log.info(
        'summon: %s %s %s (request %s)', self._workspace.name, action, record.target, event.quest
      )
      return
    record = self._records.get(event.quest)
    if record is None:
      return
    if event.transition == 'trail':
      record.trail_id = journal_record.trail_id
      log.info('summon: %s trail %s', record.target, record.trail_id)
      return
    if event.transition != 'ended':
      return
    if record.manual:
      pending_summon.discard(record.request_id)
    outcome = str(event.payload.get('outcome'))
    reason = event.payload.get('reason')
    if outcome == 'failed' and reason is not None:
      outcome = f'{outcome}:{reason}'
    log.info('summon: %s ended: %s (trail %s)', record.target, outcome, record.trail_id)

  def audit_event(self, event: 'Event', journal_record: 'Record') -> None:
    """Append one human-facing JSONL row for every journal event."""
    entry: dict[str, Any] = {
      'session': self._workspace.name,
      **event.view(),
      'args': journal_record.args,
    }
    record = self._records.get(event.quest) or self._pending.get(event.quest)
    if record is not None:
      entry['summoner'] = record.summoner
      entry['target'] = record.target
    elif event.quest in self._denial_summoners:
      entry['summoner'] = self._denial_summoners[event.quest]
    try:
      self._audit_file.parent.mkdir(parents=True, exist_ok=True)
      with self._audit_file.open('a') as audit:
        audit.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except OSError as error:
      log.warning('could not append summon audit record to %s: %s', self._audit_file, error)
    if event.transition == 'ended':
      self._records.pop(event.quest, None)
    elif event.transition == 'denied':
      self._denial_summoners.pop(event.quest, None)
