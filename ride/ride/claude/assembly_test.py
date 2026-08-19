import asyncio
import importlib.metadata

from bro.bro import BaseBro
from bro.runtime.mcp_server import _resolve_servers
from ride.claude.assembly import bro_servers, persona_servers


class _AssemblyBro(BaseBro):
  name = 'assembly-test'
  description = 'tests Claude assembly policy'
  system_prompt = 'test'


async def _tool_names(servers) -> set[str]:
  return {tool.name for server in servers for tool in await server.list_tools()}


def test_unattended_killable_session_mounts_raise(monkeypatch):
  monkeypatch.setenv('BRO_HOLD', 'unattended')
  monkeypatch.setenv('RIDE_RUNNER_PID', '4242')
  bro = _AssemblyBro()

  assert 'raise' in asyncio.run(_tool_names(bro_servers(bro)))
  assert 'raise' in asyncio.run(_tool_names(persona_servers(bro)))


def test_session_without_a_kill_target_does_not_mount_raise(monkeypatch):
  monkeypatch.setenv('BRO_HOLD', 'unattended')

  assert 'raise' not in asyncio.run(_tool_names(persona_servers(_AssemblyBro())))


def test_human_facing_holds_do_not_mount_raise(monkeypatch):
  monkeypatch.setenv('RIDE_RUNNER_PID', '4242')
  for hold in ('detached', 'attended', 'guided'):
    monkeypatch.setenv('BRO_HOLD', hold)
    assert 'raise' not in asyncio.run(_tool_names(persona_servers(_AssemblyBro())))


def test_ride_contributes_both_assembled_targets():
  entries = importlib.metadata.entry_points(group='bro.mcp.targets')
  assert {
    (entry.name, entry.value)
    for entry in entries
    if entry.value.startswith('ride.claude.assembly:')
  } == {
    ('bro', 'ride.claude.assembly:resolve_bro_target'),
    ('persona', 'ride.claude.assembly:resolve_persona_target'),
  }


def test_core_server_resolves_the_contributed_bro_target():
  assert 'bro' in {server.namespace for server in _resolve_servers('bro:bro')}
