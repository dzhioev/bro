"""summon, host side: authorization and per-root bookkeeping.

Two layers, both computed per broker root:

- `summon_allow_list` — which bros a session may summon. Every surface that starts
  a broker root (`ride solo|along` in both modes, the do-CLI container hop) computes the
  session's effective outgoing allow-list here at launch and threads it to
  `run_root_via_broker`.
- `SummonControl` — the root's summon state, wired up by `run_root_via_broker`:
  the `summon` request handler (payload validation, per-peer authorization, the
  immediate denial `reply{error}` plus a deny audit entry, the spawn of a
  `SummonLaunchSpec` with the requesting peer as its parent — everything heavy
  runs off-loop in the spawner, see `ride/ride/spawn.py`), the delivery-tap observer
  that tracks each child's trail id and outcome, and the visibility outputs
  those feed: a host-side log line per event, an append-only JSONL audit file
  (the out-of-band trace a session's own narrative cannot suppress; every entry
  names the actual summoner), and the summon-status file the session's
  statusLine renders (records and atomic write in `bro.summon_status`; each
  launch surface points its session at one through `RIDE_SUMMON_STATUS`).

Authorization is per-peer. The root follows the launch-computed effective list
above; a summoned child follows the list its own summon request resolved — its
bro's static MRO-collected `may_summon` seeds under the request's `@bro`
grant/revoke overrides — recorded at the authorized spawn. The control attributes
the requesting peer to the bro it spawned for it (the dispatcher's `origin`
topology plus this control's own spawn records; nothing is read from the wire),
and a peer it cannot attribute is denied. Provenance rides the same attribution:
a spawned child's `summoned_by` names the requester's trail — the root's from the
session's current-trail pointer (the claude recorder publishes it;
`monitor/trail_pointer.py`) or from the root run's `started` event, a summoned
child's from its spawn record — plus the requester's own `tool_call` step id when
the request payload carries one. Summons therefore chain transitively wherever
the seeds chain, bounded by `_MAX_SUMMON_DEPTH` — seeds are declared per-bro, so
a seed cycle (a → b → a) would otherwise recurse through real containers.

A widening is per request, never implicit, and never an escalation: a requester's
own scope is not inherited — only what its `grant` names reaches the child — and
every granted name must already be in the requester's own scope, so no chain of
summons reaches authority the session was not launched with. Both bounds are the
requester's: `allow_list` for `@bro` values, and its credential scope for the
rest — the root's threaded in at construction from what the launch hydrated, a
summoned child's recomputed from its own spawn record (`_child_credentials`).

The request's `grant`/`revoke` split by kind: `@bro` values resolve here, on the
loop, so a malformed or no-op override is denied immediately, while the unified
values ride the spawn, where the lowering (`ride/ride/spawn.py`) applies the
credential half against the child's own computed scope — a bad override fails
the launch — and records the whole lists in the child's session spec. `harness`
and `llm` are child-facing: they select the child's driving loop and recipe,
ride its recorded session spec and inner argv, and shape its computed scope.

The same per-request attribution also names the requester's workspace (the root's
own, a child's from its `broker-<channel>` clone), threaded into
the spawn as the child's base-ref inheritance source: a summoned child bases on
its summoner's workspace HEAD unless the request's `into` overrides. The HEAD
read itself is blocking git work and runs off-loop in the spawner
(`ride/ride/spawn.py:_lower_summon`); the handler only resolves the path.

Both state files live under `bro.workspace.paths.summon_dir`, keyed by the
workspace name: `<name>.jsonl` (audit) and `<name>.status.json` (live status).
They sit outside the workspace dir so the audit survives a drop. The host process writes both; a container session
reads the status through the dedicated read-only `/var/ride/summon` bind, while a
host session reads the host path.

The wire contract (the `summon` tag, payload keys, the 1800s default timeout) is
owned by the peer-side `bro.summon` module; this module enforces it host-side. Broker
imports stay function-local: this module sits on the launch path before the
`_broker_enabled` gate (see ride/AGENTS.md, "Lazy broker import").
"""

import json
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from bro import summon_status
from bro.base import credentials, log
from bro.summon import DEFAULT_HARNESS, DEFAULT_TIMEOUT
from bro.summon_status import STATUS_ENV
from bro.workspace.model import Workspace
from bro.workspace.paths import CONTAINER_SUMMON_ROOT, summon_dir, workspace_tree
from ride.harness import HARNESS_NAMES
from ride.scope import split_scope_overrides

