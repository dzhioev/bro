"""Tool-schema assembly for the managed native live probe."""

from bro.authority_view_test_helper import publish
from bro.broker.environment import BROKER_CHANNEL
from bro.llm.llm import Recipe
from bro.llm.mcp import ToolRegistry
from bro.native.harness import BRO
from bro.native.runner import Runner
from bro.registry import create_bro
from bro.summon import SUMMONED_ENV
from bro.wire_chat.client import tools_to_chat_format


async def managed_roster(monkeypatch, recipe: Recipe) -> list[dict]:
  with monkeypatch.context() as surface:
    surface.setenv(BROKER_CHANNEL, 'offline-probe-channel')
    surface.setenv(SUMMONED_ENV, '1')
    publish(surface, targets=('bro-eyebro',))
    with Runner(create_bro('bro-dev', llm_spec=recipe)) as runner:
      return tools_to_chat_format(
        await ToolRegistry(
          runner.bro.assemble(harness=BRO, hold='unattended', live_run=runner)
        ).resolve()
      )
  raise RuntimeError('native roster assembly was interrupted')
