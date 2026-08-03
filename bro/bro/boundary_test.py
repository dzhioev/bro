import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FRAMEWORK_PATHS = (
  _REPO_ROOT / 'bro' / 'bro',
  _REPO_ROOT / 'bro-dev' / 'bro_dev',
)
_PPP_MODULE_PREFIXES = ('apps', 'dev', 'emails', 'extra', 'flow', 'infra', 'mac', 'ppp_bros')


def _imports(path: Path) -> list[tuple[int, str]]:
  tree = ast.parse(path.read_text(), filename=str(path))
  imports: list[tuple[int, str]] = []
  for node in ast.walk(tree):
    if isinstance(node, ast.Import):
      imports.extend((node.lineno, alias.name) for alias in node.names)
    elif isinstance(node, ast.ImportFrom) and node.module is not None:
      imports.append((node.lineno, node.module))
  return imports


def _has_prefix(module: str, prefix: str) -> bool:
  return module == prefix or module.startswith(f'{prefix}.')


def test_framework_does_not_import_ppp_modules():
  violations: list[str] = []
  for framework_path in _FRAMEWORK_PATHS:
    for source_path in framework_path.rglob('*.py'):
      for line, module in _imports(source_path):
        if any(_has_prefix(module, prefix) for prefix in _PPP_MODULE_PREFIXES):
          shown = source_path.relative_to(_REPO_ROOT)
          violations.append(f'{shown}:{line}: {module}')
  assert violations == []
