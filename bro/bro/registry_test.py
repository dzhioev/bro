from dataclasses import dataclass
from typing import ClassVar

import pytest

from bro.bros.bro import Bro
import bro.registry
from bro.registry import _REGISTRY, register, create_bro, get_class, list_classes
from llm.llm import LLM, LLMSpec
from llm.mcp import MCPServer


class MockLLM(LLM):
  def __init__(self, mcp_servers: list[MCPServer] | None = None):
    super().__init__(mcp_servers)

  async def send(self, messages: list[dict], *, request_timeout: float | None = None) -> str:
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
  saved = dict(_REGISTRY)
  saved_initialized = bro.registry._initialized
  _REGISTRY.clear()
  bro.registry._initialized = True
  yield
  _REGISTRY.clear()
  _REGISTRY.update(saved)
  bro.registry._initialized = saved_initialized


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
