#!/usr/bin/env python
"""regenerate the console-script table and committed argv bridges.

Scans every Python module in the current three-source layout for a callable,
non-async top-level `main`. Each such file produces a canonical script name from
its import path with the stem kebab-cased (for example,
`bro/bro/cw/cli.py` -> `bro.cw.cli`). A module may declare
`__cli_name__ = 'custom-name'` to register an additional bare alias.

Console-script launchers invoke their target with no arguments, while repo CLIs
expose pure `main(argv)` functions. Each distribution therefore carries a
committed `_entrypoints.py` bridge whose zero-argument functions pass `sys.argv`
to the selected module. `sync-scripts` updates those bridges together with the
root `[project.scripts]` table; `--check` verifies both artifacts.
"""

import ast
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from bro.base.args import Parser

__cli_name__ = 'sync-scripts'

SKIP_DIRS = {'.venv', 'build', '.claude', 'setup', '__pycache__', 'var'}


@dataclass(frozen=True)
class Source:
  directory: Path
  bridge_module: str
  bridge_path: Path
  excluded_directories: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Entry:
  module: str
  canonical: str
  explicit: Optional[str]
  bridge_module: str


def _project_root() -> Path:
  result = subprocess.run(
    ['git', 'rev-parse', '--show-toplevel'],
    capture_output=True,
    text=True,
    check=True,
  )
  return Path(result.stdout.strip())


def _sources(root: Path) -> tuple[Source, ...]:
  return (
    Source(
      root,
      '_entrypoints',
      root / '_entrypoints.py',
      excluded_directories=frozenset({'bro', 'bro-dev'}),
    ),
    Source(root / 'bro', 'bro._entrypoints', root / 'bro' / 'bro' / '_entrypoints.py'),
    Source(
      root / 'bro-dev',
      'bro_dev._entrypoints',
      root / 'bro-dev' / 'bro_dev' / '_entrypoints.py',
    ),
  )


def _iter_py_files(source: Source):
  # os.walk with in-place pruning avoids descending into every worktree venv under var/.
  collected: list[Path] = []
  for directory, directory_names, file_names in os.walk(source.directory):
    directory_names[:] = [
      name
      for name in directory_names
      if name not in SKIP_DIRS
      and name not in source.excluded_directories
      and not name.endswith('.egg-info')
    ]
    relative_directory = Path(directory).relative_to(source.directory)
    for file_name in file_names:
      if not file_name.endswith('.py'):
        continue
      if file_name == '__init__.py' or file_name == 'test.py' or file_name.endswith('_test.py'):
        continue
      collected.append(relative_directory / file_name)
  yield from sorted(collected)


def _module_name(relative_path: Path) -> str:
  return '.'.join(relative_path.with_suffix('').parts)


def _canonical(relative_path: Path) -> str:
  parts = [part.replace('_', '-') for part in relative_path.with_suffix('').parts]
  return '.'.join(parts)


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


def _discover(root: Path) -> tuple[list[Entry], list[str]]:
  entries: list[Entry] = []
  top_level_modules: list[str] = []
  for source in _sources(root):
    for relative_path in _iter_py_files(source):
      module_name = _module_name(relative_path)
      if source.directory == root and len(relative_path.parts) == 1:
        top_level_modules.append(module_name)
      tree = _parse_module(source.directory / relative_path)
      if tree is None or not _has_sync_main(tree):
        continue
      entries.append(
        Entry(
          module=module_name,
          canonical=_canonical(relative_path),
          explicit=_cli_name(tree, module_name),
          bridge_module=source.bridge_module,
        )
      )
  return entries, top_level_modules


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
  groups: dict[Optional[str], list[tuple[str, str]]] = {}
  for target, names in targets.items():
    module = name_modules[names[0]]
    top_level = module.split('.', 1)[0] if '.' in module else None
    names.sort(key=lambda name: (len(name.split('.')) == 1, name))
    for name in names:
      groups.setdefault(top_level, []).append((name, target))
  ordered: list[tuple[str, list[tuple[str, str]]]] = []
  for key in sorted(key for key in groups if key is not None):
    ordered.append((key, sorted(groups[key])))
  if None in groups:
    ordered.append(('top-level', sorted(groups[None])))
  return ordered


