"""Artifact sharing on the launcher: the ride store and its request kinds.

`ArtifactStore` is the ride-scoped content-addressed store under
`<runtime-root>/artifacts/<ride>/`. Ingest stages a private copy of the
source opened from the tree root one component at a time without following
links, reading only from the opened descriptors — a reflink where the filesystem clones, a plain copy
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

Launch-built workspaces also hold writable `mint/<workspace>/` directories.
An entry mint opens one regular file there without following links and uses
the same descriptor ingest as a tree mint. Joined members use their party's
directory; manually launched parties and worker containers have none.

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
import errno
import fcntl
import json
import os
import shutil
import stat
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
from bro.worker_types import ArtifactDenied, PeerDescription, UnattributablePeer
from bro.workspace.paths import CONTAINER_MINT_DIR, artifacts_dir, workspace_dir
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

_MINT_KEYS = frozenset({'path', 'entry', 'name'})
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


def mint_dir(ride: str, peer_workspace: str) -> Path:
  """a workspace's writable mint directory inside the ride store."""
  return store_dir(ride) / 'mint' / peer_workspace


def mint_mount(ride: str, peer_workspace: str) -> str:
  """a workspace's read-write mint bind mount, as a docker mount spec."""
  return f'{mint_dir(ride, peer_workspace)}:{CONTAINER_MINT_DIR}'


@contextlib.contextmanager
def _directory_below(root: int, components: Sequence[str]) -> Generator[int]:
  descriptor = os.dup(root)
  try:
    for component in components:
      child = os.open(
        component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor
      )
      os.close(descriptor)
      descriptor = child
    yield descriptor
  finally:
    os.close(descriptor)


def _remove_store(root: Path) -> None:
  with _descriptor(root.parent, os.O_RDONLY | os.O_DIRECTORY) as directory:
    pending: list[tuple[tuple[str, ...], bool]] = [((root.name,), False)]
    while pending:
      components, visited = pending.pop()
      with _directory_below(directory, components[:-1]) as parent:
        name = components[-1]
        if not stat.S_ISDIR(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode):
          os.unlink(name, dir_fd=parent)
        elif visited:
          os.rmdir(name, dir_fd=parent)
        else:
          os.chmod(name, 0o700, dir_fd=parent, follow_symlinks=False)
          with _descriptor(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, parent=parent
          ) as source:
            names = os.listdir(source)
          pending.append((components, True))
          pending.extend(((*components, child), False) for child in names)


def _denial(ref: str) -> str:
  return f'artifact {ref} is not shared with this peer'


class _SymbolicLink(ValueError):
  def __init__(self, component: str):
    self.component = component
    super().__init__(f'crosses a symbolic link at {component!r}')


@contextlib.contextmanager
def _descriptor(path: str | Path, flags: int, *, parent: Optional[int] = None) -> Generator[int]:
  descriptor = os.open(path, flags | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
  try:
    yield descriptor
  finally:
    os.close(descriptor)


def _open_component(
  stack: contextlib.ExitStack, parent: int, component: str, *, directory: bool
) -> int:
  flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if directory else 0)
  try:
    return stack.enter_context(_descriptor(component, flags, parent=parent))
  except OSError as error:
    if error.errno in (errno.ELOOP, errno.ENOTDIR):
      if stat.S_ISLNK(os.stat(component, dir_fd=parent, follow_symlinks=False).st_mode):
        raise _SymbolicLink(component) from None
    raise


@contextlib.contextmanager
def _open_source(
  root: Path, components: Sequence[str], *, file_only: bool = False
) -> Generator[int]:
  """open below a host-named root, with no link traversal in `components`."""
  for component in components:
    if component in ('', '.', '..') or '/' in component or '\0' in component:
      raise ValueError(f'invalid path component {component!r}')
  with contextlib.ExitStack() as stack:
    descriptor = stack.enter_context(_descriptor(root, os.O_RDONLY | os.O_DIRECTORY))
    for index, component in enumerate(components):
      descriptor = _open_component(
        stack, descriptor, component, directory=index < len(components) - 1
      )
    mode = os.fstat(descriptor).st_mode
    if not stat.S_ISREG(mode) and (file_only or not stat.S_ISDIR(mode)):
      raise ValueError(
        'source is not a regular file' if file_only else 'source is not a file or directory'
      )
    yield descriptor


