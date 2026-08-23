"""peer → workspace attribution for one broker root, shared by summon and
artifacts.

A peer is attributed through two links: the live hop — the dispatcher's worker
index maps a requesting peer to the exchange it answers, so only a live peer
resolves — and the summon records this registry accumulates, one per
authorized summon, carrying the parent exchange and the child's workspace
name. Records are never dropped, so the chain of summoners above a peer stays
resolvable after a mid-chain summoner exits. Every name is host-derived —
nothing a peer says on the wire attributes it: a spawned child's workspace is
noted by the spawner before its container starts — before the peer can
connect — so attribution never races the launch, and a manual child's is read
from the claimed record its own launch wrote (`ride/ride/pending_summon.py`),
in place before the child can attach. Everything here runs on the broker
loop.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from bro.workspace.paths import workspace_tree
from ride import pending_summon
from ride.workspace.model import Workspace

if TYPE_CHECKING:
  from bro.broker.dispatcher import Dispatcher
  from bro.broker.runtime import Peer

_UNATTRIBUTABLE = 'cannot attribute the requesting peer to a workspace'


class UnattributablePeer(Exception):
  """a requesting peer that cannot be attributed a workspace. The message is
  the denial reason."""


@dataclass(frozen=True)
class PeerIdentity:
  """one peer's workspace attribution: the name is its identity in audits,
  views, and sharing; `manual` marks a user-launched child, whose container
  the host did not build."""

  workspace: str
  tree: Path
  manual: bool = False


@dataclass
class _Summoned:
  parent: Optional[str]  # the exchange the summoner answers; None — summoned by the root
  manual: bool
  workspace: Optional[str] = None


class Peers:
  def __init__(self, workspace: Workspace):
    self._root = workspace
    self._summoned: dict[str, _Summoned] = {}  # by exchange; kept for the session

  def note_summon(
    self, context: 'Dispatcher', requester: 'Peer', exchange: str, *, manual: bool = False
  ) -> None:
    """record an authorized summon: the child answering `exchange` was
    requested by `requester`."""
    if requester == context.root:
      parent = None
    else:
      parent = context.workers.get(requester)
      if parent is None:
        raise UnattributablePeer(_UNATTRIBUTABLE)
    self._summoned[exchange] = _Summoned(parent=parent, manual=manual)

  def note_workspace(self, exchange: str, name: str) -> None:
    """record the workspace name of the child answering `exchange`."""
    self._summoned[exchange].workspace = name

  def _record(self, context: 'Dispatcher', peer: 'Peer') -> tuple[str, _Summoned]:
    exchange = context.workers.get(peer)
    record = self._summoned.get(exchange) if exchange is not None else None
    if exchange is None or record is None:
      raise UnattributablePeer(_UNATTRIBUTABLE)
    return exchange, record

  def _resolved_workspace(self, exchange: str, record: _Summoned) -> Optional[str]:
    if record.workspace is None and record.manual:
      record.workspace = pending_summon.claimed_workspace(exchange)
    return record.workspace

  def identity(self, context: 'Dispatcher', peer: 'Peer') -> PeerIdentity:
    """resolve a requesting peer's workspace; raises `UnattributablePeer` for
    one that cannot be attributed."""
    if peer == context.root:
      return PeerIdentity(workspace=self._root.name, tree=self._root.tree)
    exchange, record = self._record(context, peer)
    name = self._resolved_workspace(exchange, record)
    if name is None:
      if record.manual:
        raise UnattributablePeer("the manual child's launch has not claimed its token yet")
      raise UnattributablePeer(_UNATTRIBUTABLE)
    return PeerIdentity(workspace=name, tree=workspace_tree(name), manual=record.manual)

  def workspace_for(self, exchange: str) -> Optional[str]:
    """the workspace name of the child answering `exchange`, or None for an
    exchange no summon record names (or a manual child yet unclaimed)."""
    record = self._summoned.get(exchange)
    if record is None:
      return None
    return self._resolved_workspace(exchange, record)

  def ancestors(self, context: 'Dispatcher', peer: 'Peer') -> tuple[str, ...]:
    """the workspace names of the peer's summoners, nearest first, ending with
    the session root; empty for the root itself."""
    if peer == context.root:
      return ()
    _, record = self._record(context, peer)
    names: list[str] = []
    parent = record.parent
    while parent is not None:
      ancestor = self._summoned[parent]
      name = self._resolved_workspace(parent, ancestor)
      if name is None:
        raise UnattributablePeer(_UNATTRIBUTABLE)
      names.append(name)
      parent = ancestor.parent
    names.append(self._root.name)
    return tuple(names)
