"""artifact sharing, host side: the session store and the two request kinds.

`ArtifactStore` is the session-scoped content-addressed store under
`<runtime-root>/artifacts/<session>/`. Ingest stages a private copy of the
peer-named tree path — a reflink where the filesystem clones, a plain copy
otherwise, never a hardlink, so nothing a producer writes afterwards reaches
stored bytes — normalizes modes to the manifest's vocabulary (0o755/0o644 per
the recorded executable bit), digests the copy (`bro.artifact.digest_path`),
and commits it to `objects/<ref>`, deduplicating by digest and refusing a mint
past the byte cap rather than evicting. Committed content is immutable by
construction, so the read path re-verifies nothing.

Each container peer has a view directory `shared/<workspace>/` holding one
hardlink (or hardlinked tree) per ref it may reach — the source of its
read-only `/var/ride/artifacts` bind mount, so a ref linked while the peer
runs appears without a remount. A mint links the minter and its summoners up
to the root; a summon's `share` list is linked into the child's view during
spawn lowering (`ride/ride/spawn.py`). The host-mode root has no mount
namespace: its `get` falls back to a private copy under the workspace's own
`artifacts/` directory. A manually launched child has no host-built launch
and therefore no view; its `get` is denied with the reason.

The store is wiped at construction and removed at `close()` — it dies with
the session — while the JSONL audit beside it (`<session>.jsonl`) survives,
recording mints, gets, shares, and denials.

`JobArtifacts` collects a broker job's run directory the same way: the run is
staged under the store's `jobs/`, and the ref that closes the job's exchange
reaches the peer that requested the job and its summoners.

`ArtifactControl` serves the `artifact.mint` / `artifact.get` kinds
(contract: `bro/artifact.py`) and implements the contributed-kind resolver
(`bro.kinds.ArtifactResolver`). Attribution and shape validation run on the
broker loop; store I/O runs in a thread with the correlated result delivered
from a done-callback. The exchange never enters the dispatcher's table, so
exactly-one-result is this module's duty: the callback folds a store refusal
into `result{denied}` and any other exception into `result{failed}` instead
of letting it vanish. A sharing denial is uniform — identical whether or not
the ref exists. Broker imports stay function-local: `ride/ride/root.py`
composes the root view mount from the path helpers here before the broker
gate."""

import asyncio
import contextlib
import fcntl
import json
import os
import shutil
import threading
import time
from collections.abc import Callable, Generator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from bro.artifact import digest_path, is_ref
from bro.base import log
from bro.base.lulid import lulid
from bro.kinds import ArtifactDenied, tree_path
from bro.workspace.paths import CONTAINER_ARTIFACTS_ROOT, artifacts_dir, workspace_dir
from ride.peers import PeerIdentity, Peers, UnattributablePeer

if TYPE_CHECKING:
  from bro.broker.brotocol import Message
  from bro.broker.dispatcher import Dispatcher
  from bro.broker.runtime import Peer
  from ride.workspace.model import Workspace

# refusal bound on the store's committed bytes — a runaway-ingest guard, not a
# quota (the store dies with the session)
MAX_STORE_BYTES = 32 << 30
# the Linux ioctl that clones a file's extents; any refusal (another OS, a
# filesystem that cannot clone, a cross-device pair) falls back to a plain copy
_FICLONE = 0x40049409

_MINT_KEYS = frozenset({'path'})
_GET_KEYS = frozenset({'ref'})


def store_dir(session: str) -> Path:
  """the session's artifact store directory."""
  return artifacts_dir() / session


def audit_file(session: str) -> Path:
  """the session's artifact audit, beside the store it outlives."""
  return artifacts_dir() / f'{session}.jsonl'


def view_dir(session: str, peer_workspace: str) -> Path:
  """a peer's view directory inside the session store."""
  return store_dir(session) / 'shared' / peer_workspace


def view_mount(session: str, peer_workspace: str) -> str:
  """a peer's read-only view bind mount, as a docker mount spec."""
  return f'{view_dir(session, peer_workspace)}:{CONTAINER_ARTIFACTS_ROOT}:ro'


