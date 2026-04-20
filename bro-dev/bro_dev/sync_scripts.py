#!/usr/bin/env python
"""regenerate [project.scripts] and py-modules in pyproject.toml.

scans every .py file for a callable, non-async top-level `main`. each such file
produces a canonical script entry named after its full module path with the
stem kebab-cased (e.g. flow/create_report.py -> flow.create-report). a module
may declare __cli_name__ = 'custom-name' to register an additional alias that
maps to the same module:main.
"""

import importlib
import inspect
import logging
import re
import sys
from pathlib import Path

from base.args import Parser

ROOT = Path(__file__).resolve().parent
PYPROJECT = ROOT / 'pyproject.toml'
SKIP_DIRS = {'.venv', 'build', '.claude', 'env', '__pycache__'}


def _iter_py_files():
  for path in sorted(ROOT.rglob('*.py')):
    rel = path.relative_to(ROOT)
    if any(p in SKIP_DIRS or p.endswith('.egg-info') for p in rel.parts):
      continue
    if path.name == '__init__.py':
      continue
    if path.name == 'test.py' or path.name.endswith('_test.py'):
      continue
    yield rel


def _module_name(rel: Path) -> str:
  return '.'.join(rel.with_suffix('').parts)


def _canonical(rel: Path) -> str:
  parts = list(rel.with_suffix('').parts)
  parts[-1] = parts[-1].replace('_', '-')
  return '.'.join(parts)


def _is_cli_main(mod) -> bool:
  fn = getattr(mod, 'main', None)
  return callable(fn) and not inspect.iscoroutinefunction(fn)


def _discover():
  entries = []
  top_level_modules = []
  for rel in _iter_py_files():
    mod_name = _module_name(rel)
    try:
      mod = importlib.import_module(mod_name)
    except Exception as e:
      logging.warning('cannot import %s: %s', mod_name, e)
      continue
    if len(rel.parts) == 1:
      top_level_modules.append(mod_name)
    if not _is_cli_main(mod):
      continue
    entries.append(
      {
        'module': mod_name,
        'canonical': _canonical(rel),
        'explicit': getattr(mod, '__cli_name__', None),
      }
    )
  return entries, top_level_modules


def _build_scripts(entries: list[dict]) -> dict[str, str]:
  scripts: dict[str, str] = {}

  def add(name: str, target: str) -> None:
    prev = scripts.get(name)
    if prev is not None and prev != target:
      raise ValueError(f'script name collision: {name!r} -> {prev} vs {target}')
    scripts[name] = target

  for e in entries:
    target = f'{e["module"]}:main'
    add(e['canonical'], target)
    if e['explicit'] and e['explicit'] != e['canonical']:
      add(e['explicit'], target)
  return scripts


def _group_entries(scripts: dict[str, str]) -> list[tuple[str, list[tuple[str, str]]]]:
  by_target: dict[str, list[str]] = {}
  for name, target in scripts.items():
    by_target.setdefault(target, []).append(name)
  groups: dict[str | None, list[tuple[str, str]]] = {}
  for target, names in by_target.items():
    module = target.split(':', 1)[0]
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


def _render_scripts(scripts: dict[str, str]) -> str:
  lines: list[str] = []
  for group, pairs in _group_entries(scripts):
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
  if not pat.search(text):
    raise ValueError('[project.scripts] not found')
  return pat.sub(lambda m: m.group(1) + body + '\n\n', text)


def _replace_py_modules(text: str, block: str) -> str:
  pat = re.compile(r'py-modules\s*=\s*\[[^\]]*\]')
  if not pat.search(text):
    raise ValueError('py-modules not found')
  return pat.sub(block, text)


def sync_scripts() -> None:
  entries, top_level = _discover()
  scripts = _build_scripts(entries)
  text = PYPROJECT.read_text()
  text = _replace_scripts(text, _render_scripts(scripts))
  text = _replace_py_modules(text, _render_py_modules(top_level))
  PYPROJECT.write_text(text)
  logging.info(
    'wrote %d script entries (%d modules), %d py-modules',
    len(scripts),
    len(entries),
    len(top_level),
  )


def main(argv=None):
  parser = Parser(description='regenerate [project.scripts] and py-modules in pyproject.toml')
  parser.parse(argv)
  sync_scripts()


if __name__ == '__main__':
  sys.exit(main(sys.argv))
