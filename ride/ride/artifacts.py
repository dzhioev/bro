"""Artifact sharing on the launcher: the ride store and its request kinds.

`ArtifactStore` is the ride-scoped content-addressed store under
`<runtime-root>/artifacts/<ride>/`. Ingest stages a private copy of the
peer-named tree path — a reflink where the filesystem clones, a plain copy
otherwise, never a hardlink, so nothing a producer writes afterwards reaches
stored bytes — normalizes modes to the manifest's vocabulary (0o755/0o644 per
the recorded executable bit), digests the copy (`bro.artifact.digest_path`),
and commits it to `objects/<digest>`, deduplicating by digest and refusing a
mint past the byte cap rather than evicting. Committed content is immutable by
construction, so the read path re-verifies nothing.

Reach is metadata: each reader — a peer workspace, or the operator — holds
the refs it may read in the order they reached it, and access is checked by
digest. A mint reaches the minter and its owners up to the root, a share the
worker it names; neither materializes anything. Content reaches a peer only
through its own fetch, which materializes the ref at its relative path
`<digest>/<name>` by the peer's placement: a boxed peer's view directory
`shared/<workspace>/`, the source of its declared read-only artifact-view bind
mount, gets one hardlink (or hardlinked tree) per fetched ref, appearing
without a remount; an unboxed peer, having no mount namespace, gets a private
copy under its workspace's own `artifacts/` directory. A checked fetch digests
the materialization and replaces one that no longer matches, failing when the
stored object itself does not. A manually launched child has no host-built
launch and therefore no view; its fetch is denied with the reason.

The operator reads only what a session exported or the operator itself shared in
(`share_from_host`); an exported ref reaches no other peer.

The store is wiped at construction and removed at `close()` — it dies with
the ride — while the JSONL audit beside it (`<ride>.jsonl`) survives,
recording mints, fetches, shares, and denials.

`JobArtifacts` collects a broker job's run directory the same way: the run is
staged under the store's `jobs/`, and the ref that closes the job's mission
names it `JOB_RUN_NAME` and reaches the peer that requested the job and its owners.

`ArtifactControl` serves the `artifact.mint`, `artifact.fetch`, `artifact.locate`,
`artifact.list`, `artifact.share`, and `artifact.export` kinds (contract:
`bro/artifact.py`), the operator's `operator.artifact.share`,
`operator.artifact.fetch`, and `operator.artifact.list`, and implements the
worker-type resolver (`bro.worker_types.ArtifactResolver`).
Attribution, authorization, and shape validation run on the broker loop;
store I/O runs in a thread with the correlated result delivered
from a done-callback. The request never enters the dispatcher's table, so
exactly-one-result is this module's duty: the callback folds a store refusal
into `result{denied}` and any other exception into `result{failed}` instead
of letting it vanish. A sharing denial is uniform — identical whether or not
the ref exists. Broker imports stay function-local: `ride/ride/root.py`
composes the root view mount from the path helpers here before the broker
gate."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import shutil
import threading
import time
from collections.abc import Callable, Generator, Sequence
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal, Optional

from bro.artifact import (
  REF_FORM,
  compose_ref,
  digest_path,
  is_ref,
  name_error,
  path_name,
  split_ref,
)
from bro.base import log
from bro.base.lulid import lulid
from bro.worker_types import ArtifactDenied, PeerDescription, UnattributablePeer, tree_path
from bro.workspace.paths import artifacts_dir, workspace_dir
from ride.peer_facts import PeerFacts
from ride.workspace.model import Workspace

if TYPE_CHECKING:
  from bro.broker.brotocol import Message
  from bro.broker.dispatcher import Dispatcher
  from bro.broker.environment import Talk
  from bro.broker.runtime import Peer

# refusal bound on the store's committed bytes — a runaway-ingest guard, not a
# quota (the store dies with the ride)
MAX_STORE_BYTES = 32 << 30
# the Linux ioctl that clones a file's extents; any refusal (another OS, a
# filesystem that cannot clone, a cross-device pair) falls back to a plain copy
_FICLONE = 0x40049409

_MINT_KEYS = frozenset({'path', 'name'})
_FETCH_KEYS = frozenset({'ref', 'checked'})
_LOCATE_KEYS = frozenset({'ref'})
_SHARE_KEYS = frozenset({'id', 'ref'})
_EXPORT_KEYS = frozenset({'ref'})
# an owner's live share is announced to the worker where the owner may start a
# message, and refused where no talk move could carry the ref to it at all
_OWNER_STARTS: Talk = frozenset({'owner.say', 'owner.question'})
_REF_CARRIERS: Talk = frozenset({*_OWNER_STARTS, 'worker.question'})
JOB_RUN_NAME = 'run'  # the name a collected job run's ref carries


def store_dir(ride: str) -> Path:
  """The ride's artifact store directory."""
  return artifacts_dir() / ride


