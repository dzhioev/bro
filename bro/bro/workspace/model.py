import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from workspace.docker import image_tag
from workspace.git import git_run
from workspace.paths import containers_dir, host_log_dir, session_end_dir, worktrees_dir

_CONTAINER_PREFIX = 'c:'


def format_ref(name: str, is_container: bool) -> str:
  return f'{_CONTAINER_PREFIX}{name}' if is_container else name


def parse_ref(ref: str) -> tuple[str, bool]:
  if ref.startswith(_CONTAINER_PREFIX):
    return ref[len(_CONTAINER_PREFIX) :], True
  return ref, False


def _host_pidfile(project: Path, name: str) -> Path:
  # per-worktree git admin dir (outside the working tree, so it never shows up in
  # `git status` and is cleaned up with the worktree). `cw` writes its own pid here
  # for the session's duration.
  return project / '.git' / 'worktrees' / name / 'cw-session.pid'


def _last_active(workspace: Path) -> Optional[float]:
  if not workspace.is_dir():
    return None
  result = subprocess.run(
    ['find', str(workspace), '-not', '-path', '*/.git/*', '-type', 'f', '-printf', '%T@\n'],
    capture_output=True,
    text=True,
  )
  if result.returncode != 0 or len(result.stdout.strip()) == 0:
    return None
  return max(float(line) for line in result.stdout.splitlines() if len(line) > 0)


KILLED = 'killed'


def _session_end_file(project: Path, ref: str) -> Path:
  return session_end_dir(project) / ref


def record_session_end(project: Path, ref: str, code: Optional[int]) -> None:
  """record how the workspace's session ended: the exit code, or None for a
  killed child (no exit code at the seam). every launch seam writes this after
  the session ends; a recorded clean exit is what makes the workspace
  reclaimable (`Workspace.is_clean`)."""
  file = _session_end_file(project, ref)
  file.parent.mkdir(parents=True, exist_ok=True)
  file.write_text(str(code) if code is not None else KILLED)


def clear_session_end(project: Path, ref: str) -> None:
  """drop the workspace's session-end record — every launch seam clears it at
  session start, so a session that dies without reaching its seam's record
  (a crashed host, a wedged launch) leaves no record and the workspace is kept."""
  _session_end_file(project, ref).unlink(missing_ok=True)


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
  """remove a container workspace dir, including files the host user can't unlink.

  container processes can leave files owned by uids that don't match the host
  user (e.g. a pre-fix `cw exec` ran as root, or root-running tooling reached
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
  """a managed workspace backed by either a host worktree or a container clone.

  owns the inspection + teardown surface where the two kinds' duality lives
  (is_active / is_clean / remove). Launch stays mode-specific (worktrees.py /
  containers.py) and only consumes a Workspace for the post-run finish. Session
  state a launch surface attaches to a workspace is that surface's own — its
  readers and teardown compose around `remove()` there.
  """

  def __init__(self, name: str, project: Path):
    self.name = name
    self.project = project

  @property
  @abstractmethod
  def path(self) -> Path: ...

  @property
  @abstractmethod
  def ref(self) -> str: ...

  @abstractmethod
  def is_active(self, mounts: set[str]) -> bool: ...

  @abstractmethod
  def remove(self) -> None: ...

  def is_clean(self) -> tuple[bool, list[str]]:
    """whether the workspace is safe to remove: its last session finished
    successfully. the recorded session end is the one deciding factor — anything
    else (a failure, a kill, no record) keeps the workspace for inspection and
    recovery. returns (safe, reasons)."""
    file = _session_end_file(self.project, self.ref)
    try:
      end = file.read_text().strip()
    except FileNotFoundError:
      return False, ['no recorded session end']
    if end == '0':
      return True, []
    if end == KILLED:
      return False, ['last session was killed']
    return False, [f'last session exited with code {end}']

  def _remove_session_state(self) -> None:
    # per-session host-side state keyed by `ref` (the mode-prefixed key the launch
    # surfaces pass to the broker root): the session host log
    # (workspace/spawn.py:_HostLogRedirect) and the session-end record. neither
    # survives removal — unlike the var/cw/summon/ audit
    (host_log_dir(self.project) / f'{self.ref}.log').unlink(missing_ok=True)
    clear_session_end(self.project, self.ref)

  def last_active(self) -> Optional[float]:
    return _last_active(self.path)

  @classmethod
  def from_ref(cls, ref: str, project: Path) -> 'Workspace':
    name, is_container = parse_ref(ref)
    workspace: Workspace = (
      ContainerWorkspace(name, project) if is_container else HostWorktree(name, project)
    )
    if not workspace.path.is_dir():
      kind = 'container workspace' if is_container else 'workspace'
      raise ValueError(f'{kind} not found: {ref}')
    return workspace

  @classmethod
  def all(cls, project: Path) -> list['Workspace']:
    # enumeration only (cheap): the per-workspace I/O (subject/last_active/is_clean)
    # is left to the parallelized loops in listing.py / clean.py.
    result: list[Workspace] = []
    worktrees = worktrees_dir(project)
    if worktrees.is_dir():
      result.extend(HostWorktree(p.name, project) for p in worktrees.iterdir() if p.is_dir())
    containers = containers_dir(project)
    if containers.is_dir():
      result.extend(ContainerWorkspace(p.name, project) for p in containers.iterdir() if p.is_dir())
    return result


class HostWorktree(Workspace):
  @property
  def path(self) -> Path:
    return worktrees_dir(self.project) / self.name

  @property
  def ref(self) -> str:
    return self.name

  @property
  def pidfile(self) -> Path:
    return _host_pidfile(self.project, self.name)

  def is_active(self, mounts: set[str]) -> bool:
    # host sessions run plain `claude` (no `-w`), so cw is the worktree's owner for
    # the session; its pid in the lockfile, still alive, means the session is active.
    # `mounts` (running container mounts) is irrelevant to a host worktree — ignored.
    pidfile = self.pidfile
    if not pidfile.is_file():
      return False
    try:
      pid = int(pidfile.read_text().strip())
    except ValueError:
      return False
    try:
      os.kill(pid, 0)
    except ProcessLookupError:
      return False
    except PermissionError:
      return True
    return True

  def remove(self) -> None:
    git_run('worktree', 'remove', '--force', str(self.path))
    git_run('branch', '-D', f'worktree-{self.name}')
    self._remove_session_state()


class ContainerWorkspace(Workspace):
  @property
  def path(self) -> Path:
    return containers_dir(self.project) / self.name

  @property
  def ref(self) -> str:
    return format_ref(self.name, True)

  def is_active(self, mounts: set[str]) -> bool:
    return str(self.path) in mounts

  def remove(self) -> None:
    # the session state is cleaned in a finally so it never outlives the workspace,
    # even when the workspace dir removal escalates and then fails.
    try:
      _remove_container_dir(self.path, _cleanup_image())
    finally:
      self._remove_session_state()