if TYPE_CHECKING:
  from bro.broker.brotocol import Message
  from bro.broker.dispatcher import Dispatcher
  from bro.broker.runtime import Peer

__all__ = [
  'STATUS_ENV',
  'SummonControl',
  'container_status_path',
  'summon_allow_list',
  'summon_status_file',
]

_PROMPT_HEAD_CHARS = 120
_PAYLOAD_KEYS = frozenset(
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
    'llm',
    'harness',
  }
)
# the deepest peer a summon may spawn: the root sits at depth 0, its children at
# 1, grandchildren at 2; a request that would nest deeper is denied — the guard
# against seed cycles recursing through real containers (see module docstring).
_MAX_SUMMON_DEPTH = 2


def summon_status_file(project: Path, workspace_name: str) -> Path:
  """the session's summon-status file, as the host process writes it."""
  return summon_dir(project) / f'{workspace_name}.status.json'


def container_status_path(workspace_name: str) -> str:
  """the session's status file at its dedicated in-container mount."""
  return str(CONTAINER_SUMMON_ROOT / f'{workspace_name}.status.json')


def summon_allow_list(bro_name: str, *, grant: list[str], revoke: list[str]) -> set[str]:
  """the effective summon allow-list of a session running as `bro_name`:
  `(may_summon ∪ grant) − revoke`.

  the seeds are the bro's MRO-collected `may_summon` defaults; `grant`/`revoke`
  are the per-session `--grant @bro`/`--revoke @bro` overrides, applied
  strictly (`credentials.apply_grant_revoke`). every name involved — seed or
  override — must be a registered bro, checked against `registry.known_names()`
  without importing any target module, so a typo fails the launch immediately
  rather than minutes later as a denied summon. an unknown `bro_name` degrades to
  empty seeds with a warning, mirroring credential scoping (`scoped_secrets`):
  an ambient RIDE_BRO this checkout doesn't know must not break the launch."""
  # imported here, not at module level: the registry import pulls the bro class
  # graph, which the pre-gate launch path must not pay for up front
  from bro.registry import create_bro, known_names

  seeds: tuple[str, ...]
  try:
    seeds = create_bro(bro_name)._may_summon
  except KeyError as e:
    log.warning('could not resolve bro %r for summon scoping: %s', bro_name, e)
    seeds = ()
  unknown = sorted((set(seeds) | set(grant) | set(revoke)) - known_names())
  if len(unknown) > 0:
    raise ValueError(f'unknown summon target(s): {", ".join(unknown)}; not in the bro registry')
  return credentials.apply_grant_revoke(
    seeds, grant=grant, revoke=revoke, subject='summon allow-list'
  )


def _prompt_head(prompt: str) -> str:
  return ' '.join(prompt.split())[:_PROMPT_HEAD_CHARS]


def _validate(payload: dict[str, Any]) -> Optional[str]:
  """the request's shape errors, or None when well-formed. Strict: an unknown key
  is rejected rather than ignored — a typo'd `timout` silently falling back to the
  default would hide the caller's bug."""
  from bro.llm.mcp import HOLDS
  from bro.llm.providers import LLMSelectionError, parse as parse_llm

  unknown = sorted(set(payload) - _PAYLOAD_KEYS)
  if len(unknown) > 0:
    return f'unknown summon field(s): {", ".join(unknown)}'
  for key in ('target', 'prompt'):
    value = payload.get(key)
    if not isinstance(value, str) or len(value) == 0:
      return f'summon needs a non-empty string {key!r}'
  timeout = payload.get('timeout')
  if timeout is not None and (not isinstance(timeout, (int, float)) or timeout <= 0):
    return "summon 'timeout' must be a positive number of seconds"
  into = payload.get('into')
  if into is not None and (not isinstance(into, str) or len(into) == 0):
    return "summon 'into' must be a non-empty git ref"
  hold = payload.get('hold')
  if hold is not None and hold not in HOLDS:
    return f"summon 'hold' must be one of {', '.join(HOLDS)}"
  step_id = payload.get('step_id')
  if step_id is not None and (
    not isinstance(step_id, int) or isinstance(step_id, bool) or step_id < 0
  ):
    return "summon 'step_id' must be a non-negative int"
  index = payload.get('index')
  if index is not None and (
    step_id is None or not isinstance(index, int) or isinstance(index, bool) or index < 0
  ):
    return "summon 'index' requires step_id and must be a non-negative int"
  # grant/revoke are checked on presence, not on non-None: unlike the optional
  # fields above they feed a non-optional consumer (the override split), so a
  # null must be a shape error rather than a default
  for key in ('grant', 'revoke'):
    if key in payload and (
      not isinstance(payload[key], list)
      or not all(isinstance(value, str) and len(value) > 0 for value in payload[key])
    ):
      return f'summon {key!r} must be a list of non-empty names'
  llm = payload.get('llm')
  if llm is not None:
    if not isinstance(llm, str):
      return "summon 'llm' must be a string"
    try:
      parse_llm(llm)
    except LLMSelectionError as error:
      return f"summon 'llm': {error}"
  harness = payload.get('harness')
  if harness is not None and harness not in HARNESS_NAMES:
    return f"summon 'harness' must be one of {', '.join(HARNESS_NAMES)}"
  return None


