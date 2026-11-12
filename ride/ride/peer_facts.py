"""Mission-keyed descriptions for every peer in one broker root."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from bro.authority.records import dump_values
from bro.authority.rules import Mission, Prepared
from bro.monitor.trail_pointer import read, session_pointer
from bro.worker_types import Extension, PeerDescription, UnattributablePeer
from bro.workspace.paths import workspace_dir
from ride import pending_launch
from ride.authority_reading import SummonAllowance
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
  records: Path | None = None
  expected: bool = False
  artifact_view: PurePosixPath | None = None
  published_ports: tuple[tuple[int, int], ...] = ()
  extension: Extension | None = None
  record: Mission | Prepared | None = None
  allowance: SummonAllowance | None = None
  pending_override: dict[str, Any] | None = None
  credential_applier: Any = None


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
    if record.kind == 'root' and event.transition == 'accepted':
      self.add(event.mission, self._root)
      self._root_mission = event.mission
    if event.transition in ('started', 'ended'):
      facts = self._facts.get(event.mission)
      if facts is not None and facts.workspace is None and facts.expected:
        facts.workspace = pending_launch.claimed_workspace(event.mission)

  def add(self, mission: str, facts: WorkerFacts) -> None:
    if mission in self._facts:
      raise ValueError(f'peer facts already recorded for mission {mission!r}')
    self._facts[mission] = facts

  def note_workspace(
    self,
    mission: str,
    workspace: str,
    *,
    records: Path | None = None,
    artifact_view: PurePosixPath | None = None,
  ) -> None:
    facts = self.for_mission(mission)
    facts.workspace = workspace
    facts.records = records
    facts.artifact_view = artifact_view

  def note_published_ports(self, mission: str, ports: tuple[tuple[int, int], ...]) -> None:
    self.for_mission(mission).published_ports = ports

  def for_mission(self, mission: str) -> WorkerFacts:
    facts = self._facts.get(mission)
    if facts is None:
      raise UnattributablePeer(f'{_UNATTRIBUTABLE} to facts for mission {mission!r}')
    return facts

  def __contains__(self, mission: str) -> bool:
    return mission in self._facts

  def records_directory(self, mission: str) -> Path:
    facts = self.for_mission(mission)
    if facts.records is not None:
      return facts.records
    if mission == self._root_mission:
      return self._root_path
    return workspace_dir(self._workspace(mission, facts))

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
    return PeerDescription(
      mission=mission,
      workspace=workspace,
      tree=tree,
      type=facts.type,
      expected=facts.expected,
      artifact_view=facts.artifact_view,
      published_ports=facts.published_ports,
      depth=len(journal.ancestry(mission)),
      extension=facts.extension,
      mission_record=facts.record if isinstance(facts.record, Mission) else None,
    )

  def missions(self) -> list[dict[str, Any]]:
    if self._journal is None:
      raise RuntimeError('peer facts have no journal')
    missions = []
    for mission, facts in self._facts.items():
      if facts.workspace is None and facts.expected:
        facts.workspace = pending_launch.claimed_workspace(mission)
      record = self._journal.records.get(mission)
      view = record.view() if record is not None else self._journal.evicted_view(mission)
      if view is None:
        raise RuntimeError(f'mission {mission!r} has facts but no journal record')
      missions.append(
        {
          'id': mission,
          'parent': view['parent'],
          'type': facts.type,
          'state': view['state'],
          'workspace': facts.workspace,
          'options': (
            dump_values(facts.record.descriptor.values)
            if isinstance(facts.record, Mission)
            else None
          ),
          'extension': None if facts.extension is None else dict(facts.extension.view()),
        }
      )
    return missions

  def ancestors(self, context: Dispatcher, peer: Peer) -> tuple[str, ...]:
    description = self.resolve(context, peer)
    workspaces = []
    for ancestor in context.journal.ancestry(description.mission):
      facts = self.for_mission(ancestor)
      workspaces.append(self._workspace(ancestor, facts))
    return tuple(workspaces)

  def attribution(self, context: Dispatcher, peer: Peer) -> dict[str, Any]:
    return self.attribution_for_mission(context.journal, self.resolve(context, peer).mission)

  def attribution_for_mission(self, journal: Journal, mission: str) -> dict[str, Any]:
    description = self._description(mission, journal)
    attribution: dict[str, Any] = {'workspace': description.workspace, 'type': description.type}
    if description.extension is not None:
      attribution['extension'] = dict(description.extension.view())
    trail_id = read(session_pointer(self.records_directory(mission)))
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
