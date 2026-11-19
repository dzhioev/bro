"""A tagged git distribution for real runtime materializations."""

import contextlib
import importlib.metadata
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ride.runtime_bundle import RuntimeBundle, _manifest, _persist_bundle


@dataclass(frozen=True)
class GitBundle:
  bundle: RuntimeBundle
  source: Path
  commit: str
  version: str

  @contextlib.contextmanager
  def source_hidden(self) -> Iterator[None]:
    hidden = self.source.with_name('hidden-source')
    self.source.rename(hidden)
    try:
      yield
    finally:
      hidden.rename(self.source)


def tagged_git_bundle(root: Path) -> GitBundle:
  source = root / 'source'
  (source / 'src' / 'demo').mkdir(parents=True)
  (source / 'src' / 'demo' / '__init__.py').write_text('value = 42\n')
  (source / 'pyproject.toml').write_text("""\
[project]
name = "demo"
dynamic = ["version"]

[project.entry-points."bro.toolsets"]
demo = "demo:toolset"

[build-system]
requires = ["setuptools", "setuptools-scm"]
build-backend = "setuptools.build_meta"

[tool.setuptools_scm]
""")

  def git(*arguments: str) -> str:
    return subprocess.check_output(
      [
        'git',
        '-C',
        str(source),
        '-c',
        'user.email=probe@invalid',
        '-c',
        'user.name=probe',
        *arguments,
      ],
      text=True,
      stderr=subprocess.PIPE,
    ).strip()

  git('init', '--quiet')
  git('add', '--all')
  git('commit', '--quiet', '--message', 'tagged release')
  git('tag', 'v1.2.3')
  (source / 'src' / 'demo' / '__init__.py').write_text('value = 43\n')
  git('add', '--all')
  git('commit', '--quiet', '--message', 'after release')
  commit = git('rev-parse', 'HEAD')
  installation = root / 'installation'
  python = installation / 'bin' / 'python'
  subprocess.run(
    ['uv', 'venv', '--python', sys.executable, str(installation)], check=True, capture_output=True
  )
  pin = f'demo @ git+{source.as_uri()}@{commit}'
  subprocess.run(
    ['uv', 'pip', 'install', '--no-cache', '--python', str(python), pin],
    check=True,
    capture_output=True,
  )
  site_packages = next((installation / 'lib').glob('python*/site-packages'))
  [distribution] = importlib.metadata.distributions(path=[str(site_packages)])
  manifest = _manifest(
    f'{sys.version_info.major}.{sys.version_info.minor}', [pin], [], home='probe'
  )
  bundle = RuntimeBundle(_persist_bundle(root / 'runtime', manifest, []), manifest['python'])
  return GitBundle(bundle, source, commit, distribution.version)
