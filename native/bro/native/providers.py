import importlib
from collections.abc import Callable
from typing import Optional, cast

from bro.llm.llm import NativeLLMSpec
from bro.llm.mcp import MCPServer
from bro.llm.observer import Observer
from bro.llm.tracker import Tracker
from bro.native.llm import LLM

_NATIVE_PROVIDER_MODULES = {
  'openai': 'bro.native.llms.openai',
  'echo': 'bro.native.llms.echo',
}

Factory = Callable[
  [NativeLLMSpec, Optional[list[MCPServer]], Optional[Observer], Optional[Tracker], Optional[str]],
  LLM,
]


def create(
  spec: NativeLLMSpec,
  mcp_servers: Optional[list[MCPServer]] = None,
  observer: Optional[Observer] = None,
  tracker: Optional[Tracker] = None,
  agent: Optional[str] = None,
) -> LLM:
  module_name = _NATIVE_PROVIDER_MODULES.get(spec.TYPE)
  if module_name is None:
    raise ValueError(f'native provider {spec.TYPE!r} has no client')
  factory = cast(Factory, importlib.import_module(module_name).create)
  return factory(spec, mcp_servers, observer, tracker, agent)
