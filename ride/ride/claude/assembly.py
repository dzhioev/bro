from bro.bro import BaseBro
from bro.launch.hold import HOLD_VARIABLE, session_hold
from bro.llm.mcp import MCPServer
from bro.registry import create_bro
from ride.claude.harness import CLAUDE


def persona_servers(bro: BaseBro, hold: str) -> list[MCPServer]:
  """assemble additions to Claude Code's native tool surface for a session under
  `hold`."""
  return bro.assemble(harness=CLAUDE, hold=hold)


def resolve_persona_target(name: str) -> list[MCPServer]:
  hold = session_hold()
  if hold is None:
    raise RuntimeError(f'persona servers run inside a managed session, which sets {HOLD_VARIABLE}')
  return persona_servers(create_bro(name), hold)
