#!/usr/bin/env python
"""regenerate the console-script tables in pyproject.toml and the venv entrypoints bridge.

scans every .py file for a callable, non-async top-level `main`. each such file
produces a canonical script entry named after its full module path with the stem
kebab-cased (e.g. apps/create_report.py -> apps.create-report). a module may
declare __cli_name__ = 'custom-name' to register an additional alias.

CLIs expose a pure `def main(argv)` (no sys.argv default). a console script can't
call that directly -- the generated launcher invokes its entry point with no args
-- so each script's [project.scripts] target points at a generated `_entrypoints`
shim whose zero-arg functions feed sys.argv to the CLI's main:

    gmail = "_entrypoints:gmail"   ->   def gmail(): return _run('gmail')

`_entrypoints.py` is written into the venv's site-packages (gitignored, since all
of .venv is) and regenerated on every provision by `setup/provision_repo.sh` -- the
shared provisioner the three surfaces (setup_repo.sh, cw's host-mode worktree
launch, the container entrypoint) call. So it is NOT committed, unlike the
pyproject tables.
That shim is the single place the process reads the global argv.

modes:
  --pyproject    rewrite [project.scripts] + py-modules in pyproject.toml (committed)
  --entrypoints  write _entrypoints.py into the venv site-packages (ephemeral)
  --check        verify pyproject.toml is up to date; exit 1 if stale (no write)
  (no flags)     run --pyproject and --entrypoints

the provisioner invokes `python -m dev.sync_scripts --entrypoints` (module path, not the
`sync-scripts` console script) -- the console script routes through the bridge,
which doesn't exist yet on a fresh venv. main keeps its own `__main__` guard for
the same reason.
"""

import ast
import logging
import os
import re
import sys
import sysconfig
from pathlib import Path
from typing import Optional

from base.args import Parser
from base.project_root import PROJECT_ROOT

__cli_name__ = 'sync-scripts'

ROOT = PROJECT_ROOT
PYPROJECT = ROOT / 'pyproject.toml'
SKIP_DIRS = {'.venv', 'build', '.claude', 'setup', '__pycache__', 'var'}


def _iter_py_files():
  # os.walk with in-place pruning: rglob would physically descend into the
  # skipped trees before any filter could discard them — under the main repo
  # that means walking every worktree venv in var/ (~10s per --check)
  collected = []
  for directory, dir_names, file_names in os.walk(ROOT):
    dir_names[:] = [d for d in dir_names if d not in SKIP_DIRS and not d.endswith('.egg-info')]
    rel_dir = Path(directory).relative_to(ROOT)
    for file_name in file_names:
      if not file_name.endswith('.py'):
        continue
      if file_name == '__init__.py':
        continue
      if file_name == 'test.py' or file_name.endswith('_test.py'):
        continue
      collected.append(rel_dir / file_name)
  yield from sorted(collected)


def _module_name(rel: Path) -> str:
  return '.'.join(rel.with_suffix('').parts)


def _canonical(rel: Path) -> str:
  parts = [p.replace('_', '-') for p in rel.with_suffix('').parts]
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


def _cli_name(tree: ast.Module, mod_name: str) -> Optional[str]:
  for node in tree.body:
    if not isinstance(node, ast.Assign):
      continue
    if not (len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)):
      continue
    if node.targets[0].id != '__cli_name__':
      continue
    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
      return node.value.value
    raise ValueError(f'{mod_name}: __cli_name__ must be a string literal')
  return None


def _discover():
  entries = []
  top_level_modules = []
  for rel in _iter_py_files():
    mod_name = _module_name(rel)
    if len(rel.parts) == 1:
      top_level_modules.append(mod_name)
    tree = _parse_module(ROOT / rel)
    if tree is None or not _has_sync_main(tree):
      continue
    entries.append(
      {
        'module': mod_name,
        'canonical': _canonical(rel),
        'explicit': _cli_name(tree, mod_name),
      }
    )
  return entries, top_level_modules


def _scripts_and_modules(entries: list[dict]) -> tuple[dict[str, str], dict[str, str]]:
  """returns (name -> "_entrypoints:attr" target, name -> source module)."""
  scripts: dict[str, str] = {}
  name_module: dict[str, str] = {}

  def add(name: str, module: str) -> None:
    target = f'_entrypoints:{_attribute(module)}'
    previous = scripts.get(name)
    if previous is not None and previous != target:
      raise ValueError(f'script name collision: {name!r} -> {previous} vs {target}')
    scripts[name] = target
    name_module[name] = module

  for e in entries:
    add(e['canonical'], e['module'])
    if e['explicit'] is not None and e['explicit'] != e['canonical']:
      add(e['explicit'], e['module'])
  return scripts, name_module


