"""normalized tar contexts for the runtime and project container images."""

import contextlib
import io
import subprocess
import tarfile
import tempfile
import tomllib
from pathlib import Path, PurePosixPath

from bro.shell import shell_dir
from bro.workspace.project import project_config
from ride.repository import Repository, as_repository

SETUP_DIR = Path(__file__).resolve().parent.parent / 'setup'
CONTAINER_DIR = SETUP_DIR / 'container'
SHELL_DIR = shell_dir()
SHELL_HELPERS = ('prelude.sh', 'log.sh', 'strict.sh')

INJECTED_PREFIX = '.bro-container'
DOCKERFILE_PATH = f'{INJECTED_PREFIX}/Dockerfile'
MANIFEST_PREFIX = f'{INJECTED_PREFIX}/manifests'

RUNTIME_FILES = {
  DOCKERFILE_PATH: CONTAINER_DIR / 'Dockerfile',
  f'{INJECTED_PREFIX}/entrypoint.sh': CONTAINER_DIR / 'entrypoint.sh',
  f'{INJECTED_PREFIX}/git.sh': CONTAINER_DIR / 'git.sh',
  **{f'{INJECTED_PREFIX}/{name}': SHELL_DIR / name for name in SHELL_HELPERS},
}
PROJECT_FILES = {DOCKERFILE_PATH: CONTAINER_DIR / 'project.Dockerfile'}

_FIXED_MTIME = 0
_INJECTED_MODE = 0o644


def _local_member_directories(project: Path) -> list[Path]:
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


def _matches(path: str, patterns: list[str]) -> bool:
  candidate = PurePosixPath(path)
  return any(candidate.match(pattern) for pattern in patterns)


def _committed_member_directories(repository: Repository, pyproject: bytes) -> list[str]:
  workspace = tomllib.loads(pyproject.decode()).get('tool', {}).get('uv', {}).get('workspace')
  if workspace is None:
    return []
  files = set(repository.list_files())
  directories = sorted(
    name.removesuffix('/pyproject.toml') for name in files if name.endswith('/pyproject.toml')
  )
  excluded = workspace.get('exclude', [])
  return [
    directory
    for directory in directories
    if _matches(directory, workspace.get('members', [])) and not _matches(directory, excluded)
  ]


def manifest_paths(project: Repository | Path) -> list[str]:
  """project-relative paths of every manifest uv reads, or none for a non-uv repo."""
  repository = as_repository(project)
  lock = repository.read_file('uv.lock')
  if lock is None:
    return []
  pyproject = repository.read_file('pyproject.toml')
  if pyproject is None:
    raise FileNotFoundError(f'{repository.identity} is missing pyproject.toml')
  if repository.is_url:
    members = _committed_member_directories(repository, pyproject)
    return ['pyproject.toml', 'uv.lock', *(f'{directory}/pyproject.toml' for directory in members)]
  return [
    'pyproject.toml',
    'uv.lock',
    *(
      f'{directory.relative_to(repository.git_dir).as_posix()}/pyproject.toml'
      for directory in _local_member_directories(repository.git_dir)
    ),
  ]


@contextlib.contextmanager
def _committed_tree(repository: Repository):
  assert repository.tree_ref is not None
  with tempfile.TemporaryDirectory() as directory:
    archive = subprocess.run(
      ['git', 'archive', repository.tree_ref],
      cwd=repository.git_dir,
      capture_output=True,
      check=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
      source.extractall(directory, filter='data')
    yield Path(directory)


def project_files(project: Repository | Path) -> list[str]:
  """project-relative committed files included when baking the editable workspace."""
  repository = as_repository(project)
  config = repository.project_config() if repository.is_url else project_config(repository.git_dir)
  command = config.build_context_command
  if command is None:
    return repository.list_files()
  if not repository.is_url:
    root = repository.git_dir
    listed = subprocess.run(
      command, shell=True, cwd=root, capture_output=True, text=True, check=True
    )
  else:
    with _committed_tree(repository) as root:
      listed = subprocess.run(
        command, shell=True, cwd=root, capture_output=True, text=True, check=True
      )
  return [line for line in listed.stdout.splitlines() if line]


def _add(archive: tarfile.TarFile, name: str, content: bytes, mode: int) -> None:
  info = tarfile.TarInfo(name)
  info.size = len(content)
  info.mtime = _FIXED_MTIME
  info.mode = mode
  archive.addfile(info, io.BytesIO(content))


def _project_mode(repository: Repository, name: str) -> int:
  if not repository.is_url:
    return 0o755 if (repository.git_dir / name).stat().st_mode & 0o111 != 0 else 0o644
  assert repository.tree_ref is not None
  result = subprocess.run(
    ['git', 'ls-tree', repository.tree_ref, '--', name],
    cwd=repository.git_dir,
    capture_output=True,
    text=True,
    check=True,
  )
  mode = result.stdout.split(maxsplit=1)[0]
  return 0o755 if mode == '100755' else 0o644


def _add_directory(archive: tarfile.TarFile, name: str) -> None:
  info = tarfile.TarInfo(name)
  info.type = tarfile.DIRTYPE
  info.mtime = _FIXED_MTIME
  info.mode = 0o755
  archive.addfile(info)


def _parents(name: str) -> list[str]:
  parts = name.split('/')[:-1]
  return ['/'.join(parts[: index + 1]) for index in range(len(parts))]


def _archive(entries: dict[str, tuple[bytes, int]]) -> bytes:
  directories = {parent for name in entries for parent in _parents(name) if parent not in entries}
  buffer = io.BytesIO()
  with tarfile.open(fileobj=buffer, mode='w', format=tarfile.GNU_FORMAT) as archive:
    for name in sorted(entries.keys() | directories):
      if name in entries:
        content, mode = entries[name]
        _add(archive, name, content, mode)
      else:
        _add_directory(archive, name)
  return buffer.getvalue()


def assemble_runtime() -> bytes:
  return _archive(
    {name: (path.read_bytes(), _INJECTED_MODE) for name, path in RUNTIME_FILES.items()}
  )


def assemble_project(project: Repository | Path) -> bytes:
  """the optional dependency-bake context for a project with uv manifests."""
  repository = as_repository(project)
  manifests = manifest_paths(repository)
  if not manifests:
    raise ValueError(f'{repository.identity} has no uv manifests')
  names = project_files(repository)
  collisions = sorted(name for name in names if name.split('/')[0] == INJECTED_PREFIX)
  if collisions:
    raise ValueError(
      f'{INJECTED_PREFIX}/ is reserved for the framework build assets; '
      f'{repository.identity} carries {", ".join(collisions)}'
    )
  entries: dict[str, tuple[bytes, int]] = {}
  for name in names:
    content = repository.read_file(name)
    if content is not None:
      entries[name] = (content, _project_mode(repository, name))
  entries.update(
    {name: (path.read_bytes(), _INJECTED_MODE) for name, path in PROJECT_FILES.items()}
  )
  for name in manifests:
    content = repository.read_file(name)
    if content is None:
      raise FileNotFoundError(f'{repository.identity} is missing manifest {name}')
    entries[f'{MANIFEST_PREFIX}/{name}'] = (content, _INJECTED_MODE)
  return _archive(entries)
