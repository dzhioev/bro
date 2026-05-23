import pytest

from bro.bro import Bro
import bro.registry
from bro.registry import _REGISTRY, register, get_bro, list_bros
from llm.llm import LLM
from llm.mcp import MCPServer


class MockLLM(LLM):
  def __init__(self, mcp_servers: list[MCPServer] | None = None):
    super().__init__(mcp_servers)

  async def send(self, messages: list[dict]) -> str:
    return ''


class AlphaBro(Bro):
  name = 'alpha'
  description = 'alpha bro'

  def __init__(self, mcp_servers=None, data_sources=None):
    super().__init__(system_prompt='alpha', mcp_servers=mcp_servers, data_sources=data_sources)

  def _create_llm(self):
    return MockLLM()


class BetaBro(Bro):
  name = 'beta'
  description = 'beta bro'

  def __init__(self, mcp_servers=None, data_sources=None):
    super().__init__(system_prompt='beta', mcp_servers=mcp_servers, data_sources=data_sources)

  def _create_llm(self):
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
  def test_register_and_get(self):
    register(AlphaBro)
    bro = get_bro('alpha')
    assert bro.name == 'alpha'
    assert isinstance(bro, AlphaBro)

  def test_duplicate_raises(self):
    register(AlphaBro)
    with pytest.raises(ValueError, match='duplicate bro name'):
      register(AlphaBro)

  def test_get_unknown_raises(self):
    with pytest.raises(KeyError, match='unknown bro'):
      get_bro('nonexistent')


class TestListBros:
  def test_list_empty(self):
    assert list_bros() == []

  def test_list_registered(self):
    register(AlphaBro)
    register(BetaBro)
    bros = list_bros()
    names = {b.name for b in bros}
    assert names == {'alpha', 'beta'}
