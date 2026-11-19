import contextlib
import fcntl
import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Optional

from bro.monitor import workspace_session_dir
from bro.workspace.paths import (
  is_workspace_name,
  runtime_base,
  workspace_dir,
  workspace_tree,
  workspaces_dir,
)
from ride.attachments import Attachment, get_attachment_type
from ride.workspace.docker import runtime_image_tag
from ride.workspace.metadata import (
  Isolation,
  UnrecognizedWorkspaceRecord,
  WorkspaceMetadata,
  WorkspaceNotFound,
  read_metadata,
  unrecognized_workspace_record,
  write_metadata,
)


@contextlib.contextmanager
def _hold_workspace_namespace() -> Generator[None]:
  root = workspaces_dir()
  root.mkdir(parents=True, exist_ok=True)
  with (root / '.lock').open('a+') as handle:
    fcntl.flock(handle, fcntl.LOCK_EX)
    yield


def _resolve_external_tree(tree: Optional[Path], isolation: Isolation) -> Optional[Path]:
  if tree is None:
    return None
  resolved = tree.expanduser().resolve()
  if isolation is not Isolation.UNBOXED:
    raise ValueError('--tree requires --unboxed')
  if not resolved.is_dir():
    raise ValueError(f'external workspace tree is not an existing directory: {resolved}')
  root = runtime_base().resolve()
  if resolved == root or resolved.is_relative_to(root):
    raise ValueError(f'external workspace tree must be outside the runtime root {root}: {resolved}')
  return resolved


class SessionBusy(RuntimeError):
  """a workspace's session lock is held by a live session."""


class WorkspaceActive(RuntimeError):
  pass


class IsolationMismatch(ValueError):
  """A launch asked for an isolation the named workspace was not created with."""


class AttachmentMismatch(ValueError):
  """a launch named a different attachment than the workspace records."""


KILLED = 'killed'
# the session's last-activity mark among its session records, touched by the
# session runner and its harness as the session works
ACTIVITY_FILENAME = 'activity'


def _cleanup_image() -> Optional[str]:
  """A present runtime image usable to delete root-owned container files."""
  tag = runtime_image_tag()
  if subprocess.run(['docker', 'image', 'inspect', tag], capture_output=True).returncode == 0:
    return tag
  listed = subprocess.run(
    ['docker', 'images', tag.split(':')[0], '--format', '{{.Repository}}:{{.Tag}}'],
    capture_output=True,
    text=True,
  )
  return next((line for line in listed.stdout.split() if '<none>' not in line), None)


def _remove_container_dir(path: Path, image: Optional[str]) -> None:
  """remove a boxed workspace's directory, including files the host user
  can't unlink.

  container processes can leave files owned by uids that don't match the host
  user (e.g. a pre-fix `ride exec` ran as root), which a host-side rmtree hits
  EPERM on. try a plain rmtree first, then escalate to deleting from inside a
  throwaway root container, which can unlink regardless of owner. raises
  RuntimeError if removal fails.
  """
  try:
    shutil.rmtree(path)
    return
  except FileNotFoundError:
    return
  except PermissionError:
    pass
  if image is None:
    raise RuntimeError(
      f'{path}: contains files owned by an in-container uid and no session image '
      'is available to remove them as root'
    )
  result = subprocess.run(
    # override the entrypoint and force uid 0 so `rm` runs as root inside the
    # container; mount the host-owned parent so it can delete the child tree
    [
      'docker',
      'run',
      '--rm',
      '-u',
      '0',
      '--entrypoint',
      'rm',
      '-v',
      f'{path.parent}:/target',
      image,
      '-rf',
      f'/target/{path.name}',
    ],
    capture_output=True,
    text=True,
  )
  if result.returncode != 0:
    raise RuntimeError(f'{path}: docker rm failed: {result.stderr.strip()}')
  if path.exists():
    raise RuntimeError(f'{path}: still present after docker rm')


def workspace_path_is_active(path: Path, mounts: set[str]) -> bool:
  """Whether a workspace path is held by a session or running container."""
  if str(path / 'tree') in mounts:
    return True
  try:
    handle = os.fdopen(os.open(path / 'lock', os.O_RDONLY), 'r')
  except FileNotFoundError:
    return False
  with contextlib.closing(handle):
    try:
      fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
      return True
    fcntl.flock(handle, fcntl.LOCK_UN)
  return False


@contextlib.contextmanager
def hold_workspace_removal(path: Path, mounts: set[str]) -> Generator[None]:
  """Claim a workspace against session starts until the removal finishes."""
  if str(path / 'tree') in mounts:
    raise WorkspaceActive(path.name)
  handle = os.fdopen(os.open(path / 'lock', os.O_RDWR | os.O_CREAT, 0o644), 'r+')
  with contextlib.closing(handle):
    try:
      fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exception:
      raise WorkspaceActive(path.name) from exception
    yield


def remove_workspace_path(path: Path) -> None:
  """Remove a workspace whose metadata cannot select an isolation subclass."""
  _remove_container_dir(path, _cleanup_image())


