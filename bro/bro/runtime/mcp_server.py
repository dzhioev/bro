#!/usr/bin/env python

import asyncio
from typing import Optional

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

import base.args
from llm.mcp import MCPServer, namespaced_tools

_BRO_PREFIX = 'bro:'


def _static_servers() -> dict[str, MCPServer]:
  import flow

  return {
    'flow': flow.MCPServer(),
  }


def _resolve_server(spec: str) -> MCPServer:
  if spec.startswith(_BRO_PREFIX):
    return _compose_bro_server(spec[len(_BRO_PREFIX) :])
  static = _static_servers()
  if spec not in static:
    raise SystemExit(f'unknown server {spec!r}; expected one of {sorted(static)} or bro:<name>')
  return static[spec]


def _compose_bro_server(name: str) -> MCPServer:
  from bro.registry import create_bro

  return _Aggregate(name, create_bro(name)._mcp_servers)


class _Aggregate(MCPServer):
  """exposes the union of several MCPServers' tools under their `namespace__tool`
  wire names; errors on collision.

  this is the `cw ss --bro` stdio surface: Claude Code mounts it under the single
  `bro` server key, so a tool surfaces as `mcp__bro__<namespace>__<tool>`. the
  namespace keeps generically-named tools (two sources' `search`) distinct.
  """

  def __init__(self, label: str, servers: list[MCPServer]):
    self._label = label
    self._servers = servers

  async def list_tools(self):
    tools = []
    seen: set[str] = set()
    for server in self._servers:
      for wrapped in await namespaced_tools(server):
        if wrapped.name in seen:
          raise SystemExit(
            f'bro {self._label!r}: duplicate tool wire name {wrapped.name!r} '
            f'(namespace {server.namespace!r})'
          )
        seen.add(wrapped.name)
        tools.append(wrapped)
    return tools


async def run(mcp_server: MCPServer):
  tools = await mcp_server.list_tools()
  tools_by_name = {t.name: t for t in tools}

  server = Server('mcp')

  @server.list_tools()
  async def handle_list_tools() -> list[types.Tool]:
    return [
      types.Tool(
        name=t.name,
        description=t.description,
        inputSchema=t.parameters,
        outputSchema=t.output_schema,
      )
      for t in tools
    ]

  @server.call_tool()
  async def handle_call_tool(
    name: str, arguments: Optional[dict]
  ) -> list[types.TextContent] | dict:
    tool = tools_by_name.get(name)
    if tool is None:
      raise ValueError(f'unknown tool: {name}')
    result = await tool.call(arguments if arguments is not None else {})
    if isinstance(result, dict):
      return result
    return [types.TextContent(type='text', text=result)]

  async with stdio_server() as (read_stream, write_stream):
    await server.run(read_stream, write_stream, server.create_initialization_options())


def main(argv: list[str]) -> Optional[int]:
  parser = base.args.Parser(description='generic MCP stdio server')
  parser.add_argument(
    'server',
    help=f'server to serve: {sorted(_static_servers())} or bro:<name>',
  )
  args = parser.parse(argv)
  asyncio.run(run(_resolve_server(args['server'])))
