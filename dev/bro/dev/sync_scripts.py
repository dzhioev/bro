#!/usr/bin/env python
"""regenerate one distribution's console-script table and committed argv bridge.

The operated project is selected explicitly (the current directory by default), and
a nested project is left to its own invocation, so every project in a repository
owns independent artifacts. Python modules with a synchronous top-level ``main``
produce a canonical script name from their import path; a literal ``__cli_name__``
adds a bare alias. A CLI at a project root is refused, its import path being a
bare name that any distribution may publish.
"""

import ast
import logging
import os
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from bro.base.args import Parser, canonical_script_name
from bro.workspace.git import git_run
from bro.workspace.paths import find_project_root

__cli_name__ = 'sync-scripts'

SKIP_DIRECTORIES = {'.venv', 'build', '.claude', 'setup', '__pycache__', 'var'}


@dataclass(frozen=True)
class Project:
  directory: Path
  pyproject: Path
  bridge_module: str
  bridge_path: Path


@dataclass(frozen=True)
class Entry:
  module: str
  canonical: str
  explicit: Optional[str]
  bridge_module: str


def _bridge_module(data: dict, pyproject: Path) -> str:
  scripts = data.get('project', {}).get('scripts')
  if not isinstance(scripts, dict) or len(scripts) == 0:
    raise ValueError(f'[project.scripts] in {pyproject} must contain at least one script')
  targets: list[str] = []
  for target in scripts.values():
    if not isinstance(target, str) or ':' not in target:
      raise ValueError(f'[project.scripts] targets in {pyproject} must be module:attribute strings')
    targets.append(target)
  modules = {target.partition(':')[0] for target in targets}
  if len(modules) != 1:
    raise ValueError(f'[project.scripts] in {pyproject} must target one bridge module')
  return next(iter(modules))


def _project(path: Path) -> Project:
  directory = path.resolve()
  if directory.is_file():
    if directory.name != 'pyproject.toml':
      raise ValueError(f'project path must be a directory or pyproject.toml: {directory}')
    directory = directory.parent
  pyproject = directory / 'pyproject.toml'
  if not pyproject.is_file():
    raise ValueError(f'missing {pyproject}')
  data = tomllib.loads(pyproject.read_text())
  bridge_module = _bridge_module(data, pyproject)
  bridge_path = directory.joinpath(*bridge_module.split('.')).with_suffix('.py')
  return Project(
    directory=directory,
    pyproject=pyproject,
    bridge_module=bridge_module,
    bridge_path=bridge_path,
  )


def _ignored_directories(directory: Path) -> set[Path]:
  """the subdirectories git ignores, resolved. Empty outside a checkout, where
  SKIP_DIRECTORIES is all there is to go on."""
  if find_project_root(directory) is None:
    return set()
  listed = git_run(
    'ls-files', '--others', '--ignored', '--exclude-standard', '--directory', cwd=directory
  )
  if listed.returncode != 0:
    raise RuntimeError(f'cannot read the ignore rules for {directory}: {listed.stderr.strip()}')
  return {(directory / line).resolve() for line in listed.stdout.splitlines() if line.endswith('/')}


def _iter_python_files(project: Project):
  ignored = _ignored_directories(project.directory)
  collected: list[Path] = []
  for directory, directory_names, file_names in os.walk(project.directory):
    directory_names[:] = [
      name
      for name in directory_names
      if name not in SKIP_DIRECTORIES
      and not name.endswith('.egg-info')
      and (Path(directory) / name).resolve() not in ignored
      and not (Path(directory) / name / 'pyproject.toml').is_file()
    ]
    relative_directory = Path(directory).relative_to(project.directory)
    for file_name in file_names:
      if not file_name.endswith('.py'):
        continue
      if file_name in ('__init__.py', 'conftest.py', 'test.py') or file_name.endswith('_test.py'):
        continue
      collected.append(relative_directory / file_name)
  yield from sorted(collected)


def _module_name(relative_path: Path) -> str:
  return '.'.join(relative_path.with_suffix('').parts)


def _attribute(module: str) -> str:
  return module.replace('.', '_')


def _parse_module(path: Path) -> Optional[ast.Module]:
  try:
    return ast.parse(path.read_text(), filename=str(path))
  except SyntaxError:
    return None


def _has_sync_main(tree: ast.Module) -> bool:
  return any(isinstance(node, ast.FunctionDef) and node.name == 'main' for node in tree.body)


def _cli_name(tree: ast.Module, module_name: str) -> Optional[str]:
  for node in tree.body:
    if not isinstance(node, ast.Assign):
      continue
    if not (len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)):
      continue
    if node.targets[0].id != '__cli_name__':
      continue
    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
      return node.value.value
    raise ValueError(f'{module_name}: __cli_name__ must be a string literal')
  return None


def _discover(project: Project) -> list[Entry]:
  entries: list[Entry] = []
  for relative_path in _iter_python_files(project):
    module_name = _module_name(relative_path)
    tree = _parse_module(project.directory / relative_path)
    if tree is None or not _has_sync_main(tree):
      continue
    if '.' not in module_name:
      raise ValueError(
        f'{relative_path}: a CLI at a project root would publish its canonical name '
        f'{module_name!r} into the bare-alias namespace; move it under a package'
      )
    entries.append(
      Entry(
        module=module_name,
        canonical=canonical_script_name(module_name),
        explicit=_cli_name(tree, module_name),
        bridge_module=project.bridge_module,
      )
    )
  return entries