def _denial(ref: str) -> str:
  return f'artifact {ref} is not shared with this peer'


def _clone_or_copy_file(source: Path, destination: Path) -> int:
  with source.open('rb') as source_file, destination.open('wb') as destination_file:
    try:
      fcntl.ioctl(destination_file.fileno(), _FICLONE, source_file.fileno())
    except OSError:
      shutil.copyfileobj(source_file, destination_file)
  executable = source.stat().st_mode & 0o111 != 0
  destination.chmod(0o755 if executable else 0o644)
  return destination.stat().st_size


def _private_copy(source: Path, destination: Path, prefix: str = '') -> int:
  """copy `source` (a file or directory) to `destination` with no inode shared
  with the source, returning the copied file bytes; raises `ValueError` on an
  entry that is neither file, directory, nor symlink."""
  if source.is_file() and not source.is_symlink():
    return _clone_or_copy_file(source, destination)
  total = 0
  destination.mkdir()
  destination.chmod(0o755)
  for entry in sorted(os.scandir(source), key=lambda scanned: scanned.name):
    target = destination / entry.name
    inner = f'{prefix}{entry.name}'
    if entry.is_symlink():
      os.symlink(os.readlink(entry.path), target)
    elif entry.is_dir(follow_symlinks=False):
      total += _private_copy(Path(entry.path), target, f'{inner}/')
    elif entry.is_file(follow_symlinks=False):
      total += _clone_or_copy_file(Path(entry.path), target)
    else:
      raise ValueError(f'unsupported entry type at {inner}')
  return total


@contextlib.contextmanager
def _staged(directory: Path) -> Generator[Path]:
  """a fresh staging path under `directory`, removed — file or tree — when the
  block ends; a stage the block renamed away is left alone."""
  staging = directory / f'.{lulid()}'
  try:
    yield staging
  finally:
    if staging.is_dir():
      shutil.rmtree(staging)
    elif staging.exists():
      staging.unlink()


def _content_size(source: Path) -> int:
  """the file bytes under `source` — the pre-copy cap check and the dedup
  answer; a fresh ingest reserves the copy's own count instead."""
  if source.is_file():
    return source.stat().st_size
  total = 0
  for directory, _, file_names in os.walk(source):
    for name in file_names:
      path = Path(directory) / name
      if not path.is_symlink() and path.is_file():
        total += path.stat().st_size
  return total


