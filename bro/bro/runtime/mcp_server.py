#!/usr/bin/env python

# module import stays cheap by design: every heavy dependency (mcp, starlette,
# uvicorn, llm.mcp, flow / the bro graph) is imported inside the function that
# needs it, so the --http path can bind its socket — and publish the port via
# --port-file — milliseconds after process start, before the import-dominated
# tool resolution (see main's bind-before-import ordering).

import asyncio
import contextlib
import os
import secrets
import socket
from collections.abc import Callable
from typing import TYPE_CHECKING, Optional

import base.args

if TYPE_CHECKING:
  from mcp.server.lowlevel import Server
  from starlette.types import ASGIApp, Receive, Scope, Send

  from llm.mcp import MCPServer, Tool

_BRO_PREFIX = 'bro:'
_PERSONA_PREFIX = 'persona:'


def _flow_server() -> 'MCPServer':
  import flow.mcp

  return flow.mcp.spec().build()


def _brog_server() -> 'MCPServer':
  import brog.mcp

  return brog.mcp.spec().build()


# lazy factories: a spec pays its import only when resolved, never at parser
# construction
_STATIC_SERVERS: dict[str, Callable[[], 'MCPServer']] = {
  'flow': _flow_server,
  'brog': _brog_server,
}


def _resolve_servers(spec: str) -> list['MCPServer']:
  if spec.startswith(_BRO_PREFIX):
    from bro.registry import create_bro

    return create_bro(spec[len(_BRO_PREFIX) :]).claude_bro_mcp_servers()
  if spec.startswith(_PERSONA_PREFIX):
    from bro.registry import create_bro

    return create_bro(spec[len(_PERSONA_PREFIX) :]).claude_persona_mcp_servers()
  factory = _STATIC_SERVERS.get(spec)
  if factory is None:
    raise SystemExit(
      f'unknown server {spec!r}; expected one of {sorted(_STATIC_SERVERS)}, '
      'bro:<name>, or persona:<name>'
    )
  return [factory()]


async def _server_tools(server: 'MCPServer') -> list[tuple['Tool', str]]:
  # (tool, description) pairs, with credential directives in each description resolved
  # against live credential availability — the serving-side counterpart of the
  # rendering the bro-LLM assembling layer does (llm.mcp `_NamespacedTool`).
  from llm.mcp import render_text

  declared = set(server.needed_secrets) | set(server.optional_secrets)
  return [
    (tool, render_text(tool.description, creds=declared)) for tool in await server.list_tools()
  ]


def _lowlevel_server(label: str, entries: list[tuple['Tool', str]]) -> 'Server':
  import mcp.types as types
  from mcp.server.lowlevel import Server

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

  def __init__(self, app: 'ASGIApp', token: str):
    self._app = app
    self._expected = f'Bearer {token}'.encode()

  async def __call__(self, scope: 'Scope', receive: 'Receive', send: 'Send') -> None:
    from starlette.responses import JSONResponse

    if scope['type'] != 'http' or scope['path'] == '/health':
      await self._app(scope, receive, send)
      return
    supplied = dict(scope['headers']).get(b'authorization', b'')
    if not secrets.compare_digest(supplied, self._expected):
      await JSONResponse({'error': 'unauthorized'}, status_code=401)(scope, receive, send)
      return
    await self._app(scope, receive, send)


def create_http_app(servers: list['MCPServer'], bearer_token: str) -> _BearerAuth:
  """Starlette app serving each namespace's tools at `/<namespace>` over streamable HTTP.

  tools keep their local names — the namespace reaches the client through the
  endpoint it mounts, so a client that mounts `/<ns>` under server key `<ns>`
  addresses a tool as `<ns>__<tool>` (Claude Code: `mcp__<ns>__<tool>`).
  same-namespace servers merge into one endpoint; a duplicate tool name within a
  namespace raises. tool resolution is eager, so once the app is constructed
  (and `/health` answers) every endpoint is ready to serve.
  """
  from mcp.server.fastmcp.server import StreamableHTTPASGIApp
  from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
  from starlette.applications import Starlette
  from starlette.requests import Request
  from starlette.responses import JSONResponse, Response
  from starlette.routing import Route

  async def collect() -> dict[str, list[tuple['Tool', str]]]:
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


async def run(mcp_server: 'MCPServer'):
  from mcp.server.stdio import stdio_server

  server = _lowlevel_server('mcp', await _server_tools(mcp_server))
  async with stdio_server() as (read_stream, write_stream):
    await server.run(read_stream, write_stream, server.create_initialization_options())


def _write_port_file(path: str, port: int) -> None:
  # write-then-rename so a reader polling for the file never sees a partial value
  tmp = f'{path}.tmp'
  with open(tmp, 'w') as f:
    f.write(str(port))
  os.replace(tmp, path)


def main(argv: list[str]) -> Optional[int]:
  parser = base.args.Parser(description='generic MCP server: stdio by default, HTTP with --http')
  parser.add_argument(
    'server',
    help=f'server to serve: {sorted(_STATIC_SERVERS)}, bro:<name>, or persona:<name>',
  )
  parser.add_argument(
    '--http',
    action='store_true',
    help='serve over streamable HTTP, one endpoint per tool namespace',
  )
  parser.add_argument('--host', default='127.0.0.1', help='HTTP bind host')
  parser.add_argument(
    '--port', type=int, help='HTTP port; 0 binds an OS-assigned one (required with --http)'
  )
  parser.add_argument(
    '--port-file',
    help='write the bound port to this file as soon as the socket is bound (requires --http)',
  )
  parser.add_argument(
    '--bearer-token',
    secret=True,
    help='token required on every HTTP request except /health (required with --http)',
  )
  args = parser.parse(argv)

  if not bool(args['http']):
    if (
      args['port'] is not None or args['port_file'] is not None or args['bearer_token'] is not None
    ):
      raise SystemExit('--port/--port-file/--bearer-token only apply with --http')
    if args['server'].startswith((_BRO_PREFIX, _PERSONA_PREFIX)):
      raise SystemExit(
        'bro:<name> and persona:<name> serve one endpoint per namespace; run them with --http'
      )
    asyncio.run(run(_resolve_servers(args['server'])[0]))
    return None

  if args['port'] is None or args['bearer_token'] is None:
    raise SystemExit('--http requires --port and --bearer-token')
  # bind before the heavy import/tool resolution: the port is discoverable
  # (--port-file) milliseconds in and is never released between discovery and
  # serving, and a client connect that lands mid-import sits in the TCP backlog
  # until uvicorn accepts on the pre-bound socket.
  server_socket = socket.create_server((args['host'], int(args['port'])))
  if args['port_file'] is not None:
    _write_port_file(args['port_file'], server_socket.getsockname()[1])
  app = create_http_app(_resolve_servers(args['server']), args['bearer_token'])
  import uvicorn

  uvicorn.Server(uvicorn.Config(app, log_level='info')).run(sockets=[server_socket])
  return None