def _scripts_and_modules(entries: list[Entry]) -> tuple[dict[str, str], dict[str, str]]:
  scripts: dict[str, str] = {}
  name_modules: dict[str, str] = {}

  def add(name: str, entry: Entry) -> None:
    target = f'{entry.bridge_module}:{_attribute(entry.module)}'
    previous = scripts.get(name)
    if previous is not None and previous != target:
      raise ValueError(f'script name collision: {name!r} -> {previous} vs {target}')
    scripts[name] = target
    name_modules[name] = entry.module

  for entry in entries:
    add(entry.canonical, entry)
    if entry.explicit is not None and entry.explicit != entry.canonical:
      add(entry.explicit, entry)
  return scripts, name_modules


def _group_entries(
  scripts: dict[str, str], name_modules: dict[str, str]
) -> list[tuple[str, list[tuple[str, str]]]]:
  targets: dict[str, list[str]] = {}
  for name, target in scripts.items():
    targets.setdefault(target, []).append(name)
  groups: dict[str, list[tuple[str, str]]] = {}
  for target, names in targets.items():
    top_level = name_modules[names[0]].partition('.')[0]
    names.sort(key=lambda name: (len(name.split('.')) == 1, name))
    for name in names:
      groups.setdefault(top_level, []).append((name, target))
  return [(key, sorted(groups[key])) for key in sorted(groups)]


def _render_scripts(scripts: dict[str, str], name_modules: dict[str, str]) -> str:
  lines: list[str] = []
  for group, pairs in _group_entries(scripts, name_modules):
    lines.append(f'# {group}')
    for name, target in pairs:
      lines.append(f'"{name}" = "{target}"')
  return '\n'.join(lines)


def _replace_scripts(text: str, body: str) -> str:
  pattern = re.compile(r'(?ms)^(\[project\.scripts\][ \t]*\n)(.*?)(?=^\[|\Z)')
  if pattern.search(text) is None:
    raise ValueError('[project.scripts] not found')
  return pattern.sub(lambda match: match.group(1) + body + '\n\n', text)


def _render_pyproject(text: str, entries: list[Entry]) -> str:
  scripts, name_modules = _scripts_and_modules(entries)
  return _replace_scripts(text, _render_scripts(scripts, name_modules))


def _render_entrypoints(entries: list[Entry]) -> str:
  attributes: dict[str, str] = {}
  for entry in entries:
    attribute = _attribute(entry.module)
    existing = attributes.get(attribute)
    if existing is not None and existing != entry.module:
      raise ValueError(
        f'entrypoint attribute collision: {attribute!r} from {existing} and {entry.module}'
      )
    attributes[attribute] = entry.module
  lines = [
    '# generated by `sync-scripts`; do not edit.',
    '# console scripts import these zero-argument shims, which feed sys.argv to',
    "# each CLI's main(argv) -- the single place the process reads the global argv.",
    'import sys',
    '',
    'from bro.base.args import run_cli',
  ]
  for attribute in sorted(attributes):
    lines += ['', '', f'def {attribute}():', f'  return run_cli({attributes[attribute]!r}, sys.argv)']
  return '\n'.join(lines) + '\n'


def _rendered_artifacts(project: Project) -> tuple[str, str]:
  entries = _discover(project)
  return (
    _render_pyproject(project.pyproject.read_text(), entries),
    _render_entrypoints(entries),
  )


def sync_pyproject(project: Project) -> None:
  rendered, _ = _rendered_artifacts(project)
  project.pyproject.write_text(rendered)
  logging.info('updated %s', project.pyproject)


def sync_entrypoints(project: Project) -> None:
  _, rendered = _rendered_artifacts(project)
  project.bridge_path.write_text(rendered)
  logging.info('updated %s', project.bridge_path)


def check(project: Project) -> bool:
  rendered_pyproject, rendered_bridge = _rendered_artifacts(project)
  return (
    project.pyproject.read_text() == rendered_pyproject
    and project.bridge_path.is_file()
    and project.bridge_path.read_text() == rendered_bridge
  )


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description='regenerate console-script metadata and committed argv bridge')
  parser.add_argument('--project', default='.', help='project directory or pyproject.toml')
  parser.add_argument('--pyproject', action='store_true', help='rewrite [project.scripts]')
  parser.add_argument(
    '--entrypoints', action='store_true', help='rewrite the committed _entrypoints.py module'
  )
  parser.add_argument(
    '--check', action='store_true', help='verify the generated artifacts are up to date'
  )
  args = parser.parse(argv)
  project = _project(Path(args['project']))
  if args['check'] is True:
    if not check(project):
      print('console-script artifacts are stale; run `sync-scripts` and commit', file=sys.stderr)
      return 1
    return 0
  both = not args['pyproject'] and not args['entrypoints']
  if args['pyproject'] is True or both:
    sync_pyproject(project)
  if args['entrypoints'] is True or both:
    sync_entrypoints(project)
  return 0


if __name__ == '__main__':
  sys.exit(main(sys.argv))