def audit_file(ride: str) -> Path:
  """The ride's artifact audit, beside the store it outlives."""
  return artifacts_dir() / f'{ride}.jsonl'


def view_dir(ride: str, peer_workspace: str) -> Path:
  """a peer's view directory inside the ride store."""
  return store_dir(ride) / 'shared' / peer_workspace


def view_mount(ride: str, peer_workspace: str, container_path: PurePosixPath) -> str:
  """a peer's read-only view bind mount, as a docker mount spec."""
  return f'{view_dir(ride, peer_workspace)}:{container_path}:ro'


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


def _publish_without_replacing(staging: Path, destination: Path) -> None:
  """move `staging` to `destination`, refusing if anything exists there at the
  moment of publication: a file is hard-linked into place and a directory
  claims its name with an exclusive `mkdir` before the rename."""
  try:
    if staging.is_dir():
      destination.mkdir()
      try:
        staging.rename(destination)
      except OSError:
        destination.rmdir()
        raise
    else:
      os.link(staging, destination)
  except FileExistsError:
    raise ArtifactDenied(f'{destination} already exists') from None
  except OSError:
    raise ArtifactDenied(f'cannot place {destination}') from None


@contextlib.contextmanager
def _staged(directory: Path) -> Generator[Path]:
  """a fresh staging path under `directory`, removed — file, symlink, or tree,
  never following a symlink — when the block ends; a stage the block renamed
  away is left alone."""
  staging = directory / f'.{lulid()}'
  try:
    yield staging
  finally:
    if staging.is_dir() and not staging.is_symlink():
      shutil.rmtree(staging)
    elif os.path.lexists(staging):
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


class _Reach:
  """the refs one reader may read, in the order they reached it, and the
  digests they name — the access check, since reach is to the content."""

  def __init__(self) -> None:
    self.refs: dict[str, None] = {}
    self.digests: set[str] = set()

  def add(self, ref: str) -> None:
    self.refs.setdefault(ref, None)
    self.digests.add(split_ref(ref)[0])


def _link_tree(source: Path, destination: Path) -> None:
  """hardlink the file at `source` to `destination`, or rebuild the directory
  at `source` there with every file hardlinked."""
  if source.is_dir():
    shutil.copytree(source, destination, symlinks=True, copy_function=os.link)
  else:
    os.link(source, destination)


def _matches(entry: Path, digest: str) -> bool:
  if entry.is_symlink():
    return False
  try:
    return digest_path(entry) == digest
  except ValueError:
    return False


