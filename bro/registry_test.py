import importlib.metadata
from dataclasses import dataclass
from typing import ClassVar, Optional

import pytest

import bro.registry
from bro.llm.llm import LLM, LLMSpec
from bro.llm.mcp import MCPServer
from bro.registry import _REGISTRY, create_bro, get_class, list_classes, register
from bros.bro import Bro


class MockLLM(LLM):
  def __init__(self, mcp_servers: Optional[list[MCPServer]] = None):
    super().__init__(mcp_servers)

  async def send(self, messages: list[dict], *, request_timeout: Optional[float] = None) -> str:
    return ''


@dataclass(frozen=True)
class _EchoOnlySpec(LLMSpec):
  TYPE: ClassVar[str] = 'mock'

  model: str = 'mock'

  def create_llm(self, mcp_servers=None, observer=None, tracker=None, agent=None) -> LLM:
    return MockLLM(mcp_servers)

  def dump(self) -> dict:
    return {'type': self.TYPE, 'model': self.model}

  @classmethod
  def _from_dict_impl(cls, data: dict) -> 'LLMSpec':
    return cls(model=data['model'])


class AlphaBro(Bro):
  name = 'alpha'
  description = 'alpha bro'

  def __init__(self):
    super().__init__(system_prompt='alpha')

  def _create_llm(self, *, hold: str):
    return MockLLM()


class BetaBro(Bro):
  name = 'beta'
  description = 'beta bro'

  def __init__(self):
    super().__init__(system_prompt='beta')

  def _create_llm(self, *, hold: str):
    return MockLLM()


class ExternalBro(Bro):
  name = 'external'
  description = 'external bro'

  def __init__(self):
    super().__init__(system_prompt='external')

  def _create_llm(self, *, hold: str):
    return MockLLM()


@pytest.fixture(autouse=True)
def clean_registry():
  # isolate to hand-registered bros: disable autoload so the real bros from
  # BRO_SPECS never bleed into get_class / list_classes during the test.
  saved = dict(_REGISTRY)
  saved_autoload = bro.registry._autoload
  _REGISTRY.clear()
  bro.registry._autoload = False
  yield
  _REGISTRY.clear()
  _REGISTRY.update(saved)
  bro.registry._autoload = saved_autoload


class TestRegister:
  def test_register_and_create_returns_instance_of_class(self):
    register(AlphaBro)
    bro = create_bro('alpha')
    assert bro.name == 'alpha'
    assert isinstance(bro, AlphaBro)

  def test_create_returns_fresh_instance_each_call(self):
    register(AlphaBro)
    a = create_bro('alpha')
    b = create_bro('alpha')
    assert a is not b

  def test_create_with_llm_spec_routes_through_create_factory(self):
    register(AlphaBro)
    spec = _EchoOnlySpec(model='custom-model')
    bro = create_bro('alpha', llm_spec=spec)
    assert bro.llm_spec is spec

  def test_get_class_returns_the_registered_class(self):
    register(AlphaBro)
    assert get_class('alpha') is AlphaBro

  def test_duplicate_raises(self):
    register(AlphaBro)
    with pytest.raises(ValueError, match='duplicate bro name'):
      register(AlphaBro)

  def test_create_unknown_raises(self):
    with pytest.raises(KeyError, match='unknown bro'):
      create_bro('nonexistent')

  def test_get_class_unknown_raises(self):
    with pytest.raises(KeyError, match='unknown bro'):
      get_class('nonexistent')


class TestListClasses:
  def test_list_empty(self):
    assert list_classes() == []

  def test_list_returns_classes_not_instances(self):
    register(AlphaBro)
    register(BetaBro)
    classes = list_classes()
    assert {cls.name for cls in classes} == {'alpha', 'beta'}
    assert all(isinstance(cls, type) for cls in classes)


