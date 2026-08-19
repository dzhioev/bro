import contextlib
import fcntl
import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Generator
from pathlib import Path
from typing import ClassVar, Optional

from bro.base import log
from bro.workspace.git import git_run
from bro.workspace.paths import workspace_dir, workspace_tree, workspaces_dir
from ride.workspace.docker import image_tag
from ride.workspace.metadata import (
  WorkspaceKind,
  WorkspaceMetadata,
  is_workspace,
  read_metadata,
  workspace_branch,
  write_metadata,
)


class SessionBusy(RuntimeError):
  """a workspace's session lock is held by a live session."""


class KindMismatch(ValueError):
  """a launch asked for a kind the named workspace was not created as."""


def _last_active(tree: Path) -> Optional[float]:
  if not tree.is_dir():
    return None
  result = subprocess.run(
    ['find', str(tree), '-not', '-path', '*/.git/*', '-type', 'f', '-printf', '%T@\n'],
    capture_output=True,
    text=True,
  )
  if result.returncode != 0 or len(result.stdout.strip()) == 0:
    return None
  return max(float(line) for line in result.stdout.splitlines() if len(line) > 0)


KILLED = 'killed'


def _cleanup_image() -> Optional[str]:
  """a locally-present session image usable to delete root-owned container files.

  prefers the current image tag, then any other locally-present image of the
  project's repository. returns None when none exist (nothing to escalate the
  removal with).
  """
  tag = image_tag()
  if subprocess.run(['docker', 'image', 'inspect', tag], capture_output=True).returncode == 0:
    return tag
  listed = subprocess.run(
    ['docker', 'images', '--format', '{{.Repository}}:{{.Tag}}', tag.split(':')[0]],
    capture_output=True,
    text=True,
  )
  for line in listed.stdout.splitlines():
    candidate = line.strip()
    if len(candidate) > 0 and '<none>' not in candidate:
      return candidate
  return None


