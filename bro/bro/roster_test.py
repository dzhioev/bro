"""every registered bro's served tool text must survive its roster scoping.

Builds each bro's real tool surfaces (live servers plus the service server, per
consuming harness) and audits the text each tool serves — description and
parameter-schema annotations: no unrendered template directive may leak, and no
text may name a sibling tool that the build's scoping excluded (the audit that
motivated server-owned rendering; `reference/conditions.md`, "Server-domain
vocabularies").
"""

import re

import pytest

from base.template import _DIRECTIVE_RE
from bro.bro import BaseBro
from bro.bros import BRO_SPECS
from bro.registry import create_bro
from llm.mcp import MCPServer, Tool

# (surface label, server-list builder) — the three consuming harnesses a bro's
# declared components serve
_SURFACES = [
  ('bro-native', lambda bro: bro._mcp_servers_for(hold='unattended')),
  ('claude-bro', lambda bro: bro.claude_bro_mcp_servers()),
  ('claude-persona', lambda bro: bro.claude_persona_mcp_servers()),
]


@pytest.fixture(autouse=True)
def brog_config(monkeypatch):
  # brog's state factory reads the self-contained `brog` secret at build
  monkeypatch.setattr(
    'base.credentials.get_json',
    lambda name: {'backend': 'flow', 'transport': 'http', 'url': 'https://x', 'token': 't'},
  )


def _served_texts(tool: Tool) -> list[str]:
  texts = [tool.description]

  def walk(node) -> None:
    if isinstance(node, dict):
      for key, value in node.items():
        if key == 'description' and isinstance(value, str):
          texts.append(value)
        else:
          walk(value)
    elif isinstance(node, list):
      for item in node:
        walk(item)

  walk(tool.parameters)
  return texts


async def _audit_server(label: str, server: MCPServer) -> list[str]:
  problems: list[str] = []
  tools = await server.list_tools()
  mounted = {tool.name for tool in tools}
  universe = server.tool_universe if server.tool_universe is not None else tuple(mounted)
  excluded = set(universe) - mounted
  for tool in tools:
    where = f'{label} {server.namespace}::{tool.name}'
    for text in _served_texts(tool):
      if _DIRECTIVE_RE.search(text) is not None:
        problems.append(f'{where}: unrendered template directive in served text')
      for name in excluded:
        if re.search(rf'\b{re.escape(name)}\b', text) is not None:
          problems.append(f'{where}: names {name!r}, excluded from this roster')
  return problems


@pytest.mark.asyncio
@pytest.mark.parametrize('name', sorted(BRO_SPECS))
async def test_served_tool_text_stays_inside_the_roster(name):
  problems: list[str] = []
  for label, servers_for in _SURFACES:
    # a fresh instance per surface: the service server and live servers cache
    # per instance, and each surface must build its own
    for server in servers_for(create_bro(name)):
      problems.extend(await _audit_server(f'{name}/{label}', server))
  assert problems == []


@pytest.mark.asyncio
@pytest.mark.parametrize('name', sorted(BRO_SPECS))
async def test_composed_prompts_leak_no_directives(name):
  bro = create_bro(name)
  for prompt in (bro.system_prompt, bro.claude_system_prompt):
    assert _DIRECTIVE_RE.search(prompt) is None


class TestSummonRecoveryFork:
  # the summon description's lost-request-id recovery path must match the
  # mount: `summon_list` exists only when the session tracks summon status
  def _summon_description(self, bro: BaseBro) -> str:
    server = bro.claude_bro_mcp_servers()[-1]
    by_name = {tool.name: tool for tool in __import__('asyncio').run(server.list_tools())}
    assert 'summon_list' in (server.tool_universe or ())
    return by_name['summon'].description, 'summon_list' in by_name  # type: ignore[return-value]

  def test_recovery_names_summon_list_only_when_mounted(self, monkeypatch):
    monkeypatch.setenv('BROKER_CHANNEL', '/tmp/test-broker.sock')
    monkeypatch.delenv('CW_SUMMON_STATUS', raising=False)
    description, mounted = self._summon_description(create_bro('bro'))
    assert not mounted
    assert 'summon_list' not in description

    monkeypatch.setenv('CW_SUMMON_STATUS', '/tmp/test-summon-status.json')
    description, mounted = self._summon_description(create_bro('bro'))
    assert mounted
    assert 'recover the request id with summon_list' in description