def _clone_or_copy_file(source: int, destination: Path) -> int:
  with os.fdopen(os.dup(source), 'rb') as source_file, destination.open('wb') as destination_file:
    source_file.seek(0)
    try:
      fcntl.ioctl(destination_file.fileno(), _FICLONE, source_file.fileno())
    except OSError:
      shutil.copyfileobj(source_file, destination_file)
  executable = os.fstat(source).st_mode & 0o111 != 0
  destination.chmod(0o755 if executable else 0o644)
  return destination.stat().st_size


def _copy_descriptor(source: int, destination: Path, prefix: str) -> int:
  mode = os.fstat(source).st_mode
  if stat.S_ISREG(mode):
    return _clone_or_copy_file(source, destination)
  if not stat.S_ISDIR(mode):
    raise ValueError(f'unsupported entry type at {prefix.rstrip("/")}')
  total = 0
  destination.mkdir(mode=0o755)
  destination.chmod(0o755)
  with os.scandir(source) as entries:
    names = sorted(entry.name for entry in entries)
  for name in names:
    target = destination / name
    inner = f'{prefix}{name}'
    if stat.S_ISLNK(os.stat(name, dir_fd=source, follow_symlinks=False).st_mode):
      os.symlink(os.readlink(name, dir_fd=source), target)
    else:
      with contextlib.ExitStack() as stack:
        descriptor = _open_component(stack, source, name, directory=False)
        total += _copy_descriptor(descriptor, target, f'{inner}/')
  return total


def _private_copy(source: Path | int, destination: Path, prefix: str = '') -> int:
  """copy from opened descriptors, returning the private copy's file bytes."""
  if isinstance(source, int):
    return _copy_descriptor(source, destination, prefix)
  with _descriptor(source, os.O_RDONLY) as descriptor:
    return _copy_descriptor(descriptor, destination, prefix)


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


