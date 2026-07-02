#!/usr/bin/env python

import asyncio
import contextlib
import secrets
from typing import Optional

import mcp.types as types
import uvicorn
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

import base.args
from base import credentials
from llm.mcp import MCPServer, Tool, render_has_cred

_BRO_PREFIX = 'bro:'


def _static_servers() -> dict[str, MCPServer]:
  import flow

  return {
    'flow': flow.MCPServer(),
  }


def _resolve_servers(spec: str) -> list[MCPServer]:
  if spec.startswith(_BRO_PREFIX):
    from bro.registry import create_bro

    return create_bro(spec[len(_BRO_PREFIX) :]).claude_bro_mcp_servers()
  static = _static_servers()
  if spec not in static:
    raise SystemExit(f'unknown server {spec!r}; expected one of {sorted(static)} or bro:<name>')
  return [static[spec]]


async def _server_tools(server: MCPServer) -> list[tuple[Tool, str]]:
  # (tool, description) pairs, with `has_cred` blocks in each description resolved
  # against live credential availability — the serving-side counterpart of the
  # rendering the bro-LLM assembling layer does (llm.mcp `_NamespacedTool`).
  declared = set(server.needed_secrets) | set(server.optional_secrets)
  return [
    (tool, render_has_cred(tool.description, credentials.available, declared))
    for tool in await server.list_tools()
  ]


def _lowlevel_server(label: str, entries: list[tuple[Tool, str]]) -> Server:
  tools_by_name: dict[str, Tool] = {}
  for tool, _ in entries:
    if tool.name in tools_by_name:
      raise SystemExit(f'duplicate tool name {tool.name!r} in {label!r}')
    tools_by_name[tool.name] = tool

  server = Server(label)

  @server.list_tools()
  async def handle_list_tools() -> list[types.Tool]:
    return [
      types.Tool(
        name=tool.name,
        description=description,
        inputSchema=tool.parameters,
        outputSchema=tool.output_schema,
      )
      for tool, description in entries
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

  return server


class _BearerAuth:
  """ASGI wrapper requiring `Authorization: Bearer <token>` on every HTTP request.

  `/health` is exempt so a readiness poll needs no secret.
  """

  def __init__(self, app: ASGIApp, token: str):
    self._app = app
    self._expected = f'Bearer {token}'.encode()

  async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
    if scope['type'] != 'http' or scope['path'] == '/health':
      await self._app(scope, receive, send)
      return
    supplied = dict(scope['headers']).get(b'authorization', b'')
    if not secrets.compare_digest(supplied, self._expected):
      await JSONResponse({'error': 'unauthorized'}, status_code=401)(scope, receive, send)
      return
    await self._app(scope, receive, send)


def create_http_app(servers: list[MCPServer], bearer_token: str) -> _BearerAuth:
  """Starlette app serving each namespace's tools at `/<namespace>` over streamable HTTP.

  tools keep their local names — the namespace reaches the client through the
  endpoint it mounts, so a client that mounts `/<ns>` under server key `<ns>`
  addresses a tool as `<ns>__<tool>` (Claude Code: `mcp__<ns>__<tool>`).
  same-namespace servers merge into one endpoint; a duplicate tool name within a
  namespace raises. tool resolution is eager, so once the app is constructed
  (and `/health` answers) every endpoint is ready to serve.
  """

  async def collect() -> dict[str, list[tuple[Tool, str]]]:
    by_namespace: dict[str, list[tuple[Tool, str]]] = {}
    for server in servers:
      by_namespace.setdefault(server.namespace, []).extend(await _server_tools(server))
    return by_namespace

  by_namespace = asyncio.run(collect())

  managers: list[StreamableHTTPSessionManager] = []
  routes: list[Route] = []
  for namespace, entries in by_namespace.items():
    manager = StreamableHTTPSessionManager(
      app=_lowlevel_server(namespace, entries), stateless=True, json_response=True
    )
    managers.append(manager)
    routes.append(Route(f'/{namespace}', StreamableHTTPASGIApp(manager)))

  async def health(request: Request) -> Response:
    return JSONResponse({'status': 'ok', 'namespaces': sorted(by_namespace)})

  routes.append(Route('/health', health))

  @contextlib.asynccontextmanager
  async def lifespan(app: Starlette):
    async with contextlib.AsyncExitStack() as stack:
      for manager in managers:
        await stack.enter_async_context(manager.run())
      yield

  return _BearerAuth(Starlette(routes=routes, lifespan=lifespan), bearer_token)


async def run(mcp_server: MCPServer):
  server = _lowlevel_server('mcp', await _server_tools(mcp_server))
  async with stdio_server() as (read_stream, write_stream):
    await server.run(read_stream, write_stream, server.create_initialization_options())


def main(argv: list[str]) -> Optional[int]:
  parser = base.args.Parser(description='generic MCP server: stdio by default, HTTP with --http')
  parser.add_argument(
    'server',
    help=f'server to serve: {sorted(_static_servers())} or bro:<name>',
  )
  parser.add_argument(
    '--http',
    action='store_true',
    help='serve over streamable HTTP, one endpoint per tool namespace',
  )
  parser.add_argument('--host', default='127.0.0.1', help='HTTP bind host')
  parser.add_argument('--port', type=int, help='HTTP port (required with --http)')
  parser.add_argument(
    '--bearer-token',
    secret=True,
    help='token required on every HTTP request except /health (required with --http)',
  )
  args = parser.parse(argv)

  if not bool(args['http']):
    if args['port'] is not None or args['bearer_token'] is not None:
      raise SystemExit('--port/--bearer-token only apply with --http')
    if args['server'].startswith(_BRO_PREFIX):
      raise SystemExit('bro:<name> serves one endpoint per namespace; run it with --http')
    asyncio.run(run(_resolve_servers(args['server'])[0]))
    return None

  if args['port'] is None or args['bearer_token'] is None:
    raise SystemExit('--http requires --port and --bearer-token')
  app = create_http_app(_resolve_servers(args['server']), args['bearer_token'])
  uvicorn.run(app, host=args['host'], port=int(args['port']), log_level='info')
  return None
