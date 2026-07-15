"""summon, host side: authorization and per-root bookkeeping.

Two layers, both computed per broker root:

- `summon_allow_list` — which bros a session may summon. Every surface that starts
  a broker root (`cw ss` in both modes, the do-CLI container hop) computes the
  session's effective outgoing allow-list here at launch and threads it to
  `run_root_via_broker`.
- `SummonControl` — the root's summon state, wired up by `run_root_via_broker`:
  the `summon` request handler (payload validation, per-peer authorization, the
  immediate denial `reply{error}` plus a deny audit entry, the spawn of a
  `SummonLaunchSpec` with the requesting peer as its parent — everything heavy
  runs off-loop in the spawner, see `cw/spawn.py`), the delivery-tap observer
  that tracks each child's trail id and outcome, and the visibility outputs
  those feed: a host-side log line per event, an append-only JSONL audit file
  (the out-of-band trace a session's own narrative cannot suppress; every entry
  names the actual summoner), and the summon-status file the session's
  statusLine renders (`session-log-statusline` reads it via the
  `CW_SUMMON_STATUS` env var each launch surface points at it).

Authorization is per-peer. The root follows the launch-computed effective list
above; a summoned child follows its own bro's static MRO-collected `may_summon`
seeds — the control attributes the requesting peer to the bro it spawned for it
(the dispatcher's `origin` topology plus this control's own spawn records;
nothing is read from the wire), and a peer it cannot attribute is denied. Grants
never pass through to children: a child's launch is programmatic, so widening
stays a root-session surface. Summons therefore chain transitively wherever the
seeds chain, bounded by `_MAX_SUMMON_DEPTH` — seeds are declared per-bro, so a
seed cycle (a → b → a) would otherwise recurse through real containers.

The same per-request attribution also names the requester's workspace (the root's
from the session key, a child's from its `broker-<channel>` clone), threaded into
the spawn as the child's base-ref inheritance source: a summoned child bases on
its summoner's workspace HEAD unless the request's `into` overrides. The HEAD
read itself is blocking git work and runs off-loop in the spawner
(`cw/spawn.py:_lower_summon`); the handler only resolves the path.

Both state files live under `var/cw/summon/` (`_summon_dir`), keyed by the
session key the launch surface passes: the workspace name, mode-prefixed for a
container session (`c:<name>`, the container-ref convention of `cw/workspace.py`)
and bare on host — a same-name host worktree and container workspace can run
concurrently (the one-session guard is per-mode) and must not interleave one
audit file or clobber one status file. `<key>.jsonl` (audit) and
`<key>.status.json` (live status) sit outside the workspace dirs so the audit
survives a drop, gitignored so neither dirties clean checks. The host process
writes both; a container session reads the status file through its read-only
`/host-repo` mount of the project root (`container_status_path`), a host session
through the host path (`summon_status_file`).

The wire contract (the `summon` tag, payload keys, the 1800s default timeout) is
owned by the peer-side `summon` module; this module enforces it host-side. Broker
imports stay function-local: this module sits on the launch path before the
`_broker_enabled` gate (see cw/CLAUDE.md, "Lazy broker import").
"""

import json
import time
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from base import credentials, log
from cw.paths import _containers_dir, _summon_dir
from cw.workspace import ContainerWorkspace, HostWorktree, _parse_ref
from summon import DEFAULT_TIMEOUT, STATUS_ENV

if TYPE_CHECKING:
  from broker.brotocol import Message
  from broker.dispatcher import Dispatcher
  from broker.runtime import Peer

__all__ = [
  'STATUS_ENV',
  'SummonControl',
  'container_status_path',
  'summon_allow_list',
  'summon_status_file',
]

_PROMPT_HEAD_CHARS = 120
_PAYLOAD_KEYS = frozenset({'target', 'prompt', 'timeout', 'into'})
# the deepest peer a summon may spawn: the root sits at depth 0, its children at
# 1, grandchildren at 2; a request that would nest deeper is denied — the guard
# against seed cycles recursing through real containers (see module docstring).
_MAX_SUMMON_DEPTH = 2


