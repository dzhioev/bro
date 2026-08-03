#!/usr/bin/env python

# module import stays cheap by design: every heavy dependency (mcp, starlette,
# uvicorn, llm.mcp, flow / the bro graph) is imported inside the function that
# needs it, so the --http path can bind its socket — and publish the port via
# --port-file — milliseconds after process start, before the import-dominated
# tool resolution (see main's bind-before-import ordering).

import asyncio
import contextlib
import importlib.metadata
import os
import secrets
import socket
from collections.abc import Callable
from typing import TYPE_CHECKING, Optional, cast

import base.args

if TYPE_CHECKING:
  from mcp.server.lowlevel import Server
  from starlette.types import ASGIApp, Receive, Scope, Send

  from llm.mcp import MCPServer, MCPServerSpec, Tool

__cli_name__ = 'mcp-server'

BEARER_TOKEN_ENV = 'MCP_SERVER_BEARER_TOKEN'

_BRO_PREFIX = 'bro:'
_PERSONA_PREFIX = 'persona:'


_TOOLSET_ENTRY_POINT_GROUP = 'bro.toolsets'


def _toolset_entry_points() -> tuple[importlib.metadata.EntryPoint, ...]:
  return tuple(importlib.metadata.entry_points(group=_TOOLSET_ENTRY_POINT_GROUP))


def _toolset_server(namespace: str) -> 'MCPServer':
  matches = [
    entry_point for entry_point in _toolset_entry_points() if entry_point.name == namespace
  ]
  if len(matches) > 1:
    values = ', '.join(entry_point.value for entry_point in matches)
    raise SystemExit(f'duplicate toolset {namespace!r}: {values}')
  if len(matches) == 0:
    known = sorted(entry_point.name for entry_point in _toolset_entry_points())
    raise SystemExit(
      f'unknown server {namespace!r}; expected one of {known}, bro:<name>, or persona:<name>'
    )
  factory = matches[0].load()
  if not callable(factory):
    raise TypeError(f'toolset entry point {namespace!r} must load a callable')
  toolset_factory = cast('Callable[[], MCPServerSpec]', factory)
  return toolset_factory().build()


def _resolve_servers(spec: str) -> list['MCPServer']:
  if spec.startswith(_BRO_PREFIX):
    from bro.registry import create_bro

    return create_bro(spec[len(_BRO_PREFIX) :]).claude_bro_mcp_servers()
  if spec.startswith(_PERSONA_PREFIX):
    from bro.registry import create_bro

    return create_bro(spec[len(_PERSONA_PREFIX) :]).claude_persona_mcp_servers()
  return [_toolset_server(spec)]


def _lowlevel_server(label: str, entries: list['Tool']) -> 'Server':
  # tool text (descriptions, parameter annotations) arrives fully rendered —
  # each server renders its own at build time (llm.mcp `FunctionTool`).
  import mcp.types as types
  from mcp.server.lowlevel import Server

  tools_by_name: dict[str, Tool] = {}
  for tool in entries:
    if tool.name in tools_by_name:
      raise SystemExit(f'duplicate tool name {tool.name!r} in {label!r}')
    tools_by_name[tool.name] = tool

  server = Server(label)

  @server.list_tools()
  async def handle_list_tools() -> list[types.Tool]:
    return [
      types.Tool(
        name=tool.name,
        description=tool.description,
        inputSchema=tool.parameters,
        outputSchema=tool.output_schema,
      )
      for tool in entries
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

  async def collect() -> dict[str, list['Tool']]:
    by_namespace: dict[str, list[Tool]] = {}
    for server in servers:
      by_namespace.setdefault(server.namespace, []).extend(await server.list_tools())
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

  server = _lowlevel_server('mcp', await mcp_server.list_tools())
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
    help=(
      f'server to serve: {sorted(entry_point.name for entry_point in _toolset_entry_points())}, '
      'bro:<name>, or persona:<name>'
    ),
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
    help=f'token required on every HTTP request except /health (defaults to {BEARER_TOKEN_ENV})',
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

  bearer_token = args['bearer_token']
  if bearer_token is None:
    bearer_token = os.environ.get(BEARER_TOKEN_ENV)
  if args['port'] is None or bearer_token is None:
    raise SystemExit(f'--http requires --port and --bearer-token or {BEARER_TOKEN_ENV}')
  # bind before the heavy import/tool resolution: the port is discoverable
  # (--port-file) milliseconds in and is never released between discovery and
  # serving, and a client connect that lands mid-import sits in the TCP backlog
  # until uvicorn accepts on the pre-bound socket.
  server_socket = socket.create_server((args['host'], int(args['port'])))
  if args['port_file'] is not None:
    _write_port_file(args['port_file'], server_socket.getsockname()[1])
  app = create_http_app(_resolve_servers(args['server']), bearer_token)
  import uvicorn

  uvicorn.Server(uvicorn.Config(app, log_level='info')).run(sockets=[server_socket])
  return None
