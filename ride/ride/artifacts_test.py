import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import cast

import pytest

import ride.artifacts
from bro.artifact import GET, MINT, digest_path
from bro.broker import brotocol
from bro.broker.dispatcher import Dispatcher
from bro.kinds import ArtifactDenied
from bro.workspace.paths import CONTAINER_ARTIFACTS_ROOT, workspace_dir, workspace_tree
from ride.artifacts import ArtifactControl, ArtifactStore, JobArtifacts
from ride.peers import PeerIdentity, Peers, UnattributablePeer
from ride.workspace.metadata import WorkspaceKind
from ride.workspace.model import Workspace

ROOT = 'ROOT-CHANNEL'
CHILD = 'CHILD-CHANNEL'
UNKNOWN_REF = f'sha256:{"a" * 64}'


@pytest.fixture
def workspace(tmp_path):
  workspace = Workspace.ensure('ws', tmp_path, WorkspaceKind.CONTAINER)
  workspace_tree('ws').mkdir(parents=True)
  return workspace


@pytest.fixture
def store(workspace):
  return ArtifactStore(workspace, root_in_container=True)


def _root_identity() -> PeerIdentity:
  return PeerIdentity(workspace='ws', tree=workspace_tree('ws'))


def _tree_file(name: str, content: bytes) -> str:
  path = workspace_tree('ws') / name
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_bytes(content)
  return name


def _audit(session: str = 'ws') -> list[dict]:
  lines = ride.artifacts.audit_file(session).read_text().splitlines()
  return [json.loads(line) for line in lines]


class TestMint:
  def test_mints_a_file_and_links_the_minter_view(self, store):
    relative = _tree_file('out/a.bin', b'payload')
    ref, size = store.mint(_root_identity(), (), relative)
    assert ref == digest_path(workspace_tree('ws') / relative)
    assert size == 7
    assert (ride.artifacts.store_dir('ws') / 'objects' / ref).read_bytes() == b'payload'
    assert (ride.artifacts.view_dir('ws', 'ws') / ref).read_bytes() == b'payload'
    [entry] = _audit()
    assert entry['event'] == 'mint'
    assert entry['peer'] == 'ws'
    assert entry['path'] == relative
    assert entry['ref'] == ref
    assert entry['size'] == 7
    assert entry['shared_with'] == ['ws']

  def test_stored_bytes_are_private_to_the_store(self, store):
    relative = _tree_file('a.bin', b'payload')
    ref, _ = store.mint(_root_identity(), (), relative)
    (workspace_tree('ws') / relative).write_bytes(b'REWRITTEN AFTER MINT')
    assert (ride.artifacts.store_dir('ws') / 'objects' / ref).read_bytes() == b'payload'

  def test_reminting_identical_content_is_free(self, store, monkeypatch):
    first = _tree_file('a.bin', b'payload')
    second = _tree_file('b.bin', b'payload')
    ref, size = store.mint(_root_identity(), (), first)
    # the cap admits one copy of the content; the dedup mint still fits, so the
    # re-mint stored nothing new
    monkeypatch.setattr(ride.artifacts, 'MAX_STORE_BYTES', size)
    assert store.mint(_root_identity(), (), second) == (ref, size)
    different = _tree_file('c.bin', b'other!!')
    with pytest.raises(ArtifactDenied, match='byte cap'):
      store.mint(_root_identity(), (), different)

  def test_mints_a_directory_with_normalized_modes(self, store):
    tree = workspace_tree('ws')
    (tree / 'bundle' / 'sub').mkdir(parents=True)
    (tree / 'bundle' / 'data.txt').write_bytes(b'alpha')
    (tree / 'bundle' / 'data.txt').chmod(0o600)
    (tree / 'bundle' / 'sub' / 'tool').write_bytes(b'#!/bin/sh\n')
    (tree / 'bundle' / 'sub' / 'tool').chmod(0o700)
    os.symlink('data.txt', tree / 'bundle' / 'link')
    ref, size = store.mint(_root_identity(), (), 'bundle')
    assert ref == digest_path(tree / 'bundle')
    stored = ride.artifacts.store_dir('ws') / 'objects' / ref
    assert digest_path(stored) == ref
    assert (stored / 'data.txt').stat().st_mode & 0o777 == 0o644
    assert (stored / 'sub' / 'tool').stat().st_mode & 0o777 == 0o755
    assert os.readlink(stored / 'link') == 'data.txt'
    assert size == len(b'alpha') + len(b'#!/bin/sh\n')

  def test_shared_with_covers_the_minter_and_its_ancestors(self, store):
    relative = _tree_file('a.bin', b'payload')
    child = PeerIdentity(workspace='broker-CH', tree=workspace_tree('ws'))
    ref, _ = store.mint(child, ('ws',), relative)
    assert store.reachable(ref, 'broker-CH')
    assert store.reachable(ref, 'ws')
    assert not store.reachable(ref, 'broker-OTHER')

  def test_a_mint_links_the_view_of_every_reader_that_has_one(self, store):
    store.view('broker-CH')
    relative = _tree_file('a.bin', b'payload')
    child = PeerIdentity(workspace='broker-CH', tree=workspace_tree('ws'))
    ref, _ = store.mint(child, ('ws',), relative)
    assert (ride.artifacts.view_dir('ws', 'broker-CH') / ref).read_bytes() == b'payload'
    assert (ride.artifacts.view_dir('ws', 'ws') / ref).read_bytes() == b'payload'

  @pytest.mark.parametrize(
    ('relative', 'error'),
    [
      ('/etc/passwd', 'relative to the workspace root'),
      ('../outside', 'escapes the workspace'),
      ('absent.bin', 'no file or directory'),
    ],
  )
  def test_bad_paths_are_denied(self, store, relative, error):
    with pytest.raises(ArtifactDenied, match=error):
      store.mint(_root_identity(), (), relative)

  def test_an_escaping_symlink_inside_the_tree_is_denied(self, store):
    tree = workspace_tree('ws')
    (tree / 'bundle').mkdir()
    os.symlink('/etc/passwd', tree / 'bundle' / 'evil')
    with pytest.raises(ArtifactDenied, match='escapes the directory'):
      store.mint(_root_identity(), (), 'bundle')
    assert list((ride.artifacts.store_dir('ws') / 'staging').iterdir()) == []

  def test_an_unsupported_entry_is_denied(self, store):
    tree = workspace_tree('ws')
    (tree / 'bundle').mkdir()
    os.mkfifo(tree / 'bundle' / 'pipe')
    with pytest.raises(ArtifactDenied, match='unsupported entry type'):
      store.mint(_root_identity(), (), 'bundle')

  def test_a_mint_past_the_byte_cap_is_denied(self, store, monkeypatch):
    monkeypatch.setattr(ride.artifacts, 'MAX_STORE_BYTES', 3)
    relative = _tree_file('a.bin', b'payload')
    with pytest.raises(ArtifactDenied, match='byte cap'):
      store.mint(_root_identity(), (), relative)