def _group_entries(
  scripts: dict[str, str], name_module: dict[str, str]
) -> list[tuple[str, list[tuple[str, str]]]]:
  by_target: dict[str, list[str]] = {}
  for name, target in scripts.items():
    by_target.setdefault(target, []).append(name)
  groups: dict[Optional[str], list[tuple[str, str]]] = {}
  for target, names in by_target.items():
    module = name_module[names[0]]  # canonical + alias for a target share the module
    top = module.split('.', 1)[0] if '.' in module else None
    names.sort(key=lambda n: (len(n.split('.')) == 1, n))
    for name in names:
      groups.setdefault(top, []).append((name, target))
  ordered: list[tuple[str, list[tuple[str, str]]]] = []
  for key in sorted(k for k in groups if k is not None):
    ordered.append((key, sorted(groups[key])))
  if None in groups:
    ordered.append(('top-level', sorted(groups[None])))
  return ordered


def _render_scripts(scripts: dict[str, str], name_module: dict[str, str]) -> str:
  lines: list[str] = []
  for group, pairs in _group_entries(scripts, name_module):
    lines.append(f'# {group}')
    for name, target in pairs:
      lines.append(f'"{name}" = "{target}"')
  return '\n'.join(lines)


def _render_py_modules(modules: list[str]) -> str:
  lines = ['py-modules = [']
  for m in sorted(modules):
    lines.append(f'  "{m}",')
  lines.append(']')
  return '\n'.join(lines)


def _replace_scripts(text: str, body: str) -> str:
  pat = re.compile(r'(?ms)^(\[project\.scripts\][ \t]*\n)(.*?)(?=^\[|\Z)')
  if pat.search(text) is None:
    raise ValueError('[project.scripts] not found')
  return pat.sub(lambda m: m.group(1) + body + '\n\n', text)


def _replace_py_modules(text: str, block: str) -> str:
  pat = re.compile(r'py-modules\s*=\s*\[[^\]]*\]')
  if pat.search(text) is None:
    raise ValueError('py-modules not found')
  return pat.sub(block, text)


def _render_pyproject(text: str) -> str:
  entries, top_level = _discover()
  scripts, name_module = _scripts_and_modules(entries)
  text = _replace_scripts(text, _render_scripts(scripts, name_module))
  text = _replace_py_modules(text, _render_py_modules(top_level))
  return text


def sync_pyproject() -> None:
  PYPROJECT.write_text(_render_pyproject(PYPROJECT.read_text()))
  logging.info('updated %s', PYPROJECT.name)


def check_pyproject() -> bool:
  current = PYPROJECT.read_text()
  return _render_pyproject(current) == current


def _render_entrypoints(entries: list[dict]) -> str:
  by_attribute: dict[str, str] = {}
  for e in entries:
    attribute = _attribute(e['module'])
    existing = by_attribute.get(attribute)
    if existing is not None and existing != e['module']:
      raise ValueError(
        f'entrypoint attr collision: {attribute!r} from {existing} and {e["module"]}'
      )
    by_attribute[attribute] = e['module']
  lines = [
    '# generated by `sync-scripts --entrypoints`; do not edit.',
    '# lives in the venv site-packages (gitignored), regenerated on every venv build.',
    '# console scripts import these zero-arg shims, which feed sys.argv to each',
    "# CLI's main(argv) -- the single place the process reads the global argv.",
    'import importlib',
    'import sys',
    '',
    '',
    'def _run(module):',
    '  return importlib.import_module(module).main(sys.argv)',
  ]
  for attribute in sorted(by_attribute):
    lines += ['', '', f'def {attribute}():', f'  return _run({by_attribute[attribute]!r})']
  return '\n'.join(lines) + '\n'


def write_entrypoints() -> None:
  entries, _ = _discover()
  target = Path(sysconfig.get_paths()['purelib']) / '_entrypoints.py'
  target.write_text(_render_entrypoints(entries))
  logging.info('wrote %s (%d entrypoints)', target, len(entries))


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(
    description='regenerate the console-script tables and/or the venv entrypoints bridge'
  )
  parser.add_argument(
    '--pyproject', action='store_true', help='rewrite [project.scripts] + py-modules'
  )
  parser.add_argument(
    '--entrypoints', action='store_true', help='write _entrypoints.py into the venv site-packages'
  )
  parser.add_argument(
    '--check', action='store_true', help='verify pyproject.toml is up to date; exit 1 if stale'
  )
  args = parser.parse(argv)
  if args['check']:
    if not check_pyproject():
      print('pyproject.toml is stale; run `sync-scripts --pyproject` and commit', file=sys.stderr)
      return 1
    return 0
  both = not args['pyproject'] and not args['entrypoints']
  if args['pyproject'] or both:
    sync_pyproject()
  if args['entrypoints'] or both:
    write_entrypoints()
  return 0


if __name__ == '__main__':
  sys.exit(main(sys.argv))