def summon_status_file(project: Path, session: str) -> Path:
  """the session's summon-status file, as the host process writes it."""
  return _summon_dir(project) / f'{session}.status.json'


def container_status_path(project: Path, session: str) -> str:
  """the same file as `summon_status_file`, seen from inside a container through
  its read-only `/host-repo` mount of the project root."""
  return f'/host-repo/{summon_status_file(project, session).relative_to(project)}'


def summon_allow_list(bro_name: str, *, grant: list[str], revoke: list[str]) -> set[str]:
  """the effective summon allow-list of a session running as `bro_name`:
  `(may_summon ∪ grant) − revoke`.

  the seeds are the bro's MRO-collected `may_summon` defaults; `grant`/`revoke`
  are the per-session `--grant-summon`/`--revoke-summon` overrides, applied
  strictly (`credentials.apply_grant_revoke`). every name involved — seed or
  override — must be a registered bro, checked against the `BRO_SPECS` keys
  without importing any target module, so a typo fails the launch immediately
  rather than minutes later as a denied summon. an unknown `bro_name` degrades to
  empty seeds with a warning, mirroring credential scoping (`scoped_secrets`):
  an ambient CW_BRO this checkout doesn't know must not break the launch."""
  # imported here, not at module level: keeps `import cw` free of the bro graph
  # (see cw/CLAUDE.md, "Lazy bro import")
  from bro.bros import BRO_SPECS
  from bro.registry import create_bro

  seeds: tuple[str, ...]
  try:
    seeds = create_bro(bro_name)._may_summon
  except KeyError as e:
    log.warning('could not resolve bro %r for summon scoping: %s', bro_name, e)
    seeds = ()
  unknown = sorted((set(seeds) | set(grant) | set(revoke)) - set(BRO_SPECS))
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
  return None


@dataclass
class _ActiveSummon:
  request_id: str
  target: str
  prompt_head: str
  started_at: float  # epoch seconds of the authorized spawn
  summoner: dict[str, Any]  # audit/status attribution (see _Requester.summoner)
  depth: int  # the spawned child's summon-nesting depth (the root sits at 0)
  trail_id: Optional[str] = None


@dataclass(frozen=True)
class _Requester:
  """the summon identity of a requesting peer, resolved per request.

  `summoner` is the attribution the audit and status entries carry: the root is
  `{'session': <key>}`, a summoned child `{'target': <bro>, 'trail_id': …}`.
  `list_description` names the allow-list in denial messages — the two lists have
  different widening levers (relaunch flags vs seeding the bro), and the denial
  reason should point at the right one. `workspace` is the requester's own
  workspace path — the base-ref inheritance source for the children it summons."""

  allow_list: set[str]
  summoner: dict[str, Any]
  depth: int
  list_description: str
  workspace: Path


