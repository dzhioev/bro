"""Normalized tar contexts for the runtime container image."""

import io
import tarfile
from pathlib import Path

from bro.harness import installed_harness_names
from bro.shell import shell_dir
from ride.harness import get_harness
from ride.provisioning import relative_file

SETUP_DIR = Path(__file__).resolve().parent.parent / 'setup'
CONTAINER_DIR = SETUP_DIR / 'container'
SHELL_DIR = shell_dir()
UV_VERSION_FILE = SHELL_DIR / 'uv-version'
SHELL_HELPERS = ('prelude.sh', 'log.sh', 'strict.sh', 'install_awscli.sh')

INJECTED_PREFIX = '.bro-container'
DOCKERFILE_PATH = f'{INJECTED_PREFIX}/Dockerfile'

RUNTIME_FILES = {
  DOCKERFILE_PATH: CONTAINER_DIR / 'Dockerfile',
  f'{INJECTED_PREFIX}/entrypoint.sh': CONTAINER_DIR / 'entrypoint.sh',
  **{f'{INJECTED_PREFIX}/{name}': SHELL_DIR / name for name in SHELL_HELPERS},
}

_FIXED_MTIME = 0
_INJECTED_MODE = 0o644


def _add(archive: tarfile.TarFile, name: str, content: bytes, mode: int) -> None:
  info = tarfile.TarInfo(name)
  info.size = len(content)
  info.mtime = _FIXED_MTIME
  info.mode = mode
  archive.addfile(info, io.BytesIO(content))


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


def runtime_inputs() -> tuple[dict[str, tuple[bytes, int]], dict[str, str]]:
  entries = {name: (path.read_bytes(), _INJECTED_MODE) for name, path in RUNTIME_FILES.items()}
  arguments = {}
  dockerfile = entries[DOCKERFILE_PATH][0].decode()
  for name in installed_harness_names():
    image = get_harness(name).runtime_image()
    dockerfile += f'\n# harness: {name}\n{image.dockerfile}\n'
    for relative, content in image.files.items():
      relative_file(relative)
      entries[f'{INJECTED_PREFIX}/harnesses/{name}/{relative}'] = (content, _INJECTED_MODE)
    for key, value in image.build_arguments.items():
      if key in arguments or key in ('PYTHON_VERSION', 'UV_VERSION') or key.startswith('IMAGE_'):
        raise ValueError(f'harness image build argument claimed twice: {key}')
      arguments[key] = value
  entries[DOCKERFILE_PATH] = (dockerfile.encode(), _INJECTED_MODE)
  return entries, arguments


def assemble_runtime(entries: dict[str, tuple[bytes, int]] | None = None) -> bytes:
  if entries is None:
    entries, _arguments = runtime_inputs()
  return _archive(entries)