def _render_scripts(scripts: dict[str, str], name_modules: dict[str, str]) -> str:
  lines: list[str] = []
  for group, pairs in _group_entries(scripts, name_modules):
    lines.append(f'# {group}')
    for name, target in pairs:
      lines.append(f'"{name}" = "{target}"')
  return '\n'.join(lines)


def _render_py_modules(modules: list[str]) -> str:
  lines = ['py-modules = [']
  for module in sorted(modules):
    lines.append(f'  "{module}",')
  lines.append(']')
  return '\n'.join(lines)


def _replace_scripts(text: str, body: str) -> str:
  pattern = re.compile(r'(?ms)^(\[project\.scripts\][ \t]*\n)(.*?)(?=^\[|\Z)')
  if pattern.search(text) is None:
    raise ValueError('[project.scripts] not found')
  return pattern.sub(lambda match: match.group(1) + body + '\n\n', text)


def _replace_py_modules(text: str, block: str) -> str:
  pattern = re.compile(r'py-modules\s*=\s*\[[^\]]*\]')
  if pattern.search(text) is None:
    raise ValueError('py-modules not found')
  return pattern.sub(block, text)


def _render_pyproject(root: Path, text: str, entries: list[Entry], modules: list[str]) -> str:
  del root
  scripts, name_modules = _scripts_and_modules(entries)
  text = _replace_scripts(text, _render_scripts(scripts, name_modules))
  return _replace_py_modules(text, _render_py_modules(modules))


def _render_entrypoints(entries: list[Entry], bridge_module: str) -> str:
  attributes: dict[str, str] = {}
  for entry in entries:
    if entry.bridge_module != bridge_module:
      continue
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
    'import importlib',
    'import sys',
    '',
    '',
    'def _run(module):',
    '  return importlib.import_module(module).main(sys.argv)',
  ]
  for attribute in sorted(attributes):
    lines += ['', '', f'def {attribute}():', f'  return _run({attributes[attribute]!r})']
  return '\n'.join(lines) + '\n'


def _rendered_artifacts(root: Path) -> tuple[str, dict[Path, str]]:
  entries, modules = _discover(root)
  pyproject = root / 'pyproject.toml'
  rendered_pyproject = _render_pyproject(root, pyproject.read_text(), entries, modules)
  bridges = {
    source.bridge_path: _render_entrypoints(entries, source.bridge_module)
    for source in _sources(root)
  }
  return rendered_pyproject, bridges


def sync_pyproject(root: Path) -> None:
  rendered, _ = _rendered_artifacts(root)
  path = root / 'pyproject.toml'
  path.write_text(rendered)
  logging.info('updated %s', path)


def sync_entrypoints(root: Path) -> None:
  _, bridges = _rendered_artifacts(root)
  for path, rendered in bridges.items():
    path.write_text(rendered)
    logging.info('updated %s', path)


def check(root: Path) -> bool:
  rendered_pyproject, bridges = _rendered_artifacts(root)
  pyproject_matches = (root / 'pyproject.toml').read_text() == rendered_pyproject
  bridges_match = all(
    path.is_file() and path.read_text() == rendered for path, rendered in bridges.items()
  )
  return pyproject_matches and bridges_match


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description='regenerate console-script metadata and committed argv bridges')
  parser.add_argument(
    '--pyproject', action='store_true', help='rewrite [project.scripts] and py-modules'
  )
  parser.add_argument(
    '--entrypoints', action='store_true', help='rewrite the committed _entrypoints.py modules'
  )
  parser.add_argument(
    '--check', action='store_true', help='verify all generated artifacts are up to date'
  )
  args = parser.parse(argv)
  root = _project_root()
  if args['check'] is True:
    if not check(root):
      print('console-script artifacts are stale; run `sync-scripts` and commit', file=sys.stderr)
      return 1
    return 0
  both = not args['pyproject'] and not args['entrypoints']
  if args['pyproject'] is True or both:
    sync_pyproject(root)
  if args['entrypoints'] is True or both:
    sync_entrypoints(root)
  return 0


if __name__ == '__main__':
  sys.exit(main(sys.argv))