@dataclass
class _ActiveSummon:
  request_id: str
  target: str
  prompt_head: str
  started_at: float  # epoch seconds of the authorized spawn
  summoner: dict[str, Any]  # audit/status attribution (see _Requester.summoner)
  depth: int  # the spawned child's summon-nesting depth (the root sits at 0)
  allow_list: set[str]  # the spawned child's own effective summon allow-list
  grant: list[str]  # the request's scope overrides, audited as the summoner issued them
  revoke: list[str]
  llm: Optional[str]
  harness: Optional[str]
  trail_id: Optional[str] = None


@dataclass(frozen=True)
class _Requester:
  """the summon identity of a requesting peer, resolved per request.

  `summoner` is the attribution the audit and status entries carry: the root is
  `{'session': <key>}`, a summoned child `{'target': <bro>, 'trail_id': …}`.
  `list_description` names the allow-list in denial messages — the two lists have
  different widening levers (relaunching the session vs the summon that spawned
  the peer, or seeding its bro), and the denial reason should point at the right
  one. `workspace` is the requester's own
  workspace path — the base-ref inheritance source for the children it summons.
  `credentials` reads the peer's own credential scope, the bound on what it may
  grant; it is a thunk because computing a summoned peer's scope is real work
  (bro import + registry read) that only a request carrying a credential grant
  should pay for on the broker loop."""

  allow_list: set[str]
  credentials: Callable[[], set[str]]
  summoner: dict[str, Any]
  summoned_by: Optional[dict[str, Any]]
  depth: int
  list_description: str
  workspace: Path