class ArtifactStore:
  """one session's content-addressed store (see the module docstring). The
  metadata lock guards the reach map and the byte account; content operations
  run outside it, so a mint hashing for seconds never blocks a loop-side
  check."""

  def __init__(self, workspace: 'Workspace', *, root_in_container: bool):
    self.session = workspace.name
    self._root_in_container = root_in_container
    self._lock = threading.Lock()
    self._audit_lock = threading.Lock()
    self._reach: dict[str, set[str]] = {}  # ref -> workspace names that may read it
    self._bytes = 0
    root = store_dir(workspace.name)
    if root.exists():
      shutil.rmtree(root)  # a leftover from a crashed session; the store is session-scoped
    self._objects = root / 'objects'
    self._staging = root / 'staging'
    self._views = root / 'shared'
    self._jobs = root / 'jobs'
    for directory in (self._objects, self._staging, self._views, self._jobs):
      directory.mkdir(parents=True)
    self._audit_file = audit_file(workspace.name)
    if root_in_container:
      self.view(workspace.name)

  def close(self) -> None:
    """remove the store — the session is over; the audit stays."""
    try:
      shutil.rmtree(store_dir(self.session))
    except OSError as e:
      log.warning('could not remove the artifact store %s: %s', store_dir(self.session), e)

  def view(self, peer_workspace: str) -> Path:
    """the peer's view directory, created on first use — the source of its
    read-only in-container mount."""
    directory = self._views / peer_workspace
    directory.mkdir(exist_ok=True)
    return directory

  def job_run(self) -> Path:
    """a fresh directory for one broker job's run — inside the store, so an
    uncollected run dies with the session."""
    directory = self._jobs / lulid()
    directory.mkdir()
    return directory

  def mint(self, identity: PeerIdentity, ancestors: Sequence[str], relative: str) -> tuple[str, int]:  # fmt: skip
    """ingest the file or directory at `relative` in the minting peer's tree
    and return its ref and size, shared with the minter and its ancestors.
    Raises `ArtifactDenied` on any refusal; heavy, called off-loop."""
    try:
      source = tree_path(identity.tree, relative)
    except ValueError as e:
      raise ArtifactDenied(str(e)) from None
    if not source.is_file() and not source.is_dir():
      raise ArtifactDenied(f'no file or directory at {relative!r} in the workspace')
    return self.adopt(source, identity.workspace, ancestors, event='mint', origin=relative)

  def adopt(
    self,
    source: Path,
    workspace: str,
    ancestors: Sequence[str],
    *,
    event: str,
    origin: str,
  ) -> tuple[str, int]:
    """ingest the host path `source` and return its ref and size, shared with
    the peer named `workspace` and its `ancestors`; `event` and `origin` are
    what the audit records it as. Raises `ArtifactDenied` on any refusal;
    heavy, called off-loop.

    The source is digested before anything is copied — the dedup probe that
    makes re-ingesting unchanged content free of both the copy and the cap, and
    the point a malformed tree (an escaping symlink, an odd entry type) is
    refused before any bytes move. A fresh ingest stages a copy and commits it
    under the copy's own digest, so stored bytes always match their ref even
    when the producer wrote the source mid-ingest."""
    try:
      ref = digest_path(source)
    except ValueError as e:
      raise ArtifactDenied(str(e)) from None
    size = _content_size(source)
    if not (self._objects / ref).exists():
      with self._lock:
        over = self._bytes + size > MAX_STORE_BYTES
      if over:
        raise ArtifactDenied(f'ingesting {origin!r} would exceed the store byte cap')
      ref, size = self._ingest(source)
    shared_with = {workspace, *ancestors}
    with self._lock:
      self._reach.setdefault(ref, set()).update(shared_with)
    for name in shared_with:
      self._link_into_view(name, ref)
    self.audit(
      event,
      {
        'peer': workspace,
        'path': origin,
        'ref': ref,
        'size': size,
        'shared_with': sorted(shared_with),
      },
    )
    return ref, size

  def _ingest(self, source: Path) -> tuple[str, int]:
    with _staged(self._staging) as staging:
      try:
        size = _private_copy(source, staging)
        ref = digest_path(staging)
      except ValueError as e:
        raise ArtifactDenied(str(e)) from None
      with self._lock:
        if self._bytes + size > MAX_STORE_BYTES:
          raise ArtifactDenied(f'minting {source.name!r} would exceed the store byte cap')
        self._bytes += size  # reserved; released again when the commit dedupes
      if not self._commit(staging, ref):
        with self._lock:
          self._bytes -= size
      return ref, size

  def _commit(self, staging: Path, ref: str) -> bool:
    """move the staged copy to its object path; False when identical content
    was already committed (the digest names the bytes, so the loser of a
    commit race discards its copy)."""
    target = self._objects / ref
    if staging.is_dir():
      try:
        staging.rename(target)
      except OSError:
        if not target.exists():
          raise
        return False
      return True
    try:
      os.link(staging, target)
    except FileExistsError:
      return False
    return True

  def _link_into_view(self, peer_workspace: str, ref: str) -> None:
    """hardlink `ref` into the peer's view when the peer has one — container
    peers only. A view entry shares inodes with the store object, which
    nothing writes after commit."""
    view = self._views / peer_workspace
    if not view.is_dir():
      return  # no view: the host-mode root, or a manually launched child
    entry = view / ref
    if entry.exists():
      return
    source = self._objects / ref
    if not source.is_dir():
      try:
        os.link(source, entry)
      except FileExistsError:
        pass
      return
    with _staged(self._staging) as staging:
      shutil.copytree(source, staging, symlinks=True, copy_function=os.link)
      try:
        staging.rename(entry)
      except OSError:
        if not entry.exists():
          raise

  def reachable(self, ref: str, workspace: str) -> bool:
    """whether the peer named `workspace` may read `ref` — one uniform check,
    identical whether or not the ref exists."""
    with self._lock:
      return workspace in self._reach.get(ref, ())

  def resolve(self, ref: str, requester: str) -> Path:
    """the host path holding `ref`'s content, for the peer named `requester`;
    raises `ArtifactDenied` when it may not read it."""
    if not self.reachable(ref, requester):
      raise ArtifactDenied(_denial(ref))
    return self._objects / ref

  def share(self, refs: Sequence[str], *, to: str, by: str) -> None:
    """extend each ref's reach to the peer named `to` and link it into that
    peer's view. The caller checked `by`'s own reach on the loop; the linking
    is called off-loop, during spawn lowering."""
    if len(refs) == 0:
      return
    with self._lock:
      for ref in refs:
        self._reach.setdefault(ref, set()).add(to)
    self.view(to)
    for ref in refs:
      self._link_into_view(to, ref)
    self.audit('share', {'by': by, 'to': to, 'refs': list(refs)})

  def materialize(self, identity: PeerIdentity, ref: str) -> str:
    """the path `ref` appears at for the requesting peer — its view mount for
    a container peer, a private copy under the workspace directory for the
    host-mode root. Raises `ArtifactDenied` on any refusal; the copy is heavy,
    called off-loop."""
    if not self.reachable(ref, identity.workspace):
      raise ArtifactDenied(_denial(ref))
    if identity.manual:
      raise ArtifactDenied('no artifact view is mounted for a manually launched session')
    if identity.workspace == self.session and not self._root_in_container:
      path = str(self._host_copy(ref))
    else:
      self._link_into_view(identity.workspace, ref)
      path = str(CONTAINER_ARTIFACTS_ROOT / ref)
    self.audit('get', {'peer': identity.workspace, 'ref': ref})
    return path

  def _host_copy(self, ref: str) -> Path:
    destination_directory = workspace_dir(self.session) / 'artifacts'
    destination = destination_directory / ref
    if destination.exists():
      return destination
    destination_directory.mkdir(exist_ok=True)
    with _staged(destination_directory) as staging:
      _private_copy(self._objects / ref, staging)
      try:
        staging.rename(destination)
      except OSError:
        if not destination.exists():
          raise
    return destination

  def audit(self, event: str, entry: dict[str, Any]) -> None:
    """append one entry to the session's artifact audit."""
    entry = {
      'time': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
      'event': event,
      'session': self.session,
      **entry,
    }
    try:
      self._audit_file.parent.mkdir(parents=True, exist_ok=True)
      with self._audit_lock, self._audit_file.open('a') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except OSError as e:
      log.warning('could not append artifact audit record to %s: %s', self._audit_file, e)