class TestReachAndResolve:
  def test_resolve_answers_the_object_path_for_a_reader(self, store):
    ref, _ = store.mint(_root_identity(), (), _tree_file('a.bin', b'payload'))
    assert store.resolve(ref, 'ws') == ride.artifacts.store_dir('ws') / 'objects' / ref

  def test_denial_is_uniform_between_unknown_and_unshared(self, store):
    ref, _ = store.mint(_root_identity(), (), _tree_file('a.bin', b'payload'))
    with pytest.raises(ArtifactDenied) as unshared:
      store.resolve(ref, 'broker-OTHER')
    with pytest.raises(ArtifactDenied) as unknown:
      store.resolve(UNKNOWN_REF, 'broker-OTHER')
    assert str(unshared.value) == f'artifact {ref} is not shared with this peer'
    assert str(unknown.value) == f'artifact {UNKNOWN_REF} is not shared with this peer'


class TestShare:
  def test_share_extends_reach_and_links_the_target_view(self, store):
    ref, _ = store.mint(_root_identity(), (), _tree_file('a.bin', b'payload'))
    store.share([ref], to='broker-CH', by='ws')
    assert store.reachable(ref, 'broker-CH')
    assert (ride.artifacts.view_dir('ws', 'broker-CH') / ref).read_bytes() == b'payload'
    assert _audit()[-1] == {
      **_audit()[-1],
      'event': 'share',
      'by': 'ws',
      'to': 'broker-CH',
      'refs': [ref],
    }

  def test_sharing_nothing_writes_nothing(self, store):
    store.share([], to='broker-CH', by='ws')
    assert not ride.artifacts.audit_file('ws').exists()


