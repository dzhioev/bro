import importlib.metadata
import subprocess
import sys
from pathlib import Path

import pytest

import bro.worker_types as worker_types
from bro.base.scope import split_scope_overrides
from bro.worker_types import LaunchRequest, WorkerType


class AlphaType(WorkerType):
  name = 'alpha'
  permits = frozenset({'one', 'nested.two'})

  def talk(self, request: LaunchRequest):
    return frozenset()

  def launch(self, request: LaunchRequest):
    raise NotImplementedError


class ZetaType(AlphaType):
  name = 'zeta'


class MismatchedType(AlphaType):
  name = 'other'


class NotAType:
  pass


def _entry(name: str, value: str) -> importlib.metadata.EntryPoint:
  return importlib.metadata.EntryPoint(name, value, worker_types.WORKER_TYPE_GROUP)


def test_installed_type_loads_only_the_named_entry(monkeypatch):
  alpha = _entry('alpha', f'{__name__}:AlphaType')
  unread = _entry('unread', 'does.not.exist:Type')
  monkeypatch.setattr(worker_types, '_entry_points', lambda: (unread, alpha))

  assert worker_types.installed_type('alpha') is AlphaType


def test_installed_types_are_sorted(monkeypatch):
  second = _entry('zeta', f'{__name__}:ZetaType')
  first = _entry('alpha', f'{__name__}:AlphaType')
  monkeypatch.setattr(worker_types, '_entry_points', lambda: (second, first))
  assert list(worker_types.installed_types()) == ['alpha', 'zeta']


def test_duplicate_worker_type_raises(monkeypatch):
  entry = _entry('alpha', f'{__name__}:AlphaType')
  monkeypatch.setattr(worker_types, '_entry_points', lambda: (entry, entry))
  with pytest.raises(ValueError, match='duplicate worker type'):
    worker_types.installed_types()


def test_entry_point_and_class_names_must_match(monkeypatch):
  monkeypatch.setattr(
    worker_types,
    '_entry_points',
    lambda: (_entry('alpha', f'{__name__}:MismatchedType'),),
  )
  with pytest.raises(ValueError, match='loads class named'):
    worker_types.installed_type('alpha')


def test_entry_point_must_load_a_worker_type(monkeypatch):
  monkeypatch.setattr(
    worker_types,
    '_entry_points',
    lambda: (_entry('alpha', f'{__name__}:NotAType'),),
  )
  with pytest.raises(TypeError, match='WorkerType subclass'):
    worker_types.installed_type('alpha')


@pytest.mark.parametrize('name', ['', 'Alpha', 'alpha.beta', '-alpha', 'alpha_2'])
def test_worker_type_name_grammar(name):
  with pytest.raises(ValueError, match='expected'):
    worker_types.type_name(name)


def test_unknown_type_lists_installed_names_without_loading_them(monkeypatch):
  monkeypatch.setattr(
    worker_types,
    '_entry_points',
    lambda: (
      _entry('alpha', 'does.not.exist:Type'),
      _entry('zeta', 'also.missing:Type'),
    ),
  )
  with pytest.raises(KeyError, match='installed types: alpha, zeta'):
    worker_types.installed_type('missing')


def test_permit_grammar_accepts_a_type_and_multisegment_leaf():
  assert split_scope_overrides([':alpha.nested.two']) == ([], [], ['alpha.nested.two'])


@pytest.mark.parametrize('value', [':alpha', ':Alpha.one', ':alpha..one', ':alpha.one_2'])
def test_permit_grammar_rejects_nonleaves(value):
  with pytest.raises(ValueError, match='<type>.<leaf>'):
    split_scope_overrides([value])


def test_loading_the_bro_type_does_not_import_broker_machinery():
  result = subprocess.run(
    [
      sys.executable,
      '-c',
      'import sys; from bro.worker_types import installed_type; '
      "installed_type('bro'); "
      "assert not any(name.startswith('bro.broker') for name in sys.modules)",
    ],
    capture_output=True,
    text=True,
  )
  assert result.returncode == 0, result.stderr


def test_tree_path_rejects_an_escape(tmp_path):
  with pytest.raises(ValueError, match='escapes'):
    worker_types.tree_path(tmp_path, '../outside')
  assert worker_types.tree_path(tmp_path, 'inside') == Path(tmp_path / 'inside')
