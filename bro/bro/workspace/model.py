import json
import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cw.docker import _image_tag
from cw.git import git_run, no_prompt_env
from cw.paths import (
  _containers_dir,
  _encode_claude_path,
  _host_log_dir,
  _latest_jsonl,
  _session_claude_dir,
  _worktrees_dir,
)

_CONTAINER_PREFIX = 'c:'


def _format_ref(name: str, is_container: bool) -> str:
  return f'{_CONTAINER_PREFIX}{name}' if is_container else name


def _parse_ref(ref: str) -> tuple[str, bool]:
  if ref.startswith(_CONTAINER_PREFIX):
    return ref[len(_CONTAINER_PREFIX) :], True
  return ref, False


def _host_pidfile(project: Path, name: str) -> Path:
  # per-worktree git admin dir (outside the working tree, so it never shows up in
  # `git status` and is cleaned up with the worktree). `cw` writes its own pid here
  # for the session's duration.
  return project / '.git' / 'worktrees' / name / 'cw-session.pid'


def _read_subject(projects_dir: Path) -> Optional[str]:
  latest = _latest_jsonl(projects_dir)
  if latest is None:
    return None
  try:
    f = latest.open()
  except OSError:
    return None
  with f:
    for line in f:
      try:
        d = json.loads(line)
      except json.JSONDecodeError:
        continue
      if d.get('type') != 'user' or d.get('isSidechain') is True:
        continue
      content = d.get('message', {}).get('content')
      text: Optional[str] = None
      if isinstance(content, str):
        text = content
      elif isinstance(content, list):
        for c in content:
          if isinstance(c, dict) and c.get('type') == 'text':
            text = c.get('text')
            break
      if text is None:
        continue
      stripped = text.lstrip()
      if stripped.startswith('<'):
        continue
      first_line = stripped.split('\n', 1)[0].strip()
      if len(first_line) > 0:
        return first_line
  return None


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


@dataclass(frozen=True)
class _GitRunner:
  """the per-workspace git context the shared is_clean policy runs against.

  host and container differ in ways the policy can't hardcode, so each subtype
  supplies them here:
  - status_env / ancestry_env: two env overlays. a container clone's own commits
    live only in its object store, so the ancestry walk (run in the shared host
    repo) needs the clone's objects exposed as an alternate, while the status read
    needs the host repo's objects exposed so the clone's /host-repo alternates
    resolve. on host both are just no_prompt_env().
  - check_root: where the origin fetch / origin-master / ancestry checks cwd into.
    host: the worktree itself; container: the shared host repo (the clone's own
    remotes are unreachable — origin is HTTPS GitHub without creds).
  - read_head: the ref to compare against origin/master. host: the literal 'HEAD';
    container: the clone's HEAD sha (read separately so the walk can run in the
    shared repo). returns (ref, reason) — reason set when the read failed.
  - bring_in_submodule_head: a per-submodule hook to fetch the container clone's
    submodule HEAD into the shared repo before the ancestry check. no-op on host.
  """

  path: Path
  check_root: Path
  status_env: Mapping[str, str]
  ancestry_env: Mapping[str, str]
  read_head: Callable[[], tuple[Optional[str], Optional[str]]]
  bring_in_submodule_head: Callable[[str, Path], bool]


