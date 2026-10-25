import copy

import pytest

from bro.files import confinement
from bro.files.confinement import classification
from bro.files.confinement.tables import TABLES


def test_every_pinned_syscall_has_a_reviewed_decision():
  classification.validate_tables(TABLES)
  assert set(classification.CLASSIFICATION) == set().union(
    *(set(table) for table in TABLES.values())
  )


def test_a_new_syscall_requires_a_decision():
  tables = copy.deepcopy(TABLES)
  tables['x86_64']['future_unreviewed_call'] = 9999
  with pytest.raises(ValueError, match='unclassified.*future_unreviewed_call'):
    classification.validate_tables(tables)


def test_a_missing_denied_call_requires_an_explicit_absence():
  tables = copy.deepcopy(TABLES)
  del tables['aarch64']['socket']
  with pytest.raises(ValueError, match='absent list'):
    classification.validate_tables(tables)


def report():
  return {
    'backend': 'linux',
    'arch': next(iter(confinement.SUPPORTED_ARCHITECTURES)),
    'kernel_abi': 6,
    'landlock_abi': 6,
    'landlock': ['fs', 'tcp', 'scope'],
    'seccomp': True,
    'rlimits': ['as', 'cpu', 'fsize', 'nofile', 'core'],
  }


@pytest.mark.parametrize('protection', sorted(confinement.REQUIRED_PROTECTIONS))
def test_a_missing_required_protection_fails_closed(protection):
  value = report()
  if protection == 'seccomp':
    value['seccomp'] = False
  else:
    field, item = protection.split('.')
    value[field].remove(item)
  with pytest.raises(RuntimeError, match='file sandbox unavailable: missing'):
    confinement.validate_report(value)


@pytest.mark.parametrize('field', ['tcp', 'scope'])
def test_optional_abi_protections_are_required_when_the_kernel_has_them(field):
  value = report()
  value['landlock'].remove(field)
  with pytest.raises(RuntimeError, match=f'missing landlock.{field}'):
    confinement.validate_report(value)


@pytest.mark.parametrize('kernel_abi', range(1, 10))
def test_reports_use_the_highest_implemented_abi_available(kernel_abi):
  value = report()
  value['kernel_abi'] = kernel_abi
  value['landlock_abi'] = min(kernel_abi, confinement.MAXIMUM_LANDLOCK_ABI)
  confinement.validate_report(value)
  value['landlock_abi'] = 0
  with pytest.raises(RuntimeError, match='missing Landlock ABI'):
    confinement.validate_report(value)


@pytest.mark.parametrize(
  'patch',
  [
    {'seccomp': 1},
    {'landlock': 'fs'},
    {'rlimits': [None]},
    {'kernel_abi': True},
    {'landlock_abi': 1},
    {'backend': 'other'},
    {'arch': 'unknown'},
  ],
)
def test_invalid_or_weakened_reports_fail_closed(patch):
  with pytest.raises(RuntimeError, match='file sandbox unavailable: missing'):
    confinement.validate_report({**report(), **patch})


def test_a_platform_without_a_backend_refuses_to_start(monkeypatch):
  monkeypatch.setattr(confinement.sys, 'platform', 'darwin')
  with pytest.raises(RuntimeError, match='no confinement backend for darwin'):
    confinement.apply(confinement.SandboxLimits())


def test_an_unknown_uname_architecture_refuses_to_start(monkeypatch):
  monkeypatch.setattr(confinement, 'SUPPORTED_ARCHITECTURES', frozenset())
  with pytest.raises(RuntimeError, match='no confinement backend for'):
    confinement.architecture()


@pytest.mark.parametrize(
  'bounds',
  [
    {'cpu_seconds': 0},
    {'address_space_bytes': -1},
    {'open_files': 1.5},
    {'wall_seconds': float('nan')},
    {'wall_seconds': float('inf')},
  ],
)
def test_invalid_bounds_are_rejected_before_confinement(bounds):
  with pytest.raises(ValueError):
    confinement.SandboxLimits(**bounds)