class SummonControl:
  """one broker root's summon authorization + bookkeeping (see module docstring).

  `handle` registers as the broker's `summon` handler and `observe_delivery` as a
  delivery observer; both run on the broker loop, so everything heavy belongs in
  the spawner and what stays here is kept to the authorization decision itself.
  `log_killed_in_flight` runs once the broker loop ends, even when it raises
  — root teardown kills in-flight children without a terminal, and their loss must
  be loud."""

  def __init__(
    self,
    *,
    allow_list: Collection[str],
    credential_scope: Collection[str] = (),
    workspace: Workspace,
    status_file: Path,
    audit_file: Path,
  ):
    self._allow_list = set(allow_list)
    # the root session's own two scopes bound what its summons may grant a child
    self._credential_scope = set(credential_scope)
    self._workspace = workspace
    self._status_file = status_file
    self._audit_file = audit_file
    self._root_trail_id: Optional[str] = None
    self._active: dict[str, _ActiveSummon] = {}  # request id -> in-flight child
    self._last: Optional[summon_status.FinishedSummon] = None

  def note_root_trail(self, trail_id: Optional[str]) -> None:
    """record the root peer's own trail id (from its `started` lifecycle event)
    as the trail the root's summon children are attributed to."""
    if trail_id is not None:
      self._root_trail_id = trail_id

  # --- the `summon` request handler (broker loop) -------------------------------

  def handle(self, context: 'Dispatcher', peer: 'Peer', message: 'Message') -> None:
    from ride.spawn import SummonLaunchSpec

    payload = message.payload
    requester = self._requester(context, peer)
    if requester is None:
      self._deny(
        context, peer, message, None, 'summon denied: cannot attribute the requesting peer to a bro'
      )
      return
    error = _validate(payload)
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
    target = payload['target']
    if target not in requester.allow_list:
      from bro.registry import known_names

      if target not in known_names():
        error = f'summon denied: unknown bro {target!r}'
      else:
        error = f'summon denied: {target!r} is not in {requester.list_description}'
      self._deny(context, peer, message, requester.summoner, error)
      return
    grant = payload.get('grant', [])
    revoke = payload.get('revoke', [])
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
    if len(grant_credentials) > 0:
      beyond = sorted(set(grant_credentials) - requester.credentials())
      if len(beyond) > 0:
        self._deny(
          context,
          peer,
          message,
          requester.summoner,
          f'summon denied: cannot grant credential(s) the summoner does not '
          f'hold: {", ".join(beyond)}',
        )
        return
    timeout = payload.get('timeout')
    prompt = payload['prompt']
    summoned_by = requester.summoned_by
    step_id = payload.get('step_id')
    if summoned_by is not None and step_id is not None:
      summoned_by = {**summoned_by, 'step_id': step_id}
      if payload.get('index') is not None:
        summoned_by['index'] = payload['index']
    context.spawn(
      SummonLaunchSpec(
        target=target,
        prompt=prompt,
        parent_workspace=requester.workspace,
        summoner=summoned_by,
        may_summon=tuple(sorted(child_allow_list)),
        into=payload.get('into'),
        hold=payload.get('hold'),
        grant=tuple(grant),
        revoke=tuple(revoke),
        llm=payload.get('llm'),
        harness=payload.get('harness'),
      ),
      peer,
      timeout=float(timeout) if timeout is not None else DEFAULT_TIMEOUT,
    )
    record = _ActiveSummon(
      request_id=message.id,
      target=target,
      prompt_head=_prompt_head(prompt),
      started_at=time.time(),
      summoner=requester.summoner,
      depth=requester.depth + 1,
      allow_list=child_allow_list,
      grant=list(grant),
      revoke=list(revoke),
      llm=payload.get('llm'),
      harness=payload.get('harness'),
    )
    self._active[message.id] = record
    log.info(
      'summon: %s spawning %s (request %s): %s',
      self._workspace.name,
      target,
      message.id,
      record.prompt_head,
    )
    self._audit('spawn', record)
    self._write_status()

  def _requester(self, context: 'Dispatcher', peer: 'Peer') -> Optional['_Requester']:
    """resolve the requesting peer's summon identity: the root follows the
    session's launch-computed effective allow-list; a summoned child follows the
    list its own summon resolved, attributed through the dispatcher's `origin`
    topology and this control's spawn records. None when the peer cannot be
    attributed a bro."""
    if peer == context.root:
      return _Requester(
        allow_list=self._allow_list,
        credentials=lambda: self._credential_scope,
        summoner={'session': self._workspace.name},
        summoned_by=self._root_summoned_by(),
        depth=0,
        list_description="this session's summon allow-list",
        workspace=self._workspace.tree,
      )
    origin = context.origin.get(peer)
    record = self._active.get(origin[1]) if origin is not None else None
    if record is None:
      return None
    # function-local like SummonLaunchSpec above: ride.spawn imports broker at
    # module level (ride/AGENTS.md, "Lazy broker import")
    from ride.spawn import _workspace_name

    return _Requester(
      allow_list=set(record.allow_list),
      credentials=lambda: self._child_credentials(record),
      summoner={'target': record.target, 'trail_id': record.trail_id},
      summoned_by={'trail_id': record.trail_id} if record.trail_id is not None else None,
      depth=record.depth,
      list_description=f"{record.target}'s summon allow-list",
      workspace=workspace_tree(self._workspace.project, _workspace_name(peer)),
    )

  def _child_credentials(self, record: _ActiveSummon) -> set[str]:
    """the credential scope a summoned child runs with, recomputed from its own
    spawn record through the same helper that lowered its launch."""
    from ride.harness import get_harness
    from ride.scope import summoned_credential_scope

    harness = get_harness(record.harness if record.harness is not None else DEFAULT_HARNESS)
    grant, _ = split_scope_overrides(record.grant)
    revoke, _ = split_scope_overrides(record.revoke)
    scoped = summoned_credential_scope(
      record.target,
      harness.scope_recipe(harness.default_options()),
      grant=grant,
      revoke=revoke,
      llm_spec=harness.resolve_llm(record.llm, record.target),
    )
    return scoped.required | scoped.optional

  def _root_summoned_by(self) -> Optional[dict[str, Any]]:
    # the root's trail attribution source, per publication channel
    # (monitor/trail_pointer.py): a claude session's recorder publishes its
    # current trail at the workspace's session pointer — read per request,
    # since the recorder opens a new trail per recorder lifetime and the
    # pointer moves — while a bro-run root announces its trail in the `started`
    # lifecycle event, noted via note_root_trail. absent both (the early-launch
    # race before transcript adoption, or no recorder at all), provenance
    # degrades to no pointer, never a legacy-shaped one.
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
    context.reply(peer, {'error': error})
    entry: dict[str, Any] = {
      'request_id': message.id,
      'reason': error,
      'summoner': summoner,
    }
    target = message.payload.get('target')
    if isinstance(target, str):
      entry['target'] = target
    prompt = message.payload.get('prompt')
    if isinstance(prompt, str):
      entry['prompt_head'] = _prompt_head(prompt)
    self._append_audit('deny', entry)

  # --- the delivery-tap observer (broker loop) -----------------------------------

  def observe_delivery(self, source: Optional['Peer'], target: 'Peer', message: 'Message') -> None:
    del source, target  # a summon is identified by its request correlation alone
    from bro.broker.brotocol import Tag

    if message.in_reply_to is None:
      return
    record = self._active.get(message.in_reply_to)
    if record is None:
      return
    if message.type == Tag.STARTED:
      record.trail_id = message.payload.get('trail_id')
      log.info('summon: %s started (trail %s)', record.target, record.trail_id)
      self._write_status()
      return
    if message.type == Tag.COMPLETED:
      self._finish(record, str(message.payload.get('end_reason')))
      return
    if message.type == Tag.FAILED:
      self._finish(record, f'failed:{message.payload.get("reason")}')
      return

  def _finish(self, record: _ActiveSummon, outcome: str) -> None:
    del self._active[record.request_id]
    self._last = summon_status.FinishedSummon(
      request_id=record.request_id,
      target=record.target,
      trail_id=record.trail_id,
      summoner=record.summoner,
      outcome=outcome,
      ended_at=time.time(),
    )
    log.info('summon: %s ended: %s (trail %s)', record.target, outcome, record.trail_id)
    self._audit('end', record, outcome=outcome)
    self._write_status()

  # --- teardown (after the broker loop returns) -----------------------------------

  def log_killed_in_flight(self) -> None:
    if len(self._active) == 0:
      return  # nothing killed, and a summon-less session never writes state files
    for record in list(self._active.values()):
      log.warning(
        'summon: root exit killed in-flight child %s (request %s, trail %s)',
        record.target,
        record.request_id,
        record.trail_id,
      )
      self._audit('end', record, outcome='killed')
    self._active.clear()
    self._write_status()

  # --- the visibility outputs ------------------------------------------------------

  def _audit(self, event: str, record: _ActiveSummon, *, outcome: Optional[str] = None) -> None:
    entry: dict[str, Any] = {
      'request_id': record.request_id,
      'summoner': record.summoner,
      'target': record.target,
      'prompt_head': record.prompt_head,
      'trail_id': record.trail_id,
    }
    if len(record.grant) > 0:
      entry['grant'] = record.grant
    if len(record.revoke) > 0:
      entry['revoke'] = record.revoke
    if record.harness is not None:
      entry['harness'] = record.harness
    if outcome is not None:
      entry['outcome'] = outcome
    self._append_audit(event, entry)

  def _append_audit(self, event: str, entry: dict[str, Any]) -> None:
    entry = {
      'time': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
      'event': event,
      'session': self._workspace.name,
      **entry,
    }
    try:
      self._audit_file.parent.mkdir(parents=True, exist_ok=True)
      with self._audit_file.open('a') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except OSError as e:
      log.warning('could not append summon audit record to %s: %s', self._audit_file, e)

  def _write_status(self) -> None:
    status = summon_status.SummonStatus(
      active=tuple(
        summon_status.ActiveSummon(
          request_id=record.request_id,
          target=record.target,
          trail_id=record.trail_id,
          summoner=record.summoner,
          started_at=record.started_at,
        )
        for record in self._active.values()
      ),
      last=self._last,
    )
    try:
      summon_status.write(self._status_file, status)
    except OSError as e:
      log.warning('could not write summon status file %s: %s', self._status_file, e)
