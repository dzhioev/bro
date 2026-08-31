"""Quest-keyed identity facts for every peer in one broker root."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from bro.monitor.trail_pointer import read, session_pointer
from bro.workspace.paths import workspace_dir, workspace_tree
from ride import pending_summon
from ride.workspace.store import ScopedSecrets

if TYPE_CHECKING:
  from bro.broker.dispatcher import Dispatcher
  from bro.broker.journal import Event, Journal, Record
  from bro.broker.runtime import Peer

_UNATTRIBUTABLE = 'cannot attribute the requesting peer'


class UnattributablePeer(Exception):
  """A requesting peer with no complete row in the facts table."""


@dataclass
class PeerFact:
  """The launch facts for the peer answering one quest."""

  workspace: Optional[str]
  bro: str
  allow_list: frozenset[str]
  grant: tuple[str, ...] = ()
  revoke: tuple[str, ...] = ()
  llm: Optional[str] = None
  harness: Optional[str] = None
  manual: bool = False
  credential_scope: Optional[ScopedSecrets] = field(default=None, repr=False)


@dataclass(frozen=True)
class PeerIdentity:
  workspace: str
  tree: Path
  manual: bool = False


class PeerFacts:
  """The peer-facts table for one broker root, keyed by answered quest."""

  def __init__(self, root: PeerFact, *, root_tree: Path, root_path: Path):
    if root.workspace is None:
      raise ValueError('the root peer needs a workspace')
    self._root = root
    self._root_tree = root_tree
    self._root_path = root_path
    self._root_quest: Optional[str] = None
    self._facts: dict[str, PeerFact] = {}

  def observe_journal(self, event: 'Event', record: 'Record') -> None:
    """Seed the root row when its host-anchored quest opens."""
    if record.kind != 'root' or event.transition != 'accepted':
      return
    self.add(event.quest, self._root)
    self._root_quest = event.quest

  def add(self, quest: str, fact: PeerFact) -> None:
    if quest in self._facts:
      raise ValueError(f'peer facts already recorded for quest {quest!r}')
    self._facts[quest] = fact

  def note_workspace(self, quest: str, workspace: str) -> None:
    self.for_quest(quest).workspace = workspace

  def for_quest(self, quest: str) -> PeerFact:
    fact = self._facts.get(quest)
    if fact is None:
      raise UnattributablePeer(f'{_UNATTRIBUTABLE} to facts for quest {quest!r}')
    return fact

  def resolve(self, context: 'Dispatcher', peer: 'Peer') -> tuple[str, PeerFact]:
    quest = context.workers.get(peer)
    if quest is None:
      raise UnattributablePeer(f'{_UNATTRIBUTABLE} to an answered quest')
    return quest, self.for_quest(quest)

  def identity(self, context: 'Dispatcher', peer: 'Peer') -> PeerIdentity:
    quest, fact = self.resolve(context, peer)
    workspace = self._workspace(quest, fact)
    tree = self._root_tree if quest == self._root_quest else workspace_tree(workspace)
    return PeerIdentity(workspace, tree, fact.manual)

  def depth(self, context: 'Dispatcher', peer: 'Peer') -> int:
    quest, _ = self.resolve(context, peer)
    return len(context.journal.ancestry(quest))

  def ancestors(self, context: 'Dispatcher', peer: 'Peer') -> tuple[str, ...]:
    quest, _ = self.resolve(context, peer)
    workspaces = []
    for ancestor in context.journal.ancestry(quest):
      fact = self.for_quest(ancestor)
      workspaces.append(self._workspace(ancestor, fact))
    return tuple(workspaces)

  def attribution(self, context: 'Dispatcher', peer: 'Peer') -> dict[str, str]:
    quest, _ = self.resolve(context, peer)
    return self.attribution_for_quest(context.journal, quest)

  def attribution_for_quest(self, journal: 'Journal', quest: str) -> dict[str, str]:
    fact = self.for_quest(quest)
    workspace = self._workspace(quest, fact)
    attribution = {'workspace': workspace, 'bro': fact.bro}
    workspace_path = self._root_path if quest == self._root_quest else workspace_dir(workspace)
    trail_id = read(session_pointer(workspace_path))
    if trail_id is None:
      record = journal.records.get(quest)
      trail_id = record.trail_id if record is not None else None
    if trail_id is not None:
      attribution['trail_id'] = trail_id
    return attribution

  @staticmethod
  def _workspace(quest: str, fact: PeerFact) -> str:
    if fact.workspace is None and fact.manual:
      fact.workspace = pending_summon.claimed_workspace(quest)
    if fact.workspace is None:
      if fact.manual:
        raise UnattributablePeer("the manual child's launch has not claimed its token yet")
      raise UnattributablePeer(f'{_UNATTRIBUTABLE} to a workspace')
    return fact.workspace