class TestMaterialize:
  def test_a_container_peer_gets_its_view_path(self, store):
    ref, _ = store.mint(_root_identity(), (), _tree_file('a.bin', b'payload'))
    assert store.materialize(_root_identity(), ref) == str(CONTAINER_ARTIFACTS_ROOT / ref)
    assert _audit()[-1]['event'] == 'get'
    assert _audit()[-1]['peer'] == 'ws'
    assert _audit()[-1]['ref'] == ref

  def test_a_shared_child_gets_its_view_path(self, store):
    ref, _ = store.mint(_root_identity(), (), _tree_file('a.bin', b'payload'))
    store.share([ref], to='broker-CH', by='ws')
    child = PeerIdentity(workspace='broker-CH', tree=workspace_tree('broker-CH'))
    assert store.materialize(child, ref) == str(CONTAINER_ARTIFACTS_ROOT / ref)

  def test_the_host_mode_root_gets_a_private_copy(self, workspace):
    store = ArtifactStore(workspace, root_in_container=False)
    ref, _ = store.mint(_root_identity(), (), _tree_file('a.bin', b'payload'))
    path = store.materialize(_root_identity(), ref)
    assert path == str(workspace_dir('ws') / 'artifacts' / ref)
    assert (workspace_dir('ws') / 'artifacts' / ref).read_bytes() == b'payload'
    # the copy shares no inode with the store, and a repeat answers the same copy
    (workspace_dir('ws') / 'artifacts' / ref).write_bytes(b'edited')
    assert (ride.artifacts.store_dir('ws') / 'objects' / ref).read_bytes() == b'payload'
    assert store.materialize(_root_identity(), ref) == path
    assert (workspace_dir('ws') / 'artifacts' / ref).read_bytes() == b'edited'

  def test_a_manual_child_is_denied_with_the_reason(self, store):
    ref, _ = store.mint(_root_identity(), (), _tree_file('a.bin', b'payload'))
    store.share([ref], to='my-manual', by='ws')
    manual = PeerIdentity(workspace='my-manual', tree=workspace_tree('my-manual'), manual=True)
    with pytest.raises(ArtifactDenied, match='manually launched session'):
      store.materialize(manual, ref)

  def test_an_unreachable_ref_is_denied_uniformly(self, store):
    with pytest.raises(ArtifactDenied, match='is not shared with this peer'):
      store.materialize(_root_identity(), UNKNOWN_REF)


class TestLifecycle:
  def test_construction_wipes_a_leftover_store(self, workspace, store):
    ref, _ = store.mint(_root_identity(), (), _tree_file('a.bin', b'payload'))
    fresh = ArtifactStore(workspace, root_in_container=True)
    assert not (ride.artifacts.store_dir('ws') / 'objects' / ref).exists()
    with pytest.raises(ArtifactDenied):
      fresh.resolve(ref, 'ws')

  def test_close_removes_the_store_and_keeps_the_audit(self, store):
    store.mint(_root_identity(), (), _tree_file('a.bin', b'payload'))
    store.close()
    assert not ride.artifacts.store_dir('ws').exists()
    assert [entry['event'] for entry in _audit()] == ['mint']


@dataclass
class FakeContext:
  """the Dispatcher surface the artifact handlers drive: root exposure, the
  worker index, the synchronous denial reply, and the correlated delivery."""

  root: str = ROOT
  workers: dict = field(default_factory=dict)
  replies: list = field(default_factory=list)  # (peer, payload)
  delivered: list = field(default_factory=list)  # (peer, message)

  def reply(self, peer, payload):
    self.replies.append((peer, payload))

  def deliver(self, peer, message):
    self.delivered.append((peer, message))


async def _delivered(context: FakeContext) -> tuple[str, brotocol.Message]:
  for _ in range(1000):
    if len(context.delivered) > 0:
      return context.delivered[-1]
    await asyncio.sleep(0.005)
  raise AssertionError('no result delivered')


@pytest.fixture
def control(workspace, store):
  return ArtifactControl(store, Peers(workspace))


def _context(control) -> FakeContext:
  return FakeContext()


class TestJobArtifacts:
  @pytest.fixture
  def jobs(self, workspace, store):
    return JobArtifacts(store, Peers(workspace))

  def test_a_run_directory_lives_in_the_store(self, jobs):
    directory = jobs.open()
    assert directory.is_dir()
    assert directory.is_relative_to(ride.artifacts.store_dir('ws'))
    assert jobs.open() != directory

  @pytest.mark.asyncio
  async def test_collect_answers_the_ref_the_requester_reaches(self, jobs, store):
    directory = jobs.open()
    (directory / 'stdout').write_bytes(b'ran')
    value = await jobs.collect(directory, cast(Dispatcher, FakeContext()), ROOT)
    assert value == {'ref': digest_path(directory), 'size': 3}
    assert store.reachable(value['ref'], 'ws')
    assert [entry['event'] for entry in _audit()] == ['job']

  @pytest.mark.asyncio
  async def test_an_unattributable_requester_collects_nothing(self, jobs):
    directory = jobs.open()
    (directory / 'stdout').write_bytes(b'ran')
    with pytest.raises(UnattributablePeer):
      await jobs.collect(directory, cast(Dispatcher, FakeContext()), CHILD)


