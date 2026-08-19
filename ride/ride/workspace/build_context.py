"""the container build context: a normalized tar streamed to `docker build -`.

A stdin tar is filtered by no `.dockerignore`, so the archive is exactly what this
module puts in it: the operated repo's files plus the framework assets the image
needs, injected from the installed package under `INJECTED_PREFIX`.

Every member is normalized — fixed mtime, zeroed ownership, mode reduced to the
executable bit — because a stdin context's cache key is the archive's content
digest, so any metadata churn re-runs the expensive venv bake.
"""

import io
import subprocess
import tarfile
import tomllib
from pathlib import Path

from bro.shell import shell_dir
from bro.workspace.project import project_config

SETUP_DIR = Path(__file__).resolve().parent.parent / 'setup'
CONTAINER_DIR = SETUP_DIR / 'container'
BASE_IMAGE_DIR = SETUP_DIR / 'base_image'
SHELL_DIR = shell_dir()
SHELL_HELPERS = ('prelude.sh', 'log.sh', 'strict.sh')

INJECTED_PREFIX = '.bro-container'
DOCKERFILE_PATH = f'{INJECTED_PREFIX}/Dockerfile'
MANIFEST_PREFIX = f'{INJECTED_PREFIX}/manifests'

FRAMEWORK_FILES = {
  DOCKERFILE_PATH: CONTAINER_DIR / 'Dockerfile',
  f'{INJECTED_PREFIX}/entrypoint.sh': CONTAINER_DIR / 'entrypoint.sh',
  f'{INJECTED_PREFIX}/git.sh': CONTAINER_DIR / 'git.sh',
  **{f'{INJECTED_PREFIX}/{name}': SHELL_DIR / name for name in SHELL_HELPERS},
}

_FIXED_MTIME = 0
# the injected assets carry a fixed mode so the context digest never depends on the
# modes the framework's own install happened to land. nothing in the image execs
# them in place — they are sourced, read, or chmod'd by the Dockerfile after the COPY.
_INJECTED_MODE = 0o644


def _member_directories(project: Path) -> list[Path]:
  """the uv workspace member directories declared by `project`'s pyproject.toml."""
  table = tomllib.loads((project / 'pyproject.toml').read_text())
  workspace = table.get('tool', {}).get('uv', {}).get('workspace')
  if workspace is None:
    return []
  excluded = {path for pattern in workspace.get('exclude', []) for path in project.glob(pattern)}
  members = []
  for pattern in workspace.get('members', []):
    for path in sorted(project.glob(pattern)):
      if not path.is_dir() or path in excluded:
        continue
      if not (path / 'pyproject.toml').is_file():
        raise FileNotFoundError(f'workspace member {path} has no pyproject.toml')
      members.append(path)
  return members


def manifest_paths(project: Path) -> list[str]:
  """project-relative paths of every manifest uv reads to resolve the environment.

  The members are listed alongside the root manifests because `uv.lock` records a
  workspace member as an editable path source with no content hash — the lock is
  unchanged by an edit to a member's own pyproject.toml.
  """
  paths = ['pyproject.toml', 'uv.lock']
  paths += [
    f'{directory.relative_to(project).as_posix()}/pyproject.toml'
    for directory in _member_directories(project)
  ]
  missing = [path for path in paths if not (project / path).is_file()]
  if len(missing) > 0:
    raise FileNotFoundError(f'{project} is missing {", ".join(missing)}')
  return paths


def project_files(project: Path) -> list[str]:
  """project-relative paths of the repo's own files the context carries.

  `git ls-files` by default — tracked files as they exist in the working tree, so
  local edits are included while untracked and ignored ones stay out. A repo that
  needs more overrides the producer with `[tool.bro] build-context-command`.
  """
  command = project_config().build_context_command
  if command is not None:
    listed = subprocess.run(
      command, shell=True, cwd=project, capture_output=True, text=True, check=True
    )
    return [line for line in listed.stdout.splitlines() if len(line) > 0]
  listed = subprocess.run(['git', 'ls-files', '-z'], cwd=project, capture_output=True, check=True)
  return [name for name in listed.stdout.decode().split('\0') if len(name) > 0]


def _add(archive: tarfile.TarFile, name: str, source: Path, mode: int) -> None:
  content = source.read_bytes()
  info = tarfile.TarInfo(name)
  info.size = len(content)
  info.mtime = _FIXED_MTIME
  info.mode = mode
  archive.addfile(info, io.BytesIO(content))


def _project_mode(source: Path) -> int:
  return 0o755 if source.stat().st_mode & 0o111 != 0 else 0o644


def _add_directory(archive: tarfile.TarFile, name: str) -> None:
  info = tarfile.TarInfo(name)
  info.type = tarfile.DIRTYPE
  info.mtime = _FIXED_MTIME
  info.mode = 0o755
  archive.addfile(info)


def _parents(name: str) -> list[str]:
  parts = name.split('/')[:-1]
  return ['/'.join(parts[: index + 1]) for index in range(len(parts))]


def assemble(project: Path) -> bytes:
  """the build context for `project`'s session image, as a normalized tar."""
  entries = {name: project / name for name in project_files(project) if (project / name).is_file()}
  collisions = sorted(name for name in entries if name.split('/')[0] == INJECTED_PREFIX)
  if len(collisions) > 0:
    raise ValueError(
      f'{INJECTED_PREFIX}/ is reserved for the framework build assets; '
      f'{project} carries {", ".join(collisions)}'
    )
  modes = {name: _project_mode(source) for name, source in entries.items()}
  injected = {
    **FRAMEWORK_FILES,
    **{f'{MANIFEST_PREFIX}/{name}': project / name for name in manifest_paths(project)},
  }
  entries.update(injected)
  modes.update(dict.fromkeys(injected, _INJECTED_MODE))
  # a parent's name is a strict prefix of its children's, so one sort orders every
  # directory ahead of what it holds
  directories = {parent for name in entries for parent in _parents(name) if parent not in entries}
  buffer = io.BytesIO()
  with tarfile.open(fileobj=buffer, mode='w', format=tarfile.GNU_FORMAT) as archive:
    for name in sorted(entries.keys() | directories):
      if name in entries:
        _add(archive, name, entries[name], modes[name])
      else:
        _add_directory(archive, name)
  return buffer.getvalue()
