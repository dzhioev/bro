import shutil
from pathlib import Path

from ride.claude import claude_release
from ride.harness import RuntimeImage

IMAGE_DIRECTORY = Path(__file__).resolve().parent / 'image'


def claude_code_version() -> str:
  return (IMAGE_DIRECTORY / 'claude-code-version').read_text().strip()


def runtime_image() -> RuntimeImage:
  return RuntimeImage(
    dockerfile=(IMAGE_DIRECTORY / 'Dockerfile').read_text(),
    build_arguments={'CLAUDE_CODE_VERSION': claude_code_version()},
  )


def provision_bundle(root: Path, target: tuple[str, ...]) -> tuple[str, ...]:
  if target != ('linux', 'x86_64', 'glibc'):
    raise ValueError(f'unsupported Claude Code bundle target: {target}')
  binary = claude_release.cached_binary(claude_code_version(), 'linux-x64')
  carried = root / 'claude' / 'claude'
  carried.parent.mkdir()
  shutil.copyfile(binary, carried)
  carried.chmod(0o755)
  shutil.copyfile(binary.with_suffix('.sha256'), carried.with_suffix('.sha256'))
  return tuple(str(path.relative_to(root)) for path in (carried, carried.with_suffix('.sha256')))