class TestAutoload:
  # the autouse fixture disables autoload; re-enable it here to exercise the
  # real lazy-import path against BRO_SPECS.
  @pytest.fixture(autouse=True)
  def enable_autoload(self, monkeypatch):
    bro.registry._autoload = True
    monkeypatch.setattr(bro.registry, '_entry_points', lambda: ())

  def test_get_class_imports_bro_by_name_without_manual_register(self):
    cls = get_class('dev')
    assert cls.name == 'dev'

  def test_unknown_name_raises_even_with_autoload(self):
    with pytest.raises(KeyError, match='unknown bro'):
      get_class('nonexistent')

  def test_list_classes_autoloads_every_bro_spec(self):
    from bros import BRO_SPECS

    assert {cls.name for cls in list_classes()} == set(BRO_SPECS)

  def test_autoload_off_does_not_import_real_bros(self):
    bro.registry._autoload = False
    with pytest.raises(KeyError, match='unknown bro'):
      get_class('dev')


def _entry_point(name: str, value: str) -> importlib.metadata.EntryPoint:
  return importlib.metadata.EntryPoint(name, value, bro.registry._ENTRY_POINT_GROUP)


class TestExternalSpecs:
  # the autouse fixture disables autoload; the external entry-point path only
  # exists under autoload, so re-enable it and fake the installed entry points.
  @pytest.fixture(autouse=True)
  def enable_autoload(self):
    bro.registry._autoload = True

  def test_entry_points_use_the_bro_group(self, monkeypatch):
    calls: list[dict] = []

    def entry_points(**kwargs):
      calls.append(kwargs)
      return ()

    monkeypatch.setattr(importlib.metadata, 'entry_points', entry_points)
    assert bro.registry._entry_points() == ()
    assert calls == [{'group': 'bro'}]

  def test_get_class_resolves_an_external_entry_point(self, monkeypatch):
    monkeypatch.setattr(
      bro.registry,
      '_entry_points',
      lambda: (_entry_point('external', 'bro.registry_test:ExternalBro'),),
    )
    assert get_class('external') is ExternalBro
    assert create_bro('external').name == 'external'

  def test_external_name_mismatch_raises(self, monkeypatch):
    monkeypatch.setattr(
      bro.registry,
      '_entry_points',
      lambda: (_entry_point('mismatched', 'bro.registry_test:ExternalBro'),),
    )
    with pytest.raises(ValueError, match="declares name 'external'"):
      get_class('mismatched')

  def test_external_shadowing_a_builtin_raises(self, monkeypatch):
    monkeypatch.setattr(
      bro.registry,
      '_entry_points',
      lambda: (_entry_point('dev', 'bro.registry_test:ExternalBro'),),
    )
    with pytest.raises(ValueError, match='shadows a built-in bro'):
      bro.registry.known_names()

  def test_duplicate_externals_raise(self, monkeypatch):
    monkeypatch.setattr(
      bro.registry,
      '_entry_points',
      lambda: (
        _entry_point('external', 'bro.registry_test:ExternalBro'),
        _entry_point('external', 'other.module:Other'),
      ),
    )
    with pytest.raises(ValueError, match='duplicate external bro'):
      bro.registry.known_names()

  def test_known_names_unions_builtins_and_externals(self, monkeypatch):
    from bros import BRO_SPECS

    monkeypatch.setattr(
      bro.registry,
      '_entry_points',
      lambda: (_entry_point('external', 'bro.registry_test:ExternalBro'),),
    )
    assert bro.registry.known_names() == set(BRO_SPECS) | {'external'}

  def test_known_names_with_autoload_off_sees_only_hand_registered(self, monkeypatch):
    def unexpected_read():
      raise AssertionError('entry points must not be consulted with autoload off')

    monkeypatch.setattr(bro.registry, '_entry_points', unexpected_read)
    bro.registry._autoload = False
    register(AlphaBro)
    assert bro.registry.known_names() == {'alpha'}

  def test_list_classes_includes_externals(self, monkeypatch):
    from bros import BRO_SPECS

    monkeypatch.setattr(
      bro.registry,
      '_entry_points',
      lambda: (_entry_point('external', 'bro.registry_test:ExternalBro'),),
    )
    assert {cls.name for cls in list_classes()} == set(BRO_SPECS) | {'external'}