class TestArtifactControl:
  @pytest.mark.asyncio
  async def test_mint_answers_the_correlated_ok_result(self, control):
    _tree_file('out/a.bin', b'payload')
    context = FakeContext()
    message = brotocol.request(MINT, {'path': 'out/a.bin'})
    control.mint(cast(Dispatcher, context), ROOT, message)
    assert context.replies == []
    peer, result = await _delivered(context)
    assert peer == ROOT
    assert result.request == message.id
    assert result.payload['outcome'] == 'ok'
    value = result.payload['value']
    assert value['ref'] == digest_path(workspace_tree('ws') / 'out/a.bin')
    assert value['size'] == 7

  @pytest.mark.asyncio
  async def test_get_answers_the_view_path(self, control, store):
    ref, _ = store.mint(_root_identity(), (), _tree_file('a.bin', b'payload'))
    context = FakeContext()
    message = brotocol.request(GET, {'ref': ref})
    control.get(cast(Dispatcher, context), ROOT, message)
    _, result = await _delivered(context)
    assert result.payload == {
      'outcome': 'ok',
      'value': {'path': str(CONTAINER_ARTIFACTS_ROOT / ref)},
    }

  @pytest.mark.asyncio
  async def test_a_store_refusal_is_the_correlated_denial(self, control):
    context = FakeContext()
    message = brotocol.request(MINT, {'path': 'absent.bin'})
    control.mint(cast(Dispatcher, context), ROOT, message)
    _, result = await _delivered(context)
    assert result.payload['outcome'] == 'denied'
    assert 'no file or directory' in result.payload['error']
    assert _audit()[-1]['event'] == 'deny'

  @pytest.mark.asyncio
  async def test_an_unreachable_get_is_denied_uniformly(self, control):
    context = FakeContext()
    control.get(cast(Dispatcher, context), ROOT, brotocol.request(GET, {'ref': UNKNOWN_REF}))
    _, result = await _delivered(context)
    assert result.payload['outcome'] == 'denied'
    assert result.payload['error'] == f'artifact {UNKNOWN_REF} is not shared with this peer'

  def test_malformed_args_are_denied_synchronously(self, control):
    context = FakeContext()
    control.mint(cast(Dispatcher, context), ROOT, brotocol.request(MINT, {'path': ''}))
    control.mint(cast(Dispatcher, context), ROOT, brotocol.request(MINT, {'path': 'x', 'extra': 1}))  # fmt: skip
    control.get(cast(Dispatcher, context), ROOT, brotocol.request(GET, {'ref': 'nope'}))
    assert [payload['outcome'] for _, payload in context.replies] == ['denied'] * 3
    assert "non-empty string 'path'" in context.replies[0][1]['error']
    assert 'unknown artifact.mint field(s): extra' in context.replies[1][1]['error']
    assert "well-formed 'ref'" in context.replies[2][1]['error']
    assert [entry['event'] for entry in _audit()] == ['deny'] * 3

  def test_an_unattributable_peer_is_denied(self, control):
    context = FakeContext()
    control.mint(cast(Dispatcher, context), CHILD, brotocol.request(MINT, {'path': 'x'}))
    [(peer, payload)] = context.replies
    assert peer == CHILD
    assert payload['outcome'] == 'denied'
    assert 'cannot attribute' in payload['error']

  def test_resolve_serves_kind_handlers_with_the_same_denial(self, workspace, store):
    peers = Peers(workspace)
    control = ArtifactControl(store, peers)
    ref, _ = store.mint(_root_identity(), (), _tree_file('a.bin', b'payload'))
    context = FakeContext()
    resolved = control.resolve(ref, cast(Dispatcher, context), ROOT)
    assert resolved.read_bytes() == b'payload'
    with pytest.raises(ArtifactDenied, match='is not shared with this peer'):
      control.resolve(ref, cast(Dispatcher, context), CHILD)