def _tree_components(identity: PeerDescription, path: str) -> list[str]:
  if not path.startswith('/'):
    raise ValueError(f'{path!r} must be an absolute path')
  components = path.split('/')[1:]
  root = list(identity.tree_view.parts[1:])
  if components[: len(root)] != root or any(component in ('.', '..') for component in components):
    raise ValueError(f'{path!r} is outside the tree at {identity.tree_view}')
  return [component for component in components[len(root) :] if component != '']


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
    self._mint_directories: dict[str, Path] = {}
    self._bytes = 0
    root = store_dir(workspace.name)
    if root.exists():
      _remove_store(root)  # a leftover from a crashed session; the store is ride-scoped
    self._objects = root / 'objects'
    self._staging = root / 'staging'
    self._views = root / 'shared'
    self._jobs = root / 'jobs'
    for directory in (self._objects, self._staging, self._views, self._jobs):
      directory.mkdir(parents=True)
    self._audit_file = audit_file(workspace.name)
    self.provision_mint(workspace.name)
    if root_boxed:
      self.view(workspace.name)

  def close(self) -> None:
    """Remove the store when the ride ends; keep its audit."""
    try:
      _remove_store(store_dir(self.ride))
    except OSError as e:
      log.warning('could not remove the artifact store %s: %s', store_dir(self.ride), e)

  def view(self, peer_workspace: str) -> Path:
    """the peer's view directory, created on first use — the source of its
    read-only in-container mount."""
    directory = self._views / peer_workspace
    directory.mkdir(exist_ok=True)
    return directory

  def provision_mint(self, peer_workspace: str) -> Path:
    """create a launch-built workspace's mint directory."""
    directory = mint_dir(self.ride, peer_workspace)
    directory.mkdir(parents=True, exist_ok=True)
    with self._lock:
      self._mint_directories[peer_workspace] = directory
    return directory

  def mint_directory(self, peer_workspace: str) -> Optional[Path]:
    """the provisioned mint directory, or None for a workspace with none."""
    with self._lock:
      return self._mint_directories.get(peer_workspace)

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
    path: str,
    name: Optional[str] = None,
  ) -> tuple[str, int]:
    """ingest the file or directory at the minting peer's wire `path`
    as `name` (by default the path's last component) and return its ref and
    size, shared with the minter and its ancestors. Raises `ArtifactDenied` on
    any refusal; heavy, called off-loop."""
    try:
      components = _tree_components(identity, path)
      name = path_name(path) if name is None else name
      with _open_source(identity.tree, components) as source:
        return self._adopt(source, name, identity.workspace, ancestors, event='mint', origin=path)
    except _SymbolicLink as error:
      raise ArtifactDenied(f'{path!r} crosses a symbolic link at {error.component!r}') from None
    except (FileNotFoundError, NotADirectoryError):
      raise ArtifactDenied(f'no file or directory at {path!r} in the tree') from None
    except (OSError, ValueError) as error:
      raise ArtifactDenied(str(error)) from None

  def mint_entry(
    self, identity: PeerDescription, ancestors: Sequence[str], entry: str, name: str
  ) -> tuple[str, int]:
    """ingest one regular entry of the workspace's provisioned mint directory."""
    directory = self.mint_directory(identity.workspace)
    if directory is None:
      raise ArtifactDenied('no mint directory for this peer')
    try:
      with _open_source(directory, [entry], file_only=True) as source:
        return self._adopt(
          source, name, identity.workspace, ancestors, event='mint', origin=f'entry:{entry}'
        )
    except FileNotFoundError:
      raise ArtifactDenied(f'no entry {entry} in the mint directory') from None
    except (_SymbolicLink, ValueError):
      raise ArtifactDenied(f'entry {entry} is not a regular file') from None
    except OSError as error:
      if error.errno in (errno.ENXIO, errno.ENODEV):
        raise ArtifactDenied(f'entry {entry} is not a regular file') from None
      raise ArtifactDenied(str(error)) from None

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

    Content below the host-named source is opened without following links."""
    try:
      with _descriptor(source, os.O_RDONLY) as descriptor:
        return self._adopt(descriptor, name, workspace, ancestors, event=event, origin=origin)
    except (OSError, ValueError) as error:
      raise ArtifactDenied(str(error)) from None

  def _adopt(
    self,
    source: int,
    name: str,
    workspace: str,
    ancestors: Sequence[str],
    *,
    event: str,
    origin: str,
  ) -> tuple[str, int]:
    digest, size = self._ingest(source, origin)
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

  def _ingest(self, source: int, origin: str) -> tuple[str, int]:
    with _staged(self._staging) as staging:
      size = _private_copy(source, staging)
      digest = digest_path(staging)
      with self._lock:
        if (self._objects / digest).exists():
          return digest, size
        if self._bytes + size > MAX_STORE_BYTES:
          raise ArtifactDenied(f'ingesting {origin!r} would exceed the store byte cap')
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
  if ('path' in args) == ('entry' in args):
    return "artifact.mint needs exactly one of 'path' and 'entry'"
  if 'entry' in args:
    entry = args['entry']
    if not isinstance(entry, str):
      return "artifact.mint 'entry' must be a string"
    error = name_error(entry)
    if error is not None:
      return error
    if 'name' not in args:
      return "artifact.mint with 'entry' needs 'name'"
  else:
    path = args['path']
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
    self._answer_off_loop(
      context, peer, message.request_id, lambda: self._minted(identity, ancestors, args)
    )

  def _minted(
    self, identity: PeerDescription, ancestors: Sequence[str], args: dict[str, Any]
  ) -> dict[str, Any]:
    if 'entry' in args:
      ref, size = self._store.mint_entry(identity, ancestors, args['entry'], args['name'])
    else:
      ref, size = self._store.mint(identity, ancestors, args['path'], args.get('name'))
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