class Workspace(ABC):
  """a managed workspace: one directory (`path`) holding its writable `tree`,
  typed attachments, and every record kept about it.

  The subclasses are the isolations, which differ only in how the tree is
  released; launch stays with the surfaces and consumes a Workspace for the
  post-run finish. Session
  state a launch surface attaches to a workspace is that surface's own — its
  readers and teardown compose around `remove()` there.
  """

  isolation: ClassVar[Isolation]

  def __init__(self, name: str, metadata: WorkspaceMetadata):
    self.name = name
    self.metadata = metadata

  @property
  def attachments(self) -> tuple[Attachment, ...]:
    return self.metadata.attachments

  @property
  def start_directory(self) -> Path:
    return self.tree / self.attachments[0].name if self.attachments else self.tree

  def prepare(self, base: str | None = None) -> None:
    if not self.attachments and base is not None:
      raise ValueError('a detached workspace has no attachment base')
    if self.metadata.tree is not None and not self.tree.is_dir():
      raise RuntimeError(f'external workspace tree is gone: {self.tree}')
    self.tree.mkdir(parents=True, exist_ok=True)
    for attachment in self.attachments:
      get_attachment_type(attachment.type).prepare(
        attachment, self.tree / attachment.name, self.path, base
      )

  @property
  def path(self) -> Path:
    return workspace_dir(self.name)

  @property
  def tree(self) -> Path:
    return workspace_tree(self.name) if self.metadata.tree is None else Path(self.metadata.tree)

  @property
  def lockfile(self) -> Path:
    return self.path / 'lock'

  @property
  def resume_file(self) -> Path:
    return self.path / 'resume.json'

  @property
  def host_log(self) -> Path:
    """where the launching process's mid-session output goes while an
    interactive session owns the terminal (workspace/spawn.py)."""
    return self.path / 'session.log'

  @property
  def _session_end_file(self) -> Path:
    return self.path / 'exit'

  @abstractmethod
  def _release_tree(self, *, force: bool) -> None:
    """release whatever the tree holds outside the workspace directory."""

  @abstractmethod
  def _remove_dir(self) -> None:
    """remove the workspace directory."""

  def remove(self, *, force: bool = False) -> None:
    """Remove the workspace and its records, leaving an external tree alone."""
    for attachment in self.attachments:
      get_attachment_type(attachment.type).remove(
        attachment, self.tree / attachment.name, force=force
      )
    self._release_tree(force=force)
    self._remove_dir()

  @contextlib.contextmanager
  def hold_session_lock(self) -> Generator[None]:
    """hold this workspace's session lock for the block, or raise `SessionBusy`.

    One session per workspace: concurrent sessions would mutate the same files
    and share the workspace's gitignored state. The flock is the lock — the pid
    written into the file only names the holder in the refusal.
    """
    handle = os.fdopen(os.open(self.lockfile, os.O_RDWR | os.O_CREAT, 0o644), 'r+')
    with contextlib.closing(handle):
      try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
      except BlockingIOError as e:
        raise SessionBusy(
          f'session already active on workspace {self.name!r} '
          f'(pid {handle.read().strip()}); refusing to start a second'
        ) from e
      handle.seek(0)
      handle.truncate()
      handle.write(str(os.getpid()))
      handle.flush()
      yield

  def is_active(self, mounts: set[str]) -> bool:
    """Whether a session currently owns this workspace."""
    return workspace_path_is_active(self.path, mounts)

  def record_session_end(self, code: Optional[int]) -> None:
    """record how this workspace's session ended: the exit code, or None for a
    killed child (no exit code at the seam). every launch seam writes this after
    the session ends; a recorded clean exit is what makes the workspace
    reclaimable (`is_clean`)."""
    self._session_end_file.write_text(str(code) if code is not None else KILLED)

  def clear_session_end(self) -> None:
    """drop the session-end record — every launch seam clears it at session
    start, so a session that dies without reaching its seam's record (a crashed
    host, a wedged launch) leaves none and the workspace is kept."""
    self._session_end_file.unlink(missing_ok=True)

  def _recorded_exit_clean(self) -> tuple[bool, list[str]]:
    try:
      end = self._session_end_file.read_text().strip()
    except FileNotFoundError:
      return False, ['no recorded session end']
    if end == '0':
      return True, []
    if end == KILLED:
      return False, ['last session was killed']
    return False, [f'last session exited with code {end}']

  def is_clean(self) -> tuple[bool, list[str]]:
    """Whether the tree and its attachments are safe to remove, with refusal reasons."""
    if self.metadata.tree is not None:
      return self._recorded_exit_clean()
    if not self.attachments:
      if not self.tree.is_dir() or not any(self.tree.iterdir()):
        return True, []
      return False, ['detached workspace tree is not empty']
    results = [
      get_attachment_type(attachment.type).cleanliness(
        attachment, self.tree / attachment.name, self._recorded_exit_clean()
      )
      for attachment in self.attachments
    ]
    return all(clean for clean, _ in results), [
      reason for _, reasons in results for reason in reasons
    ]

  def last_active(self) -> Optional[float]:
    """when the workspace's session last marked activity, or None if none ever did."""
    try:
      return (workspace_session_dir(self.path) / ACTIVITY_FILENAME).stat().st_mtime
    except FileNotFoundError:
      return None

  @classmethod
  def open(cls, name: str) -> 'Workspace':
    metadata = read_metadata(name)
    return _ISOLATIONS[metadata.isolation](name, metadata)

  @classmethod
  def _create(
    cls,
    name: str,
    attachments: tuple[Attachment, ...],
    isolation: Isolation,
    *,
    throwaway: bool,
    tree: Optional[Path],
  ) -> 'Workspace':
    resolved_tree = _resolve_external_tree(tree, isolation)
    if resolved_tree is not None and attachments:
      raise ValueError('an external tree requires a detached workspace')
    if resolved_tree is not None:
      for workspace in cls.all():
        if (
          isinstance(workspace, Workspace)
          and workspace.name != name
          and workspace.metadata.tree == str(resolved_tree)
        ):
          raise ValueError(
            f'external workspace tree {resolved_tree} is already recorded by workspace '
            f'{workspace.name!r}'
          )
    path = workspace_dir(name)
    if path.exists():
      read_metadata(name)
      raise ValueError(f'workspace {name!r} already exists')
    metadata = WorkspaceMetadata(
      isolation=isolation,
      attachments=attachments,
      throwaway=throwaway,
      tree=None if resolved_tree is None else str(resolved_tree),
    )
    write_metadata(name, metadata)
    return _ISOLATIONS[isolation](name, metadata)

  @classmethod
  def create(
    cls,
    name: str,
    attachments: tuple[Attachment, ...],
    isolation: Isolation,
    *,
    throwaway: bool = False,
    tree: Optional[Path] = None,
  ) -> 'Workspace':
    with _hold_workspace_namespace():
      return cls._create(name, attachments, isolation, throwaway=throwaway, tree=tree)

  @classmethod
  def ensure(
    cls,
    name: str,
    attachments: tuple[Attachment, ...],
    isolation: Isolation,
    *,
    throwaway: bool = False,
    tree: Optional[Path] = None,
  ) -> 'Workspace':
    resolved_tree = _resolve_external_tree(tree, isolation)
    if resolved_tree is not None and attachments:
      raise ValueError('an external tree requires a detached workspace')
    with _hold_workspace_namespace():
      path = workspace_dir(name)
      if not path.exists():
        return cls._create(
          name,
          attachments,
          isolation,
          throwaway=throwaway,
          tree=resolved_tree,
        )
      workspace = cls.open(name)
      if workspace.isolation is not isolation:
        raise IsolationMismatch(
          f'workspace {name!r} is {workspace.isolation}, not {isolation}; '
          f'pick another name or remove it with `ride clean --force {name}`'
        )

      def identities(records):
        return [
          (attachment.type, get_attachment_type(attachment.type).identity(attachment))
          for attachment in records
        ]

      if identities(workspace.attachments) != identities(attachments):
        raise AttachmentMismatch(f'workspace {name!r} has different attachments from the launch')
      if workspace.metadata.tree != (None if resolved_tree is None else str(resolved_tree)):
        recorded = workspace.metadata.tree or 'its managed tree'
        requested = str(resolved_tree) if resolved_tree is not None else 'a managed tree'
        raise ValueError(
          f'workspace {name!r} records {recorded}, not {requested}; '
          f'pick another name or remove it with `ride clean --force {name}`'
        )
      return workspace

  @classmethod
  def all(cls) -> list['Workspace | UnrecognisedWorkspace']:
    root = workspaces_dir()
    if not root.is_dir():
      return []
    result: list[Workspace | UnrecognisedWorkspace] = []
    for path in sorted(root.iterdir()):
      if not path.is_dir() or not is_workspace_name(path.name):
        continue
      try:
        result.append(cls.open(path.name))
      except UnrecognizedWorkspaceRecord as error:
        result.append(UnrecognisedWorkspace(path.name, error))
      except WorkspaceNotFound as error:
        result.append(
          UnrecognisedWorkspace(path.name, unrecognized_workspace_record(path.name, error))
        )
    return result


@dataclass(frozen=True)
class UnrecognisedWorkspace:
  name: str
  error: UnrecognizedWorkspaceRecord


class UnboxedWorkspace(Workspace):
  """A workspace whose tree runs directly on the launcher's filesystem."""

  isolation = Isolation.UNBOXED

  def _release_tree(self, *, force: bool) -> None:
    del force

  def _remove_dir(self) -> None:
    shutil.rmtree(self.path)


class BoxedWorkspace(Workspace):
  """a workspace whose tree is bind-mounted into a container as `/tree`."""

  isolation = Isolation.BOXED

  def _release_tree(self, *, force: bool) -> None:
    del force

  def _remove_dir(self) -> None:
    _remove_container_dir(self.path, _cleanup_image())


_ISOLATIONS: dict[Isolation, type[Workspace]] = {
  Isolation.UNBOXED: UnboxedWorkspace,
  Isolation.BOXED: BoxedWorkspace,
}