def _validate_mint(args: dict[str, Any]) -> Optional[str]:
  unknown = sorted(set(args) - _MINT_KEYS)
  if len(unknown) > 0:
    return f'unknown artifact.mint field(s): {", ".join(unknown)}'
  path = args.get('path')
  if not isinstance(path, str) or len(path) == 0:
    return "artifact.mint needs a non-empty string 'path'"
  return None


def _validate_get(args: dict[str, Any]) -> Optional[str]:
  unknown = sorted(set(args) - _GET_KEYS)
  if len(unknown) > 0:
    return f'unknown artifact.get field(s): {", ".join(unknown)}'
  if not is_ref(args.get('ref')):
    return "artifact.get needs a well-formed 'ref' (sha256:<64 hex digits>)"
  return None


class JobArtifacts:
  """`bro.broker.dispatcher.JobOutput` over one store: a broker job's run is
  collected in the store and answered with its ref, reaching the requesting
  peer and its summoners exactly as that peer's own mint would."""

  def __init__(self, store: ArtifactStore, peers: Peers):
    self._store = store
    self._peers = peers

  def open(self) -> Path:
    return self._store.job_run()

  async def collect(self, directory: Path, context: 'Dispatcher', requester: 'Peer') -> dict:
    identity = self._peers.identity(context, requester)
    ancestors = self._peers.ancestors(context, requester)
    ref, size = await asyncio.to_thread(
      self._store.adopt,
      directory,
      identity.workspace,
      ancestors,
      event='job',
      origin=str(directory),
    )
    return {'ref': ref, 'size': size}


