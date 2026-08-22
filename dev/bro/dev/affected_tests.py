"""which of a repository's test modules a change can reach, through its import graph."""

import ast
import subprocess
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Optional

SKIPPED_DIRECTORIES = frozenset({'.git', '.venv', '__pycache__', 'node_modules'})


def changed_paths(root: Path, base: str) -> list[str]:
  """every repository-relative path the working tree differs from `base` at."""

  def git(*arguments: str) -> list[str]:
    # stderr stays on the terminal so an unresolvable base names itself
    completed = subprocess.run(
      ('git', *arguments), cwd=root, check=True, stdout=subprocess.PIPE, text=True
    )
    return [line for line in completed.stdout.splitlines() if line]

  return sorted(
    {
      *git('diff', '--name-only', f'{base}...HEAD'),
      *git('diff', '--name-only', 'HEAD'),
      *git('ls-files', '--others', '--exclude-standard'),
    }
  )


def module_name(source_roots: Sequence[Path], path: Path) -> Optional[str]:
  """the import name `path` has under whichever source root holds it, if it is a module.

  Source roots nest — a workspace member sits inside the repository root — so
  the innermost one wins, which is the one whose distribution puts the file on
  the import path.
  """
  if path.suffix != '.py':
    return None
  for source_root in sorted(source_roots, key=lambda root: len(root.parts), reverse=True):
    if not path.is_relative_to(source_root):
      continue
    parts = path.relative_to(source_root).with_suffix('').parts
    if parts and parts[-1] == '__init__':
      parts = parts[:-1]
    return '.'.join(parts) or None
  return None


def import_graph(root: Path, source_roots: Sequence[Path]) -> dict[str, set[str]]:
  """module name → the modules that import it directly."""
  names = {}
  for path in root.rglob('*.py'):
    if SKIPPED_DIRECTORIES.intersection(path.parts):
      continue
    name = module_name(source_roots, path)
    if name is not None:
      names[path] = name
  known = set(names.values())

  importers: dict[str, set[str]] = {}
  for path, name in names.items():
    for imported in _imported_names(path, name):
      if imported in known and imported != name:
        importers.setdefault(imported, set()).add(name)
  return importers


def reachable(importers: dict[str, set[str]], modules: Iterable[str]) -> set[str]:
  """`modules` plus every module that transitively imports one of them."""
  seen: set[str] = set()
  frontier = list(modules)
  while frontier:
    module = frontier.pop()
    if module in seen:
      continue
    seen.add(module)
    frontier.extend(importers.get(module, set()) - seen)
  return seen


def _imported_names(path: Path, name: str) -> set[str]:
  """every module name the file names in an import, each with its parent packages.

  Importing `a.b.c` executes `a` and `a.b` too, so both are edges this file has.
  """
  tree = ast.parse(path.read_text())
  package = name.rsplit('.', 1)[0] if '.' in name else ''
  if path.name == '__init__.py':
    package = name

  imported: set[str] = set()
  for node in ast.walk(tree):
    if isinstance(node, ast.Import):
      imported.update(alias.name for alias in node.names)
    elif isinstance(node, ast.ImportFrom):
      base = _relative_base(package, node.level) if node.level else (node.module or '')
      module = f'{base}.{node.module}' if node.level and node.module else base
      imported.add(module)
      imported.update(f'{module}.{alias.name}' for alias in node.names)
  return {prefix for full in imported for prefix in _prefixes(full)}


def _relative_base(package: str, level: int) -> str:
  parts = package.split('.') if package else []
  return '.'.join(parts[: len(parts) - level + 1])


def _prefixes(name: str) -> Iterable[str]:
  parts = name.split('.')
  for index in range(1, len(parts) + 1):
    yield '.'.join(parts[:index])