def _check_clean(runner: _GitRunner, refresh_origin: bool) -> tuple[bool, list[str]]:
  """the shared policy: status empty ∧ HEAD an ancestor of origin/master ∧ every
  submodule pushed. returns (safe, reasons) where reasons lists what prevents removal.

  refresh_origin: fetch origin/master before the ancestry check. callers that run
  many checks concurrently (clean_workspaces) fetch once up front and pass False —
  a per-check fetch into the shared repo races on the ref lock.
  """
  git_env = no_prompt_env()
  reasons: list[str] = []
  status = git_run('status', '--porcelain', cwd=runner.path, env=runner.status_env)
  if status.returncode != 0:
    return False, ['cannot read git status']
  if len(status.stdout.strip()) > 0:
    reasons.append('uncommitted or untracked changes')

  origin_ok = True
  if refresh_origin:
    fetch = git_run('fetch', '--quiet', 'origin', 'master', cwd=runner.check_root, env=git_env)
    origin_ok = fetch.returncode == 0
    if not origin_ok:
      reasons.append('could not fetch origin/master')
  if origin_ok:
    head_ref, head_reason = runner.read_head()
    if head_reason is not None:
      reasons.append(head_reason)
    if head_ref is not None:
      master = git_run('rev-parse', '--verify', 'origin/master', cwd=runner.check_root, env=git_env)
      if master.returncode != 0:
        reasons.append('origin/master not found')
      else:
        ancestor = git_run(
          'merge-base', '--is-ancestor', head_ref, 'origin/master',
          cwd=runner.check_root, env=runner.ancestry_env,
        )  # fmt: skip
        if ancestor.returncode != 0:
          ahead = git_run(
            'rev-list', '--count', head_ref, '^origin/master',
            cwd=runner.check_root, env=runner.ancestry_env,
          )  # fmt: skip
          n = ahead.stdout.strip() if ahead.returncode == 0 else '?'
          reasons.append(f'{n} commit(s) not on origin/master')

  sub_status = git_run('submodule', 'status', cwd=runner.path, env=runner.status_env)
  if sub_status.returncode == 0:
    for line in sub_status.stdout.strip().splitlines():
      stripped = line.strip()
      if stripped.startswith('-'):
        continue
      parts = stripped.lstrip('+').split()
      if len(parts) < 2:
        continue
      sub_hash, sub_path = parts[0], parts[1]
      sub_root = runner.check_root / sub_path
      if git_run('fetch', '--quiet', 'origin', cwd=sub_root, env=git_env).returncode != 0:
        reasons.append(f'submodule {sub_path}: could not fetch origin')
        continue
      if not runner.bring_in_submodule_head(sub_path, sub_root):
        reasons.append(f"submodule {sub_path}: could not fetch container's HEAD")
        continue
      sub_default = git_run('rev-parse', '--verify', 'origin/HEAD', cwd=sub_root, env=git_env)
      remote_ref = 'origin/HEAD' if sub_default.returncode == 0 else 'origin/master'
      sub_ancestor = git_run(
        'merge-base', '--is-ancestor', sub_hash, remote_ref, cwd=sub_root, env=git_env
      )
      if sub_ancestor.returncode != 0:
        reasons.append(f'submodule {sub_path}: commit {sub_hash[:8]} not pushed to remote')

  return len(reasons) == 0, reasons


def _host_git_runner(path: Path) -> _GitRunner:
  git_env = no_prompt_env()

  def read_head() -> tuple[Optional[str], Optional[str]]:
    return 'HEAD', None

  def bring_in_submodule_head(sub_path: str, sub_root: Path) -> bool:
    return True  # submodules are checked in place; nothing to bring into a shared repo

  return _GitRunner(
    path=path,
    check_root=path,
    status_env=git_env,
    ancestry_env=git_env,
    read_head=read_head,
    bring_in_submodule_head=bring_in_submodule_head,
  )


def _host_path_is_clean(path: Path, refresh_origin: bool = True) -> tuple[bool, list[str]]:
  """run the host clean policy on an arbitrary path — for `cw check-clean` with no
  ref (the cwd), which isn't a managed Workspace. HostWorktree.is_clean delegates here."""
  return _check_clean(_host_git_runner(path), refresh_origin)


