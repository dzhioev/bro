import importlib.metadata
from typing import cast

import pytest

from bro.kinds import ArtifactResolver, KindContext
from ride import kinds

_built: list[KindContext] = []

NOT_CALLABLE = object()


def sample_kind(context: KindContext):
  _built.append(context)

  def handler(context, peer, message):
    del context, peer, message

  return handler


def _entry_point(name: str, value: str) -> importlib.metadata.EntryPoint:
  return importlib.metadata.EntryPoint(name=name, value=value, group=kinds.KIND_GROUP)


def _context(tmp_path) -> KindContext:
  return KindContext(workspace_tree=tmp_path, artifacts=cast(ArtifactResolver, object()))


class TestExtensionKinds:
  def test_contributed_kind_is_built_with_the_context(self, monkeypatch, tmp_path):
    monkeypatch.setattr(
      kinds,
      '_kind_entry_points',
      lambda: (_entry_point('sample', 'ride.kinds_test:sample_kind'),),
    )
    _built.clear()
    context = _context(tmp_path)
    loaded = kinds.extension_kinds(context)
    assert list(loaded) == ['sample']
    assert callable(loaded['sample'])
    assert _built == [context]
    assert _built[0].workspace_tree == tmp_path

  def test_duplicate_kind_rejected(self, monkeypatch, tmp_path):
    entry = _entry_point('sample', 'ride.kinds_test:sample_kind')
    monkeypatch.setattr(kinds, '_kind_entry_points', lambda: (entry, entry))
    with pytest.raises(ValueError, match="duplicate broker kind 'sample'"):
      kinds.extension_kinds(_context(tmp_path))

  def test_non_callable_target_rejected(self, monkeypatch, tmp_path):
    monkeypatch.setattr(
      kinds,
      '_kind_entry_points',
      lambda: (_entry_point('sample', 'ride.kinds_test:NOT_CALLABLE'),),
    )
    with pytest.raises(TypeError, match='must load a callable'):
      kinds.extension_kinds(_context(tmp_path))

  def test_entry_points_use_the_expected_group(self, monkeypatch):
    calls = []

    def entry_points(**kwargs):
      calls.append(kwargs)
      return ()

    monkeypatch.setattr(importlib.metadata, 'entry_points', entry_points)
    assert kinds._kind_entry_points() == ()
    assert calls == [{'group': 'bro.broker_kinds'}]