def _remove_container_dir(path: Path, image: Optional[str]) -> None:
  """remove a container workspace's directory, including files the host user
  can't unlink.

  container processes can leave files owned by uids that don't match the host
  user (e.g. a pre-fix `ride exec` ran as root, or root-running tooling reached
  the docker socket), which a host-side rmtree hits EPERM on. try a plain rmtree
  first, then escalate to deleting from inside a throwaway root container, which
  can unlink regardless of owner. raises RuntimeError if removal fails.
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


class Workspace(ABC):
  """a managed workspace: one directory (`path`) holding an isolated copy of the
  operated repo (`tree`) plus every record kept about it.

  The subclasses are the kinds, which differ only in how the tree is
  materialized and released; launch stays with the surfaces (worktrees.py /
  containers.py) and only consumes a Workspace for the post-run finish. Session
  state a launch surface attaches to a workspace is that surface's own — its
  readers and teardown compose around `remove()` there.
  """

  kind: ClassVar[WorkspaceKind]

  def __init__(self, name: str, project: Path, metadata: WorkspaceMetadata):
    self.name = name
    self.project = project
    self.metadata = metadata

  @property
  def path(self) -> Path:
    return workspace_dir(self.project, self.name)

  @property
  def tree(self) -> Path:
    return workspace_tree(self.project, self.name)

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
  def _release_tree(self) -> None:
    """release whatever the tree holds outside the workspace directory, before
    the directory itself goes."""

  @abstractmethod
  def _remove_dir(self) -> None:
    """remove the workspace directory."""

  def remove(self) -> None:
    """remove the workspace: the tree and every record kept about it."""
    self._release_tree()
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
    """whether a session currently owns this workspace. `mounts` is the running
    containers' mount set, which only a container workspace reads."""
    try:
      handle = os.fdopen(os.open(self.lockfile, os.O_RDONLY), 'r')
    except FileNotFoundError:
      return False
    with contextlib.closing(handle):
      try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
      except BlockingIOError:
        return True
      fcntl.flock(handle, fcntl.LOCK_UN)
    return False

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

  def is_clean(self) -> tuple[bool, list[str]]:
    """whether the workspace is safe to remove: its last session finished
    successfully. the recorded session end is the one deciding factor — anything
    else (a failure, a kill, no record) keeps the workspace for inspection and
    recovery. returns (safe, reasons)."""
    try:
      end = self._session_end_file.read_text().strip()
    except FileNotFoundError:
      return False, ['no recorded session end']
    if end == '0':
      return True, []
    if end == KILLED:
      return False, ['last session was killed']
    return False, [f'last session exited with code {end}']

  def last_active(self) -> Optional[float]:
    return _last_active(self.tree)

  @classmethod
  def open(cls, name: str, project: Path) -> 'Workspace':
    """the workspace `name`, as its recorded metadata describes it. raises
    `ValueError` when no workspace of that name exists."""
    metadata = read_metadata(project, name)
    return _KINDS[metadata.kind](name, project, metadata)

  @classmethod
  def create(
    cls, name: str, project: Path, kind: WorkspaceKind, *, throwaway: bool = False
  ) -> 'Workspace':
    """record a new workspace of `kind`. the tree is materialized separately, by
    the launch surface that needs it."""
    metadata = WorkspaceMetadata(kind=kind, branch=workspace_branch(name), throwaway=throwaway)
    write_metadata(project, name, metadata)
    return _KINDS[kind](name, project, metadata)

  @classmethod
  def ensure(
    cls, name: str, project: Path, kind: WorkspaceKind, *, throwaway: bool = False
  ) -> 'Workspace':
    """the workspace `name`, created as `kind` when it doesn't exist yet.

    an existing workspace keeps the kind it was created as; asking for the other
    one raises `KindMismatch` rather than running a workspace as something it
    isn't.
    """
    if not is_workspace(project, name):
      return cls.create(name, project, kind, throwaway=throwaway)
    workspace = cls.open(name, project)
    if workspace.kind is not kind:
      raise KindMismatch(
        f'workspace {name!r} is a {workspace.kind} workspace, not {kind}; '
        f'pick another name or remove it with `ride clean --force {name}`'
      )
    return workspace

  @classmethod
  def all(cls, project: Path) -> list['Workspace']:
    # metadata reads only: the per-workspace I/O (subject/last_active/is_clean)
    # is left to the parallelized loops in listing.py / clean.py.
    root = workspaces_dir(project)
    if not root.is_dir():
      return []
    return [cls.open(path.name, project) for path in sorted(root.iterdir()) if path.is_dir()]


class WorktreeWorkspace(Workspace):
  """a workspace whose tree is a git worktree of the project, run on the host."""

  kind = WorkspaceKind.WORKTREE

  def _release_tree(self) -> None:
    # a workspace whose tree was never materialized (a launch that failed before
    # creating it) has no worktree registration to release, and git would refuse
    # the removal of a path it doesn't know.
    if self.tree.is_dir():
      removed = git_run('worktree', 'remove', '--force', str(self.tree))
      if removed.returncode != 0:
        raise RuntimeError(f'{self.tree}: git worktree remove failed: {removed.stderr.strip()}')
    deleted = git_run('branch', '-D', self.metadata.branch)
    if deleted.returncode != 0:
      log.warning('could not delete branch %s: %s', self.metadata.branch, deleted.stderr.strip())

  def _remove_dir(self) -> None:
    shutil.rmtree(self.path)


class ContainerWorkspace(Workspace):
  """a workspace whose tree is a fresh clone, bind-mounted into a container as
  `/workspace`."""

  kind = WorkspaceKind.CONTAINER

  def is_active(self, mounts: set[str]) -> bool:
    # the running container counts on its own: a launcher killed outright releases
    # the lock while leaving its container bound to the workspace mount.
    return str(self.tree) in mounts or super().is_active(mounts)

  def _release_tree(self) -> None:
    pass

  def _remove_dir(self) -> None:
    _remove_container_dir(self.path, _cleanup_image())


_KINDS: dict[WorkspaceKind, type[Workspace]] = {
  WorkspaceKind.WORKTREE: WorktreeWorkspace,
  WorkspaceKind.CONTAINER: ContainerWorkspace,
}
