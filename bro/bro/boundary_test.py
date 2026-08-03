import ast
from pathlib import Path

from base.source_root import SOURCE_ROOT

_FRAMEWORK = 'framework'
_PPP = 'ppp'
_PATH_CLASSIFICATION = {
  Path('apps'): _PPP,
  Path('base'): _FRAMEWORK,
  Path('bro'): _FRAMEWORK,
  Path('brog'): _FRAMEWORK,
  Path('broker'): _FRAMEWORK,
  Path('cw'): _FRAMEWORK,
  Path('dev'): _FRAMEWORK,
  Path('emails'): _PPP,
  Path('extra/github'): _FRAMEWORK,
  Path('extra/google'): _PPP,
  Path('extra/notion'): _PPP,
  Path('extra/twitch'): _PPP,
  Path('extra/credentials.py'): _PPP,
  Path('flow'): _PPP,
  Path('infra'): _PPP,
  Path('llm'): _FRAMEWORK,
  Path('mac'): _PPP,
  Path('monitor'): _FRAMEWORK,
  Path('ppp_bros'): _PPP,
  Path('prompts'): _FRAMEWORK,
  Path('reference'): _FRAMEWORK,
  Path('runtime'): _FRAMEWORK,
  Path('setup'): _FRAMEWORK,
  Path('trails'): _FRAMEWORK,
  Path('workspace'): _FRAMEWORK,
}


def _module_prefix(path: Path) -> str:
  return '.'.join(path.with_suffix('').parts)


def _imports(path: Path) -> list[tuple[int, str]]:
  tree = ast.parse(path.read_text(), filename=str(path))
  imports: list[tuple[int, str]] = []
  for node in ast.walk(tree):
    if isinstance(node, ast.Import):
      imports.extend((node.lineno, alias.name) for alias in node.names)
    elif isinstance(node, ast.ImportFrom) and node.module is not None:
      if node.module == 'extra':
        imports.extend((node.lineno, f'extra.{alias.name}') for alias in node.names)
      else:
        imports.append((node.lineno, node.module))
  return imports


def _has_prefix(module: str, prefix: str) -> bool:
  return module == prefix or module.startswith(f'{prefix}.')


def test_framework_does_not_import_ppp_modules():
  ppp_prefixes = {
    _module_prefix(path)
    for path, side in _PATH_CLASSIFICATION.items()
    if side == _PPP and path.suffix != '.py'
  }
  ppp_prefixes.add('extra.credentials')
  violations: list[str] = []
  for relative_path, side in _PATH_CLASSIFICATION.items():
    if side != _FRAMEWORK:
      continue
    path = SOURCE_ROOT / relative_path
    files = path.rglob('*.py') if path.is_dir() else [path]
    for source_path in files:
      for line, module in _imports(source_path):
        if any(_has_prefix(module, prefix) for prefix in ppp_prefixes):
          shown = source_path.relative_to(SOURCE_ROOT)
          violations.append(f'{shown}:{line}: {module}')
  assert violations == []
