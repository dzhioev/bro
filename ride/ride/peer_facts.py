"""Mission-keyed descriptions for every peer in one broker root."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from bro.monitor import party_member_dir
from bro.monitor.trail_pointer import read, session_pointer
from bro.worker_types import PeerDescription, UnattributablePeer
from bro.workspace.paths import workspace_dir
from ride import pending_launch
from ride.workspace.model import Workspace

if TYPE_CHECKING:
  from bro.broker.dispatcher import Dispatcher
  from bro.broker.journal import Event, Journal, Record
  from bro.broker.runtime import Peer

_UNATTRIBUTABLE = 'cannot attribute the requesting peer'


@dataclass
class WorkerFacts:
  type: str
  workspace: str | None
  tree: Path | None = None
  member: str | None = None
  permits: frozenset[str] = frozenset()
  expected: bool = False
  artifact_view: PurePosixPath | None = None
  published_ports: tuple[tuple[int, int], ...] = ()
  extension: Any = None


class PeerFacts:
  """The worker-facts table for one broker root, keyed by mission."""

  def __init__(self, root: WorkerFacts, *, root_tree: Path, root_path: Path):
    if root.workspace is None:
      raise ValueError('the root peer needs a workspace')
    self._root = root
    self._root_tree = root_tree
    self._root_path = root_path
    self._root_mission: str | None = None
    self._facts: dict[str, WorkerFacts] = {}
    self._journal: Journal | None = None

  def bind_journal(self, journal: Journal) -> None:
    if self._journal is not None:
      raise ValueError('peer facts already have a journal')
    self._journal = journal

  def observe_journal(self, event: Event, record: Record) -> None:
    if record.kind != 'root' or event.transition != 'accepted':
      return
    self.add(event.mission, self._root)
    self._root_mission = event.mission

  def add(self, mission: str, facts: WorkerFacts) -> None:
    if mission in self._facts:
      raise ValueError(f'peer facts already recorded for mission {mission!r}')
    self._facts[mission] = facts

  def note_workspace(
    self,
    mission: str,
    workspace: str,
    *,
    artifact_view: PurePosixPath | None = None,
  ) -> None:
    facts = self.for_mission(mission)
    facts.workspace = workspace
    facts.member = None
    facts.artifact_view = artifact_view

  def note_member(
    self,
    mission: str,
    workspace: str,
    member: str,
    *,
    artifact_view: PurePosixPath | None,
  ) -> None:
    facts = self.for_mission(mission)
    facts.workspace = workspace
    facts.member = member
    facts.artifact_view = artifact_view

  def note_published_ports(self, mission: str, ports: tuple[tuple[int, int], ...]) -> None:
    self.for_mission(mission).published_ports = ports

  def for_mission(self, mission: str) -> WorkerFacts:
    facts = self._facts.get(mission)
    if facts is None:
      raise UnattributablePeer(f'{_UNATTRIBUTABLE} to facts for mission {mission!r}')
    return facts

  def describe(self, mission_id: str) -> PeerDescription:
    if self._journal is None:
      raise RuntimeError('peer facts have no journal')
    return self._description(mission_id, self._journal)

  def resolve(self, context: Dispatcher, peer: Peer) -> PeerDescription:
    mission = context.workers.get(peer)
    if mission is None:
      raise UnattributablePeer(f'{_UNATTRIBUTABLE} to an undertaken mission')
    return self._description(mission, context.journal)

  def _description(self, mission: str, journal: Journal) -> PeerDescription:
    facts = self.for_mission(mission)
    workspace = self._workspace(mission, facts)
    tree = self._tree(mission, workspace, facts)
    bro = getattr(facts.extension, 'bro', None)
    if bro is not None and not isinstance(bro, str):
      raise UnattributablePeer(f'{_UNATTRIBUTABLE} to a bro')
    return PeerDescription(
      mission=mission,
      workspace=workspace,
      tree=tree,
      type=facts.type,
      bro=bro,
      permits=facts.permits,
      member=facts.member,
      expected=facts.expected,
      artifact_view=facts.artifact_view,
      published_ports=facts.published_ports,
      depth=len(journal.ancestry(mission)),
      extension=facts.extension,
    )

  def ancestors(self, context: Dispatcher, peer: Peer) -> tuple[str, ...]:
    description = self.resolve(context, peer)
    workspaces = []
    for ancestor in context.journal.ancestry(description.mission):
      facts = self.for_mission(ancestor)
      workspaces.append(self._workspace(ancestor, facts))
    return tuple(workspaces)

  def attribution(self, context: Dispatcher, peer: Peer) -> dict[str, str]:
    return self.attribution_for_mission(context.journal, self.resolve(context, peer).mission)

  def attribution_for_mission(self, journal: Journal, mission: str) -> dict[str, str]:
    description = self._description(mission, journal)
    attribution = {'workspace': description.workspace, 'type': description.type}
    if description.bro is not None:
      attribution['bro'] = description.bro
    if description.member is not None:
      attribution['member'] = description.member
    workspace_path = (
      self._root_path if mission == self._root_mission else workspace_dir(description.workspace)
    )
    records_path = (
      workspace_path
      if description.member is None
      else party_member_dir(workspace_path, description.member)
    )
    trail_id = read(session_pointer(records_path))
    if trail_id is None:
      record = journal.records.get(mission)
      trail_id = record.trail_id if record is not None else None
    if trail_id is not None:
      attribution['trail_id'] = trail_id
    return attribution

  @staticmethod
  def _workspace(mission: str, facts: WorkerFacts) -> str:
    if facts.workspace is None and facts.expected:
      facts.workspace = pending_launch.claimed_workspace(mission)
    if facts.workspace is None:
      if facts.expected:
        raise UnattributablePeer("the expected worker's launch has not claimed its token yet")
      raise UnattributablePeer(f'{_UNATTRIBUTABLE} to a workspace')
    return facts.workspace

  def _tree(self, mission: str, workspace: str, facts: WorkerFacts) -> Path:
    if facts.tree is not None:
      return facts.tree
    if mission == self._root_mission:
      return self._root_tree
    return Workspace.open(workspace).tree
