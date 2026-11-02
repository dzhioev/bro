from pathlib import Path
from typing import Optional

from bro.inbox import Inbox
from bro.llm.mcp import MCPServer
from bro.llm.observer import Observer
from bro.llm.tracker import Tracker
from bro.native.loop import Loop
from bro.openai.declaration import Recipe
from bro.wire_responses.client import Responses


class ResponsesLoop(Loop):
  wire: Responses


def make_loop(
  *,
  api_key='dummy',
  inbox: Inbox,
  model=None,
  mcp_servers: Optional[list[MCPServer]] = None,
  reasoning_effort=None,
  service_tier=None,
  compact_threshold=None,
  observer: Optional[Observer] = None,
  tracker: Optional[Tracker] = None,
  agent: Optional[str] = None,
  activity_file: Optional[Path] = None,
) -> ResponsesLoop:

  recipe = Recipe(
    model=model if model is not None else 'gpt-5.6-terra',
    reasoning_effort=reasoning_effort,
    service_tier=service_tier,
    compact_threshold=compact_threshold,
  )
  wire = Responses(recipe, {'api_key': api_key})
  return ResponsesLoop(wire, inbox, mcp_servers, observer, tracker, agent, activity_file)