class ArtifactControl:
  """the artifact kinds and the contributed-kind resolver over one store (see
  the module docstring). `mint` and `get` register as the broker's
  `artifact.mint` / `artifact.get` handlers; everything here runs on the
  broker loop, with store content work threaded."""

  def __init__(self, store: ArtifactStore, peers: Peers):
    self._store = store
    self._peers = peers

  def mint(self, context: 'Dispatcher', peer: 'Peer', message: 'Message') -> None:
    args = message.args
    try:
      identity = self._peers.identity(context, peer)
      ancestors = self._peers.ancestors(context, peer)
    except UnattributablePeer as reason:
      self._deny(context, peer, message, None, f'artifact mint denied: {reason}')
      return
    error = _validate_mint(args)
    if error is not None:
      self._deny(context, peer, message, identity, error)
      return
    path = args['path']
    self._answer_off_loop(context, peer, message.exchange, lambda: self._minted(identity, ancestors, path))  # fmt: skip

  def _minted(self, identity: PeerIdentity, ancestors: Sequence[str], path: str) -> dict[str, Any]:
    ref, size = self._store.mint(identity, ancestors, path)
    return {'ref': ref, 'size': size}

  def get(self, context: 'Dispatcher', peer: 'Peer', message: 'Message') -> None:
    args = message.args
    try:
      identity = self._peers.identity(context, peer)
    except UnattributablePeer as reason:
      self._deny(context, peer, message, None, f'artifact get denied: {reason}')
      return
    error = _validate_get(args)
    if error is not None:
      self._deny(context, peer, message, identity, error)
      return
    ref = args['ref']
    self._answer_off_loop(
      context, peer, message.exchange, lambda: {'path': self._store.materialize(identity, ref)}
    )

  def resolve(self, ref: str, context: 'Dispatcher', requester: 'Peer') -> Path:
    """`bro.kinds.ArtifactResolver`: the host path of `ref` for a kind
    handler's requesting peer, with the denial as uniform as the wire one."""
    try:
      identity = self._peers.identity(context, requester)
    except UnattributablePeer:
      raise ArtifactDenied(_denial(ref)) from None
    return self._store.resolve(ref, identity.workspace)

  def _answer_off_loop(
    self, context: 'Dispatcher', peer: 'Peer', exchange: str, work: Callable[[], dict[str, Any]]
  ) -> None:
    from bro.broker import brotocol

    task = asyncio.ensure_future(asyncio.to_thread(work))

    def _answered(finished: asyncio.Task) -> None:
      if finished.cancelled():
        return
      error = finished.exception()
      if error is None:
        result = brotocol.result(exchange, 'ok', value=finished.result())
      elif isinstance(error, ArtifactDenied):
        log.warning('artifact: %s', error)
        self._store.audit('deny', {'request_id': exchange, 'reason': str(error)})
        result = brotocol.result(exchange, 'denied', error=str(error))
      else:
        log.warning('artifact request %s failed: %r', exchange, error)
        result = brotocol.result(exchange, 'failed', error=str(error), detail={'reason': 'error'})
      context.deliver(peer, result)

    task.add_done_callback(_answered)

  def _deny(
    self,
    context: 'Dispatcher',
    peer: 'Peer',
    message: 'Message',
    identity: Optional[PeerIdentity],
    error: str,
  ) -> None:
    log.warning('artifact: %s', error)
    context.reply(peer, {'outcome': 'denied', 'error': error})
    entry: dict[str, Any] = {'request_id': message.id, 'reason': error}
    if identity is not None:
      entry['peer'] = identity.workspace
    self._store.audit('deny', entry)