class SummonControl:
  """one broker root's summon authorization + bookkeeping (see module docstring).

  `handle` registers as the broker's `summon` handler and `observe_delivery` as a
  delivery observer; both run on the broker loop and do only cheap synchronous
  work. `log_killed_in_flight` runs once the broker loop ends, even when it raises
  — root teardown kills in-flight children without a terminal, and their loss must
  be loud."""

  def __init__(
    self,
    *,
    allow_list: Collection[str],
    session: str,
    project: Path,
    status_file: Path,
    audit_file: Path,
  ):
    self._allow_list = set(allow_list)
    self._session = session
    self._project = project
    self._status_file = status_file
    self._audit_file = audit_file
    self._active: dict[str, _ActiveSummon] = {}  # request id -> in-flight child
    self._last: Optional[dict[str, Any]] = None  # the most recent terminal outcome

  # --- the `summon` request handler (broker loop) -------------------------------

  def handle(self, context: 'Dispatcher', peer: 'Peer', message: 'Message') -> None:
    from cw.spawn import SummonLaunchSpec

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
      from bro.bros import BRO_SPECS  # cheap: already imported to compute the allow-list

      if target not in BRO_SPECS:
        error = f'summon denied: unknown bro {target!r}'
      else:
        error = f'summon denied: {target!r} is not in {requester.list_description}'
      self._deny(context, peer, message, requester.summoner, error)
      return
    timeout = payload.get('timeout')
    prompt = payload['prompt']
    context.spawn(
      SummonLaunchSpec(
        target=target,
        prompt=prompt,
        parent_workspace=requester.workspace,
        into=payload.get('into'),
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
    )
    self._active[message.id] = record
    log.info(
      'summon: %s spawning %s (request %s): %s',
      self._session,
      target,
      message.id,
      record.prompt_head,
    )
    self._audit('spawn', record)
    self._write_status()

  def _requester(self, context: 'Dispatcher', peer: 'Peer') -> Optional['_Requester']:
    """resolve the requesting peer's summon identity: the root follows the
    session's launch-computed effective allow-list; a summoned child follows its
    own bro's static `may_summon` seeds, attributed through the dispatcher's
    `origin` topology and this control's spawn records. None when the peer cannot
    be attributed a bro."""
    if peer == context.root:
      name, is_container = _parse_ref(self._session)
      root_workspace = (
        ContainerWorkspace(name, self._project) if is_container else HostWorktree(name, self._project)
      )  # fmt: skip
      return _Requester(
        allow_list=self._allow_list,
        summoner={'session': self._session},
        depth=0,
        list_description="this session's summon allow-list",
        workspace=root_workspace.path,
      )
    origin = context.origin.get(peer)
    record = self._active.get(origin[1]) if origin is not None else None
    if record is None:
      return None
    # imported function-locally like the launch-time computation (cw/CLAUDE.md,
    # "Lazy bro import"); cheap here — spawning the child already imported its module
    from bro.registry import create_bro

    # function-local like SummonLaunchSpec above: cw.spawn imports broker at
    # module level (cw/CLAUDE.md, "Lazy broker import")
    from cw.spawn import _workspace_name

    return _Requester(
      allow_list=set(create_bro(record.target)._may_summon),
      summoner={'target': record.target, 'trail_id': record.trail_id},
      depth=record.depth,
      list_description=f"{record.target}'s may_summon seeds",
      workspace=_containers_dir(self._project) / _workspace_name(peer),
    )

  def _deny(
    self,
    context: 'Dispatcher',
    peer: 'Peer',
    message: 'Message',
    summoner: Optional[dict[str, Any]],
    error: str,
  ) -> None:
    log.warning('summon: %s: %s', self._session, error)
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
    from broker.brotocol import Tag

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
    self._last = {
      'request_id': record.request_id,  # the reattach handle (`summon check` / `summon list`)
      'target': record.target,
      'trail_id': record.trail_id,
      'summoner': record.summoner,
      'outcome': outcome,
      'ended_at': time.time(),
    }
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
    if outcome is not None:
      entry['outcome'] = outcome
    self._append_audit(event, entry)

  def _append_audit(self, event: str, entry: dict[str, Any]) -> None:
    entry = {
      'time': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
      'event': event,
      'session': self._session,
      **entry,
    }
    try:
      self._audit_file.parent.mkdir(parents=True, exist_ok=True)
      with self._audit_file.open('a') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except OSError as e:
      log.warning('could not append summon audit record to %s: %s', self._audit_file, e)

  def _write_status(self) -> None:
    status = {
      'active': [
        {
          'request_id': record.request_id,
          'target': record.target,
          'trail_id': record.trail_id,
          'summoner': record.summoner,
          'started_at': record.started_at,
        }
        for record in self._active.values()
      ],
      'last': self._last,
    }
    try:
      self._status_file.parent.mkdir(parents=True, exist_ok=True)
      scratch = self._status_file.with_suffix('.tmp')
      scratch.write_text(json.dumps(status, ensure_ascii=False))
      scratch.replace(self._status_file)  # atomic: the statusLine never sees a partial write
    except OSError as e:
      log.warning('could not write summon status file %s: %s', self._status_file, e)
