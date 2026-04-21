#!/usr/bin/env python

import asyncio
import sys

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

import base.args
from llm.mcp import MCPServer


def _get_servers():
  from flow.mcp.bridge import create_flow_server

  return {
    'flow': create_flow_server,
  }


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
  async def handle_call_tool(name: str, arguments: dict | None) -> list[types.TextContent] | dict:
    tool = tools_by_name.get(name)
    if tool is None:
      raise ValueError(f'unknown tool: {name}')
    result = await tool.call(arguments if arguments is not None else {})
    if isinstance(result, dict):
      return result
    return [types.TextContent(type='text', text=result)]

  async with stdio_server() as (read_stream, write_stream):
    await server.run(read_stream, write_stream, server.create_initialization_options())


def main(argv=None) -> int | None:
  servers = _get_servers()
  parser = base.args.Parser(description='generic MCP stdio server')
  parser.add_argument('server', choices=list(servers.keys()))
  args = parser.parse(argv)
  mcp_server = servers[args['server']]()
  asyncio.run(run(mcp_server))


if __name__ == '__main__':
  sys.exit(main(sys.argv))