def _cleanup_image() -> Optional[str]:
  """a locally-present ppp-cw image usable to delete root-owned container files.

  prefers the current image tag, then any other locally-present ppp-cw image.
  returns None when none exist (nothing to escalate the removal with).
  """
  tag = _image_tag()
  if subprocess.run(['docker', 'image', 'inspect', tag], capture_output=True).returncode == 0:
    return tag
  listed = subprocess.run(
    ['docker', 'images', '--format', '{{.Repository}}:{{.Tag}}', 'ppp-cw'],
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
      f'{path}: contains files owned by an in-container uid and no ppp-cw image '
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
  (is_active / is_clean / remove / claude_projects_dir). Launch stays mode-specific
  (worktrees.py / containers.py) and only consumes a Workspace for the post-run finish.
  """

  def __init__(self, name: str, project: Path):
    self.name = name
    self.project = project

  @property
  def session_dir(self) -> Path:
    """the session's private claude state dir — a container session's ~/.claude
    overlay (mounted), a host session's CLAUDE_CONFIG_DIR target (provisioned by
    cw/claude_config.py)."""
    return _session_claude_dir(self.name)

  @property
  @abstractmethod
  def path(self) -> Path: ...

  @property
  @abstractmethod
  def ref(self) -> str: ...

  @abstractmethod
  def claude_projects_dir(self) -> Path: ...

  @abstractmethod
  def is_active(self, mounts: set[str]) -> bool: ...

  @abstractmethod
  def remove(self) -> None: ...

  @abstractmethod
  def _git_runner(self) -> Optional[_GitRunner]: ...

  def is_clean(self, refresh_origin: bool = True) -> tuple[bool, list[str]]:
    runner = self._git_runner()
    if runner is None:
      return False, ['not a git repository']
    return _check_clean(runner, refresh_origin)

  def _remove_host_log(self) -> None:
    # the session host log (cw/spawn.py:_HostLogRedirect) is keyed by `ref`, the
    # same mode-prefixed key the launch surfaces pass to run_root_via_broker.
    # diagnostics, not audit — unlike var/cw/summon/, it does not survive removal
    (_host_log_dir(self.project) / f'{self.ref}.log').unlink(missing_ok=True)

  def subject(self) -> Optional[str]:
    return _read_subject(self.claude_projects_dir())

  def last_active(self) -> Optional[float]:
    return _last_active(self.path)

  @classmethod
  def from_ref(cls, ref: str, project: Path) -> 'Workspace':
    name, is_container = _parse_ref(ref)
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
    worktrees = _worktrees_dir(project)
    if worktrees.is_dir():
      result.extend(HostWorktree(p.name, project) for p in worktrees.iterdir() if p.is_dir())
    containers = _containers_dir(project)
    if containers.is_dir():
      result.extend(ContainerWorkspace(p.name, project) for p in containers.iterdir() if p.is_dir())
    return result


class HostWorktree(Workspace):
  @property
  def path(self) -> Path:
    return _worktrees_dir(self.project) / self.name

  @property
  def ref(self) -> str:
    return self.name

  @property
  def pidfile(self) -> Path:
    return _host_pidfile(self.project, self.name)

  def claude_projects_dir(self) -> Path:
    # transcripts live in the session's private state dir; a worktree whose
    # sessions were recorded before the dir existed (against the host ~/.claude)
    # is read from the legacy location until a launch migrates it
    # (cw/claude_config.py:_migrate_legacy_transcripts)
    private = self.session_dir / 'projects' / _encode_claude_path(self.path)
    if private.is_dir():
      return private
    legacy = Path.home() / '.claude' / 'projects' / _encode_claude_path(self.path)
    if legacy.is_dir():
      return legacy
    return private

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

  def _git_runner(self) -> Optional[_GitRunner]:
    return _host_git_runner(self.path)

  def remove(self) -> None:
    git_run('worktree', 'remove', '--force', str(self.path))
    git_run('branch', '-D', f'worktree-{self.name}')
    if self.session_dir.is_dir():
      shutil.rmtree(self.session_dir, ignore_errors=True)
    self._remove_host_log()


class ContainerWorkspace(Workspace):
  @property
  def path(self) -> Path:
    return _containers_dir(self.project) / self.name

  @property
  def ref(self) -> str:
    return _format_ref(self.name, True)

  def claude_projects_dir(self) -> Path:
    return self.session_dir / 'projects' / '-workspace'

  def is_active(self, mounts: set[str]) -> bool:
    return str(self.path) in mounts

  def _git_runner(self) -> Optional[_GitRunner]:
    # the clone's own remotes are unreachable from the host (origin = HTTPS GitHub
    # without creds, host remote = /host-repo bind mount), so the ancestry checks
    # run in the shared host repo (self.project) with the two stores cross-exposed as
    # alternates: the host repo's objects to the clone's status read, and the
    # clone's objects to the shared repo's ancestry walk — so the walk reaches the
    # container's local commits without writing them into the shared repo (which
    # would race on the shared refs across concurrent checks).
    if not (self.path / '.git').exists():
      return None
    git_env = no_prompt_env()
    status_env = {
      **git_env,
      'GIT_ALTERNATE_OBJECT_DIRECTORIES': str(self.project / '.git' / 'objects'),
    }
    ancestry_env = {
      **git_env,
      'GIT_ALTERNATE_OBJECT_DIRECTORIES': str(self.path / '.git' / 'objects'),
    }

    def read_head() -> tuple[Optional[str], Optional[str]]:
      head = git_run('rev-parse', 'HEAD', cwd=self.path, env=status_env)
      if head.returncode != 0:
        return None, "could not read container's HEAD"
      return head.stdout.strip(), None

    def bring_in_submodule_head(sub_path: str, sub_root: Path) -> bool:
      fetch = git_run(
        'fetch', '--quiet', str(self.path / sub_path), 'HEAD', cwd=sub_root, env=git_env
      )
      return fetch.returncode == 0

    return _GitRunner(
      path=self.path,
      check_root=self.project,
      status_env=status_env,
      ancestry_env=ancestry_env,
      read_head=read_head,
      bring_in_submodule_head=bring_in_submodule_head,
    )

  def remove(self) -> None:
    # the session state (claude dir, host log) is cleaned in a finally so it never
    # outlives the workspace, even when the workspace dir removal escalates and
    # then fails.
    try:
      _remove_container_dir(self.path, _cleanup_image())
    finally:
      if self.session_dir.is_dir():
        shutil.rmtree(self.session_dir, ignore_errors=True)
      self._remove_host_log()
