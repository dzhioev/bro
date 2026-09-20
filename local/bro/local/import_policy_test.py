"""Repository-owned import boundaries between framework subsystems."""

import sys
from pathlib import Path

from bro.dev.affected_tests import imported_names

_ROOT = Path(__file__).resolve().parents[3]
_BROKER = _ROOT / 'bro' / 'broker'


def _broker_import_violations(package: Path) -> dict[str, list[str]]:
  violations: dict[str, list[str]] = {}
  for path in package.rglob('*.py'):
    relative = path.relative_to(package).with_suffix('')
    parts = relative.parts[:-1] if relative.name == '__init__' else relative.parts
    module = '.'.join(('bro', 'broker', *parts))
    test_module = path.name.endswith(('_test.py', '_test_helper.py'))
    forbidden = sorted(
      name
      for name in imported_names(path, module)
      if name != 'bro'
      and not name.startswith(('bro.base', 'bro.broker'))
      and name.split('.', 1)[0] not in sys.stdlib_module_names
      and not (test_module and name == 'pytest')
    )
    if forbidden:
      violations[str(path.relative_to(package))] = forbidden
  return violations


def test_broker_imports_only_its_lower_layers():
  assert _broker_import_violations(_BROKER) == {}


def test_broker_import_policy_catches_a_deliberate_violation(tmp_path):
  package = tmp_path / 'bro' / 'broker'
  package.mkdir(parents=True)
  (package / 'bad.py').write_text('from ride import session\n')

  assert _broker_import_violations(package) == {'bad.py': ['ride', 'ride.session']}
