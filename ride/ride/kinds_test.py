import importlib.metadata
from pathlib import Path

import pytest

from ride import kinds

_built: list[Path] = []

NOT_CALLABLE = object()


def sample_kind(workspace_tree: Path):
  _built.append(workspace_tree)

  def handler(context, peer, message):
    del context, peer, message

  return handler


def _entry_point(name: str, value: str) -> importlib.metadata.EntryPoint:
  return importlib.metadata.EntryPoint(name=name, value=value, group=kinds.KIND_GROUP)


class TestExtensionKinds:
  def test_contributed_kind_is_built_with_the_workspace_tree(self, monkeypatch, tmp_path):
    monkeypatch.setattr(
      kinds,
      '_kind_entry_points',
      lambda: (_entry_point('sample', 'ride.kinds_test:sample_kind'),),
    )
    _built.clear()
    loaded = kinds.extension_kinds(tmp_path)
    assert list(loaded) == ['sample']
    assert callable(loaded['sample'])
    assert _built == [tmp_path]

  def test_duplicate_kind_rejected(self, monkeypatch, tmp_path):
    entry = _entry_point('sample', 'ride.kinds_test:sample_kind')
    monkeypatch.setattr(kinds, '_kind_entry_points', lambda: (entry, entry))
    with pytest.raises(ValueError, match="duplicate broker kind 'sample'"):
      kinds.extension_kinds(tmp_path)

  def test_non_callable_target_rejected(self, monkeypatch, tmp_path):
    monkeypatch.setattr(
      kinds,
      '_kind_entry_points',
      lambda: (_entry_point('sample', 'ride.kinds_test:NOT_CALLABLE'),),
    )
    with pytest.raises(TypeError, match='must load a callable'):
      kinds.extension_kinds(tmp_path)

  def test_entry_points_use_the_expected_group(self, monkeypatch):
    calls = []

    def entry_points(**kwargs):
      calls.append(kwargs)
      return ()

    monkeypatch.setattr(importlib.metadata, 'entry_points', entry_points)
    assert kinds._kind_entry_points() == ()
    assert calls == [{'group': 'bro.broker_kinds'}]
