import os

from bro.bro import BaseBro
from bro.launch.hold import UNATTENDED, session_hold
from bro.llm.mcp import MCPServer
from bro.registry import create_bro


def _include_raise() -> bool:
  return session_hold() == UNATTENDED and os.environ.get('RIDE_RUNNER_PID') is not None


def bro_servers(bro: BaseBro) -> list[MCPServer]:
  """assemble the tool surface for a raw Claude Code session."""
  return bro.assemble(harness='bro', wire='mcp', include_raise=_include_raise())


def persona_servers(bro: BaseBro) -> list[MCPServer]:
  """assemble additions to Claude Code's native tool surface."""
  return bro.assemble(harness='claude', wire='mcp', include_raise=_include_raise())


def resolve_bro_target(name: str) -> list[MCPServer]:
  return bro_servers(create_bro(name))


def resolve_persona_target(name: str) -> list[MCPServer]:
  return persona_servers(create_bro(name))
