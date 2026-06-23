from dataclasses import dataclass
from typing import ClassVar, Optional

import pytest

import bro.registry
from bro.bros.bro import Bro
from bro.registry import _REGISTRY, create_bro, get_class, list_classes, register
from llm.llm import LLM, LLMSpec
from llm.mcp import MCPServer


class MockLLM(LLM):
  def __init__(self, mcp_servers: Optional[list[MCPServer]] = None):
    super().__init__(mcp_servers)

  async def send(self, messages: list[dict], *, request_timeout: Optional[float] = None) -> str:
    return ''


@dataclass(frozen=True)
class _EchoOnlySpec(LLMSpec):
  TYPE: ClassVar[str] = 'mock'

  model: str = 'mock'

  def create_llm(self, mcp_servers=None, observer=None, tracker=None) -> LLM:
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

  def _create_llm(self, *, interactive: bool):
    return MockLLM()


class BetaBro(Bro):
  name = 'beta'
  description = 'beta bro'

  def __init__(self):
    super().__init__(system_prompt='beta')

  def _create_llm(self, *, interactive: bool):
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
  def enable_autoload(self):
    bro.registry._autoload = True

  def test_get_class_imports_bro_by_name_without_manual_register(self):
    cls = get_class('pm')
    assert cls.name == 'pm'

  def test_unknown_name_raises_even_with_autoload(self):
    with pytest.raises(KeyError, match='unknown bro'):
      get_class('nonexistent')

  def test_list_classes_autoloads_every_bro_spec(self):
    from bro.bros import BRO_SPECS

    assert {cls.name for cls in list_classes()} == set(BRO_SPECS)

  def test_autoload_off_does_not_import_real_bros(self):
    bro.registry._autoload = False
    with pytest.raises(KeyError, match='unknown bro'):
      get_class('pm')