class ArtifactStore:
  """One ride's content-addressed store (see the module docstring). The
  metadata lock guards the reach map and the byte account; content operations
  run outside it, so a mint hashing for seconds never blocks a loop-side
  check. Each materialization has a lock of its own, serializing the fetches
  that place, check, or replace it."""

  def __init__(self, workspace: Workspace, *, root_boxed: bool):
    self.ride = workspace.name
    self._lock = threading.Lock()
    self._audit_lock = threading.Lock()
    self._reach: dict[str, _Reach] = {}  # workspace name -> the refs it may read
    self._host_reach = _Reach()  # the refs the operator may read
    self._materializing: dict[Path, threading.Lock] = {}
    self._bytes = 0
    root = store_dir(workspace.name)
    if root.exists():
      shutil.rmtree(root)  # a leftover from a crashed session; the store is ride-scoped
    self._objects = root / 'objects'
    self._staging = root / 'staging'
    self._views = root / 'shared'
    self._jobs = root / 'jobs'
    for directory in (self._objects, self._staging, self._views, self._jobs):
      directory.mkdir(parents=True)
    self._audit_file = audit_file(workspace.name)
    if root_boxed:
      self.view(workspace.name)

  def close(self) -> None:
    """Remove the store when the ride ends; keep its audit."""
    try:
      shutil.rmtree(store_dir(self.ride))
    except OSError as e:
      log.warning('could not remove the artifact store %s: %s', store_dir(self.ride), e)

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

  def mint(
    self,
    identity: PeerDescription,
    ancestors: Sequence[str],
    relative: str,
    name: Optional[str] = None,
  ) -> tuple[str, int]:
    """ingest the file or directory at `relative` in the minting peer's tree
    as `name` (by default the path's last component) and return its ref and
    size, shared with the minter and its ancestors. Raises `ArtifactDenied` on
    any refusal; heavy, called off-loop."""
    try:
      source = tree_path(identity.tree, relative)
      name = path_name(relative) if name is None else name
    except ValueError as e:
      raise ArtifactDenied(str(e)) from None
    if not source.is_file() and not source.is_dir():
      raise ArtifactDenied(f'no file or directory at {relative!r} in the workspace')
    return self.adopt(source, name, identity.workspace, ancestors, event='mint', origin=relative)

  def adopt(
    self,
    source: Path,
    name: str,
    workspace: str,
    ancestors: Sequence[str],
    *,
    event: str,
    origin: str,
  ) -> tuple[str, int]:
    """ingest the host path `source` as `name` and return its ref and size,
    shared with the peer named `workspace` and its `ancestors`; `event` and
    `origin` are what the audit records it as. Raises `ArtifactDenied` on any
    refusal; heavy, called off-loop.

    The source is digested before anything is copied — the dedup probe that
    makes re-ingesting unchanged content free of both the copy and the cap, and
    the point a malformed tree (an escaping symlink, an odd entry type) is
    refused before any bytes move. A fresh ingest stages a copy and commits it
    under the copy's own digest, so stored bytes always match their ref even
    when the producer wrote the source mid-ingest."""
    try:
      digest = digest_path(source)
    except ValueError as e:
      raise ArtifactDenied(str(e)) from None
    size = _content_size(source)
    if not (self._objects / digest).exists():
      with self._lock:
        over = self._bytes + size > MAX_STORE_BYTES
      if over:
        raise ArtifactDenied(f'ingesting {origin!r} would exceed the store byte cap')
      digest, size = self._ingest(source)
    ref = compose_ref(digest, name)
    shared_with = {workspace, *ancestors}
    with self._lock:
      for reader in shared_with:
        self._reader(reader).add(ref)
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
        digest = digest_path(staging)
      except ValueError as e:
        raise ArtifactDenied(str(e)) from None
      with self._lock:
        if self._bytes + size > MAX_STORE_BYTES:
          raise ArtifactDenied(f'minting {source.name!r} would exceed the store byte cap')
        self._bytes += size  # reserved; released again when the commit dedupes
      if not self._commit(staging, digest):
        with self._lock:
          self._bytes -= size
      return digest, size

  def _commit(self, staging: Path, digest: str) -> bool:
    """move the staged copy to its object path; False when identical content
    was already committed (the digest names the bytes, so the loser of a
    commit race discards its copy)."""
    target = self._objects / digest
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

  def _reader(self, workspace: str) -> _Reach:
    """the reach of the peer named `workspace`, created on first use; the
    caller holds the metadata lock."""
    reach = self._reach.get(workspace)
    if reach is None:
      reach = self._reach[workspace] = _Reach()
    return reach

  def reachable(self, ref: str, workspace: str) -> bool:
    """whether the peer named `workspace` may read `ref`'s content — one
    uniform check, identical whether or not the ref exists."""
    digest, _ = split_ref(ref)
    with self._lock:
      reach = self._reach.get(workspace)
      return reach is not None and digest in reach.digests

  def readable(self, workspace: str) -> list[str]:
    """the refs the peer named `workspace` may read, in the order they
    reached it."""
    with self._lock:
      reach = self._reach.get(workspace)
      return [] if reach is None else list(reach.refs)

  def resolve(self, ref: str, requester: str) -> Path:
    """the host path holding `ref`'s content, for the peer named `requester`;
    raises `ArtifactDenied` when it may not read it."""
    if not self.reachable(ref, requester):
      raise ArtifactDenied(_denial(ref))
    digest, _ = split_ref(ref)
    return self._objects / digest

  def share(self, refs: Sequence[str], *, to: str, by: str) -> None:
    """extend each ref's reach to the peer named `to`. The caller checked
    `by`'s own reach on the loop."""
    if len(refs) == 0:
      return
    with self._lock:
      reach = self._reader(to)
      for ref in refs:
        reach.add(ref)
    self.audit('share', {'by': by, 'to': to, 'refs': list(refs)})

  def _materialization(self, identity: PeerDescription, ref: str) -> tuple[Path, str]:
    """where `ref` materializes for the requesting peer: the host path of the
    entry, and the path the peer reads it at. Raises `ArtifactDenied` for a
    peer that may not read it or has no materialization."""
    if not self.reachable(ref, identity.workspace):
      raise ArtifactDenied(_denial(ref))
    if identity.expected:
      raise ArtifactDenied('no artifact view is mounted for a manually launched worker')
    if identity.artifact_view is None:
      entry = workspace_dir(identity.workspace) / 'artifacts' / ref
      return entry, str(entry)
    return self._views / identity.workspace / ref, str(identity.artifact_view / ref)

  def fetch(self, identity: PeerDescription, ref: str, *, checked: bool = False) -> str:
    """materialize `ref` for the requesting peer and return the path it reads
    it at; an existing materialization is kept, and `checked` digests it first
    and replaces one that no longer matches. Raises `ArtifactDenied` on any
    refusal; the materialization is heavy, called off-loop."""
    entry, path = self._materialization(identity, ref)
    digest, _ = split_ref(ref)
    source = self._objects / digest
    if identity.artifact_view is None:
      staging_directory = entry.parent  # beside the copy, so publishing it is a rename

      def build(staging: Path) -> None:
        _private_copy(source, staging)
    else:
      if not (self._views / identity.workspace).is_dir():
        raise RuntimeError(f'peer {identity.workspace} has no artifact view directory')
      staging_directory = self._staging

      def build(staging: Path) -> None:
        _link_tree(source, staging)

    with self._materialization_lock(entry):
      entry.parent.mkdir(parents=True, exist_ok=True)
      if not os.path.lexists(entry):
        with _staged(staging_directory) as staging:
          build(staging)
          staging.rename(entry)
      if checked and not _matches(entry, digest):
        with _staged(staging_directory) as staging, _staged(staging_directory) as discarded:
          build(staging)
          entry.rename(discarded)
          staging.rename(entry)
        if not _matches(entry, digest):
          raise RuntimeError(f'the stored content of {ref} no longer matches its digest')
    with self._lock:
      self._reader(identity.workspace).add(ref)
    self.audit('fetch', {'peer': identity.workspace, 'ref': ref, 'checked': checked})
    return path

  def _materialization_lock(self, entry: Path) -> threading.Lock:
    with self._lock:
      return self._materializing.setdefault(entry, threading.Lock())

  def locate(self, identity: PeerDescription, ref: str) -> str:
    """the path the requesting peer reads `ref` at, which it already fetched;
    materializes nothing. Raises `ArtifactDenied` on any refusal, including a
    ref not fetched yet."""
    entry, path = self._materialization(identity, ref)
    if not os.path.lexists(entry):
      raise ArtifactDenied(f'artifact {ref} is not fetched yet; fetch it first')
    return path

  def export(self, ref: str, *, by: str) -> None:
    """extend `ref`'s reach to the operator. The caller checked `by`'s own reach
    and export right on the loop."""
    with self._lock:
      self._host_reach.add(ref)
    self.audit('export', {'by': by, 'ref': ref})

  def host_readable(self) -> list[str]:
    """the refs the operator may read, in the order they reached it."""
    with self._lock:
      return list(self._host_reach.refs)

  def _host_reads(self, ref: str) -> bool:
    digest, _ = split_ref(ref)
    with self._lock:
      return digest in self._host_reach.digests

  def share_from_host(self, source: Path, name: str, *, to: str) -> tuple[str, int]:
    """ingest the host path `source` as `name` and share it with the peer named
    `to`; the operator may read it afterwards. Heavy, called off-loop."""
    ref, size = self.adopt(source, name, to, (), event='operator-share', origin=str(source))
    with self._lock:
      self._host_reach.add(ref)
    return ref, size

  def share_to_peer(self, ref: str, *, to: str) -> None:
    """share a ref the operator may read with the peer named `to`."""
    if not self._host_reads(ref):
      raise ArtifactDenied(f'artifact {ref} is not available to the operator')
    self.share((ref,), to=to, by='operator')

  def copy_to_host(self, ref: str, directory: Path) -> Path:
    """copy an exported ref's content to `directory/<name>` and return that
    path; refuses to replace an existing path. Heavy, called off-loop."""
    if not self._host_reads(ref):
      raise ArtifactDenied(f'artifact {ref} is not available to the operator')
    digest, name = split_ref(ref)
    destination = directory / name
    directory.mkdir(parents=True, exist_ok=True)
    with _staged(directory) as staging:
      _private_copy(self._objects / digest, staging)
      _publish_without_replacing(staging, destination)
    self.audit('host-fetch', {'ref': ref, 'path': str(destination)})
    return destination

  def audit(self, event: str, entry: dict[str, Any]) -> None:
    """append one entry to the ride's artifact audit."""
    entry = {
      'time': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
      'event': event,
      'ride': self.ride,
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
  if 'name' in args:
    name = args['name']
    if not isinstance(name, str):
      return "artifact.mint 'name' must be a string"
    return name_error(name)
  return None


def _validate_fetch(args: dict[str, Any]) -> Optional[str]:
  unknown = sorted(set(args) - _FETCH_KEYS)
  if len(unknown) > 0:
    return f'unknown artifact.fetch field(s): {", ".join(unknown)}'
  if not is_ref(args.get('ref')):
    return f"artifact.fetch needs a well-formed 'ref' ({REF_FORM})"
  if not isinstance(args.get('checked', False), bool):
    return "artifact.fetch 'checked' must be a boolean"
  return None


def _validate_locate(args: dict[str, Any]) -> Optional[str]:
  unknown = sorted(set(args) - _LOCATE_KEYS)
  if len(unknown) > 0:
    return f'unknown artifact.locate field(s): {", ".join(unknown)}'
  if not is_ref(args.get('ref')):
    return f"artifact.locate needs a well-formed 'ref' ({REF_FORM})"
  return None


def _validate_share(args: dict[str, Any]) -> Optional[str]:
  unknown = sorted(set(args) - _SHARE_KEYS)
  if len(unknown) > 0:
    return f'unknown artifact.share field(s): {", ".join(unknown)}'
  mission_id = args.get('id')
  if not isinstance(mission_id, str) or len(mission_id) == 0:
    return "artifact.share needs a non-empty string 'id'"
  if not is_ref(args.get('ref')):
    return f"artifact.share needs a well-formed 'ref' ({REF_FORM})"
  return None


def _validate_export(args: dict[str, Any]) -> Optional[str]:
  unknown = sorted(set(args) - _EXPORT_KEYS)
  if len(unknown) > 0:
    return f'unknown artifact.export field(s): {", ".join(unknown)}'
  if not is_ref(args.get('ref')):
    return f"artifact.export needs a well-formed 'ref' ({REF_FORM})"
  return None


def _validate_host_share(args: dict[str, Any]) -> Optional[str]:
  unknown = sorted(set(args) - {'id', 'ref', 'path', 'name'})
  if len(unknown) > 0:
    return f'unknown operator.artifact.share field(s): {", ".join(unknown)}'
  if not isinstance(args.get('id'), str) or len(args['id']) == 0:
    return "operator.artifact.share needs a non-empty string 'id'"
  if ('ref' in args) == ('path' in args):
    return "operator.artifact.share needs exactly one of 'ref' and 'path'"
  if 'ref' in args:
    if 'name' in args:
      return "operator.artifact.share takes 'name' only with 'path'"
    if not is_ref(args['ref']):
      return f"operator.artifact.share needs a well-formed 'ref' ({REF_FORM})"
    return None
  path = args['path']
  if not isinstance(path, str) or not os.path.isabs(path):
    return "operator.artifact.share needs an absolute string 'path'"
  if 'name' in args:
    name = args['name']
    if not isinstance(name, str):
      return "operator.artifact.share 'name' must be a string"
    return name_error(name)
  return None


def _validate_host_fetch(args: dict[str, Any]) -> Optional[str]:
  unknown = sorted(set(args) - {'ref', 'to'})
  if len(unknown) > 0:
    return f'unknown operator.artifact.fetch field(s): {", ".join(unknown)}'
  if not is_ref(args.get('ref')):
    return f"operator.artifact.fetch needs a well-formed 'ref' ({REF_FORM})"
  if not isinstance(args.get('to'), str) or not os.path.isabs(args['to']):
    return "operator.artifact.fetch needs an absolute string 'to' directory"
  return None


class JobArtifacts:
  """`bro.broker.dispatcher.JobOutput` over one store: a broker job's run is
  collected in the store and answered with its ref, reaching the requesting
  peer and its owners exactly as that peer's own mint would."""

  def __init__(self, store: ArtifactStore, facts: PeerFacts):
    self._store = store
    self._facts = facts

  def open(self) -> Path:
    return self._store.job_run()

  async def collect(self, directory: Path, context: Dispatcher, requester: Peer) -> dict:
    identity = self._facts.resolve(context, requester)
    ancestors = self._facts.ancestors(context, requester)
    ref, size = await asyncio.to_thread(
      self._store.adopt,
      directory,
      JOB_RUN_NAME,
      identity.workspace,
      ancestors,
      event='job',
      origin=str(directory),
    )
    return {'ref': ref, 'size': size}


class ArtifactControl:
  """The artifact kinds and worker-type resolver over one store.

  `mint`, `fetch`, `locate`, `listing`, `share`, and `export` register as the broker's artifact request handlers,
  and `host_share`, `host_fetch`, and `host_listing` as the operator channel's.
  Everything here runs on the broker loop, with store content work threaded.
  """

  def __init__(self, store: ArtifactStore, facts: PeerFacts):
    self._store = store
    self._facts = facts

  def mint(self, context: Dispatcher, peer: Peer, message: Message) -> None:
    args = message.args
    try:
      identity = self._facts.resolve(context, peer)
      ancestors = self._facts.ancestors(context, peer)
    except UnattributablePeer as reason:
      self._deny(context, peer, message, None, f'artifact mint denied: {reason}')
      return
    error = _validate_mint(args)
    if error is not None:
      self._deny(context, peer, message, identity, error)
      return
    path = args['path']
    name = args.get('name')
    self._answer_off_loop(context, peer, message.request_id, lambda: self._minted(identity, ancestors, path, name))  # fmt: skip

  def _minted(
    self, identity: PeerDescription, ancestors: Sequence[str], path: str, name: Optional[str]
  ) -> dict[str, Any]:
    ref, size = self._store.mint(identity, ancestors, path, name)
    return {'ref': ref, 'size': size}

  def fetch(self, context: Dispatcher, peer: Peer, message: Message) -> None:
    args = message.args
    try:
      identity = self._facts.resolve(context, peer)
    except UnattributablePeer as reason:
      self._deny(context, peer, message, None, f'artifact fetch denied: {reason}')
      return
    error = _validate_fetch(args)
    if error is not None:
      self._deny(context, peer, message, identity, error)
      return
    ref, checked = args['ref'], args.get('checked', False)
    self._answer_off_loop(
      context,
      peer,
      message.request_id,
      lambda: {'path': self._store.fetch(identity, ref, checked=checked)},
    )

  def locate(self, context: Dispatcher, peer: Peer, message: Message) -> None:
    args = message.args
    try:
      identity = self._facts.resolve(context, peer)
    except UnattributablePeer as reason:
      self._deny(context, peer, message, None, f'artifact locate denied: {reason}')
      return
    error = _validate_locate(args)
    if error is not None:
      self._deny(context, peer, message, identity, error)
      return
    ref = args['ref']
    self._answer_off_loop(
      context, peer, message.request_id, lambda: {'path': self._store.locate(identity, ref)}
    )

  def listing(self, context: Dispatcher, peer: Peer, message: Message) -> None:
    try:
      identity = self._facts.resolve(context, peer)
    except UnattributablePeer as reason:
      self._deny(context, peer, message, None, f'artifact list denied: {reason}')
      return
    self._page(context, peer, message, identity, self._store.readable(identity.workspace))

  def host_listing(self, context: Dispatcher, peer: Peer, message: Message) -> None:
    self._page(context, peer, message, None, self._store.host_readable())

  def _page(
    self,
    context: Dispatcher,
    peer: Peer,
    message: Message,
    identity: Optional[PeerDescription],
    refs: Sequence[str],
  ) -> None:
    from bro.broker import brotocol

    try:
      cursor = brotocol.index_cursor(message.args, len(refs))
      value = brotocol.frame_page(
        message.request_id, 'refs', refs[cursor:], lambda count: cursor + count
      )
    except ValueError as error:
      self._deny(context, peer, message, identity, f'artifact list denied: {error}')
      return
    context.reply(peer, {'outcome': 'ok', 'value': value})

  def share(self, context: Dispatcher, peer: Peer, message: Message) -> None:
    args = message.args
    try:
      owner = self._facts.resolve(context, peer)
    except UnattributablePeer as reason:
      self._deny(context, peer, message, None, f'artifact share denied: {reason}')
      return
    error = _validate_share(args)
    if error is not None:
      self._deny(context, peer, message, owner, error)
      return
    mission_id = args['id']
    record = context.journal.records.get(mission_id)
    if record is None or record.terminal or record.owner != peer:
      self._deny(
        context,
        peer,
        message,
        owner,
        f'no live mission {mission_id!r} owned by this peer',
      )
      return
    if record.talk.isdisjoint(_REF_CARRIERS):
      self._deny(
        context,
        peer,
        message,
        owner,
        f'artifact share denied: the talk of mission {mission_id!r} carries no ref to its '
        f'worker; it needs one of {", ".join(sorted(_REF_CARRIERS))}',
      )
      return
    ref = args['ref']
    try:
      self._store.resolve(ref, owner.workspace)
    except ArtifactDenied:
      self._deny(
        context,
        peer,
        message,
        owner,
        f'cannot share artifact the owner cannot reach: {ref}',
      )
      return
    try:
      worker = self._worker_workspace(mission_id)
    except ArtifactDenied as reason:
      self._deny(context, peer, message, owner, str(reason))
      return
    announced = not record.talk.isdisjoint(_OWNER_STARTS)
    self._answer_off_loop(
      context,
      peer,
      message.request_id,
      lambda: self._shared(ref, owner=owner.workspace, worker=worker),
      then=(lambda _: self._announce(context, mission_id, ref, 'owner')) if announced else None,
    )

  def _worker_workspace(self, mission_id: str) -> str:
    """the workspace whose view an artifact share into `mission_id` extends;
    raises `ArtifactDenied` when that mission has none."""
    try:
      if self._facts.for_mission(mission_id).expected:
        raise ArtifactDenied(
          'artifact share denied: no artifact view is mounted for a manually launched worker'
        )
      return self._facts.describe(mission_id).workspace
    except UnattributablePeer:
      raise ArtifactDenied(
        f'artifact share denied: mission {mission_id!r} has no available artifact workspace'
      ) from None

  def export(self, context: Dispatcher, peer: Peer, message: Message) -> None:
    try:
      identity = self._facts.resolve(context, peer)
    except UnattributablePeer as reason:
      self._deny(context, peer, message, None, f'artifact export denied: {reason}')
      return
    error = _validate_export(message.args)
    if error is not None:
      self._deny(context, peer, message, identity, error)
      return
    record = identity.mission_record
    if record is None or record.descriptor.values.get('export') is not True:
      self._deny(
        context,
        peer,
        message,
        identity,
        'artifact export denied: this session was not started with bro.export',
      )
      return
    ref = message.args['ref']
    if not self._store.reachable(ref, identity.workspace):
      self._deny(context, peer, message, identity, _denial(ref))
      return
    self._answer_off_loop(
      context, peer, message.request_id, lambda: self._exported(ref, identity.workspace)
    )

  def _exported(self, ref: str, workspace: str) -> dict[str, Any]:
    self._store.export(ref, by=workspace)
    return {}

  def host_share(self, context: Dispatcher, peer: Peer, message: Message) -> None:
    args = message.args
    error = _validate_host_share(args)
    if error is not None:
      self._deny(context, peer, message, None, error)
      return
    mission_id = args['id']
    record = context.journal.records.get(mission_id)
    if record is None or record.terminal:
      self._deny(context, peer, message, None, f'no live mission {mission_id!r}')
      return
    try:
      worker = self._worker_workspace(mission_id)
    except ArtifactDenied as reason:
      self._deny(context, peer, message, None, str(reason))
      return

    def announce(value: dict[str, Any]) -> None:
      self._announce(context, mission_id, value['ref'], 'operator')

    if 'ref' in args:
      ref = args['ref']
      self._answer_off_loop(
        context,
        peer,
        message.request_id,
        lambda: self._host_shared_ref(ref, worker),
        then=announce,
      )
      return
    source = Path(args['path'])
    try:
      name = args['name'] if 'name' in args else path_name(str(source))
    except ValueError as reason:
      self._deny(context, peer, message, None, str(reason))
      return
    self._answer_off_loop(
      context,
      peer,
      message.request_id,
      lambda: self._host_shared_path(source, name, worker),
      then=announce,
    )

  def _host_shared_ref(self, ref: str, worker: str) -> dict[str, Any]:
    self._store.share_to_peer(ref, to=worker)
    return {'ref': ref}

  def _host_shared_path(self, source: Path, name: str, worker: str) -> dict[str, Any]:
    if not source.is_file() and not source.is_dir():
      raise ArtifactDenied(f'no file or directory at {str(source)!r}')
    ref, size = self._store.share_from_host(source, name, to=worker)
    return {'ref': ref, 'size': size}

  def host_fetch(self, context: Dispatcher, peer: Peer, message: Message) -> None:
    args = message.args
    error = _validate_host_fetch(args)
    if error is not None:
      self._deny(context, peer, message, None, error)
      return
    ref, directory = args['ref'], Path(args['to'])
    self._answer_off_loop(
      context,
      peer,
      message.request_id,
      lambda: {'path': str(self._store.copy_to_host(ref, directory))},
    )

  def _shared(self, ref: str, *, owner: str, worker: str) -> dict[str, Any]:
    self._store.share((ref,), to=worker, by=owner)
    return {}

  def _announce(
    self, context: Dispatcher, mission_id: str, ref: str, sharer: Literal['owner', 'operator']
  ) -> None:
    record = context.journal.records.get(mission_id)
    if record is not None and not record.terminal:
      context.journal.share(record, ref, sharer)

  def resolve(self, ref: str, peer: PeerDescription) -> Path:
    return self._store.resolve(ref, peer.workspace)

  def _answer_off_loop(
    self,
    context: Dispatcher,
    peer: Peer,
    request_id: str,
    work: Callable[[], dict[str, Any]],
    *,
    then: Optional[Callable[[dict[str, Any]], None]] = None,
  ) -> None:
    from bro.broker import brotocol

    async def answer() -> dict[str, Any]:
      value = await asyncio.to_thread(work)
      if then is not None:
        then(value)
      return value

    task = asyncio.ensure_future(answer())

    def _answered(finished: asyncio.Task) -> None:
      if finished.cancelled():
        return
      error = finished.exception()
      if error is None:
        result = brotocol.result(request_id, 'ok', value=finished.result())
      elif isinstance(error, ArtifactDenied):
        log.warning('artifact: %s', error)
        self._store.audit('deny', {'request_id': request_id, 'reason': str(error)})
        result = brotocol.result(request_id, 'denied', error=str(error))
      else:
        log.warning('artifact request %s failed: %r', request_id, error)
        result = brotocol.result(request_id, 'failed', error=str(error), detail={'reason': 'error'})
      context.deliver(peer, result)

    task.add_done_callback(_answered)

  def _deny(
    self,
    context: Dispatcher,
    peer: Peer,
    message: Message,
    identity: Optional[PeerDescription],
    error: str,
  ) -> None:
    log.warning('artifact: %s', error)
    context.reply(peer, {'outcome': 'denied', 'error': error})
    entry: dict[str, Any] = {'request_id': message.id, 'reason': error}
    if identity is not None:
      entry['peer'] = identity.workspace
    self._store.audit('deny', entry)
