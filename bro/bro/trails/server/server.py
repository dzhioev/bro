#!/usr/bin/env python
"""aiohttp HTTP API for the trails service.

Frames the storage layer for HTTP: bearer-token auth on every `/v1/*` route,
body validation, mapping of storage exceptions to HTTP statuses. Step ids are
minted client-side (a ULID, reused across retries) so the conditional step Put
makes a retried POST idempotent; older clients that omit `step_id` fall back to
a server-minted id. Schema and write semantics live in the parent `save bros
logs` design doc (the schema-locking gate was stage 3).
"""

import hmac
import json
import os
import sys
from typing import Any

import boto3
from aiohttp import web

import base.args
from base import log
from trails.server import storage

__cli_name__ = 'trails-server'

DEFAULT_PORT = 8004
LOOPBACK_HOSTS = frozenset({'127.0.0.1', 'localhost'})

VALID_STEP_KINDS = frozenset(storage.STEP_KINDS) - {'system_prompt', 'end'}
VALID_END_REASONS = frozenset({'terminal', 'raised', 'error'})


@web.middleware
async def _auth_middleware(request: web.Request, handler):
  if request.path == '/health':
    return await handler(request)
  expected = request.app['bearer_token']
  if expected is None:
    return await handler(request)
  header = request.headers.get('Authorization', '')
  if not hmac.compare_digest(header, f'Bearer {expected}'):
    return web.json_response({'error': 'unauthorized'}, status=401)
  return await handler(request)


async def _read_json(request: web.Request) -> Any:
  try:
    return await request.json()
  except json.JSONDecodeError:
    return None


def _err(message: str, status: int) -> web.Response:
  return web.json_response({'error': message}, status=status)


async def _handle_health(_: web.Request) -> web.Response:
  return web.json_response({'status': 'ok'})


async def _handle_create_trail(request: web.Request) -> web.Response:
  payload = await _read_json(request)
  if not isinstance(payload, dict):
    return _err('invalid json', 400)
  for key in ('bro', 'bro_version', 'llm_spec', 'system_prompt', 'interactive', 'entry_point'):
    if key not in payload:
      return _err(f'missing field: {key}', 400)
  if not isinstance(payload['bro'], str):
    return _err('bro must be a string', 400)
  if not isinstance(payload['bro_version'], int) or isinstance(payload['bro_version'], bool):
    return _err('bro_version must be an int', 400)
  if not isinstance(payload['llm_spec'], dict):
    return _err('llm_spec must be an object', 400)
  if not isinstance(payload['system_prompt'], str):
    return _err('system_prompt must be a string', 400)
  if not isinstance(payload['interactive'], bool):
    return _err('interactive must be a bool', 400)
  if not isinstance(payload['entry_point'], str):
    return _err('entry_point must be a string', 400)
  parent = payload.get('parent')
  if parent is not None and not isinstance(parent, dict):
    return _err('parent must be an object or null', 400)
  if isinstance(parent, dict):
    for k in ('trail_id', 'step_id', 'relationship'):
      if k not in parent:
        return _err(f'parent.{k} required', 400)

  store: storage.Storage = request.app['storage']
  result = await store.create_trail(
    bro=payload['bro'],
    bro_version=payload['bro_version'],
    llm_spec=payload['llm_spec'],
    system_prompt=payload['system_prompt'],
    parent=parent,
    interactive=payload['interactive'],
    entry_point=payload['entry_point'],
  )
  return web.json_response({'trail_id': result['trail_id']}, status=201)


async def _handle_put_step(request: web.Request) -> web.Response:
  trail_id = request.match_info['trail_id']
  payload = await _read_json(request)
  if not isinstance(payload, dict):
    return _err('invalid json', 400)
  kind = payload.get('kind')
  if not isinstance(kind, str) or kind not in VALID_STEP_KINDS:
    return _err(f'kind must be one of {sorted(VALID_STEP_KINDS)}', 400)
  body = payload.get('body')
  step_id = payload.get('step_id')
  if step_id is not None and not isinstance(step_id, str):
    return _err('step_id must be a string', 400)
  extras = {k: v for k, v in payload.items() if k not in ('kind', 'body', 'step_id')}

  store: storage.Storage = request.app['storage']
  try:
    await store.put_step(trail_id=trail_id, kind=kind, body=body, extras=extras, step_id=step_id)
  except storage.BodyTooLarge as e:
    return _err(str(e), 413)
  except storage.TrailNotFound:
    return _err(f'trail not found: {trail_id}', 404)
  return web.Response(status=204)


async def _handle_end_trail(request: web.Request) -> web.Response:
  trail_id = request.match_info['trail_id']
  payload = await _read_json(request)
  if not isinstance(payload, dict):
    return _err('invalid json', 400)
  reason = payload.get('reason')
  if reason not in VALID_END_REASONS:
    return _err(f'reason must be one of {sorted(VALID_END_REASONS)}', 400)
  continuation = payload.get('continuation')
  if continuation is not None and not isinstance(continuation, dict):
    return _err('continuation must be an object or null', 400)
  step_id = payload.get('step_id')
  if step_id is not None and not isinstance(step_id, str):
    return _err('step_id must be a string', 400)

  store: storage.Storage = request.app['storage']
  try:
    await store.end_trail(
      trail_id=trail_id, reason=reason, continuation=continuation, step_id=step_id
    )
  except storage.TrailNotFound:
    return _err(f'trail not found: {trail_id}', 404)
  return web.Response(status=204)


async def _handle_get_trail(request: web.Request) -> web.Response:
  trail_id = request.match_info['trail_id']
  store: storage.Storage = request.app['storage']
  trail = await store.get_trail(trail_id)
  if trail is None:
    return _err(f'trail not found: {trail_id}', 404)
  return web.json_response(trail)


async def _handle_get_steps(request: web.Request) -> web.Response:
  trail_id = request.match_info['trail_id']
  after = request.query.get('after')
  limit = _parse_limit(request.query.get('limit'), default=100, ceiling=500)
  store: storage.Storage = request.app['storage']
  result = await store.query_steps(trail_id, after=after, limit=limit)
  return web.json_response(result)


async def _handle_list_trails(request: web.Request) -> web.Response:
  bro = request.query.get('bro')
  parent = request.query.get('parent')
  since = request.query.get('since')
  until = request.query.get('until')
  cursor = request.query.get('cursor')
  limit = _parse_limit(request.query.get('limit'), default=20, ceiling=100)
  if bro is not None and parent is not None:
    return _err('only one of bro/parent may be set', 400)
  store: storage.Storage = request.app['storage']
  result = await store.list_trails(
    bro=bro, parent=parent, since=since, until=until, cursor=cursor, limit=limit
  )
  return web.json_response(result)


def _parse_limit(raw: str | None, *, default: int, ceiling: int) -> int:
  if raw is None:
    return default
  try:
    value = int(raw)
  except ValueError:
    return default
  return max(1, min(ceiling, value))


def create_app(store: storage.Storage, bearer_token: str | None) -> web.Application:
  app = web.Application(
    middlewares=[_auth_middleware], client_max_size=storage.MAX_BODY_BYTES + 64 * 1024
  )
  app['storage'] = store
  app['bearer_token'] = bearer_token
  app.router.add_get('/health', _handle_health)
  app.router.add_post('/v1/trails', _handle_create_trail)
  app.router.add_get('/v1/trails', _handle_list_trails)
  app.router.add_get('/v1/trails/{trail_id}', _handle_get_trail)
  app.router.add_post('/v1/trails/{trail_id}/steps', _handle_put_step)
  app.router.add_get('/v1/trails/{trail_id}/steps', _handle_get_steps)
  app.router.add_post('/v1/trails/{trail_id}/end', _handle_end_trail)
  return app


def resolve_auth(bearer_token: str | None, allow_no_auth: bool, host: str) -> str | None:
  if bearer_token is not None:
    return bearer_token
  if not allow_no_auth:
    raise RuntimeError(
      'TRAILS_BEARER_TOKEN is required; set TRAILS_ALLOW_NO_AUTH=1 to disable auth'
    )
  if host not in LOOPBACK_HOSTS:
    raise RuntimeError(f'TRAILS_ALLOW_NO_AUTH=1 requires HOST in {sorted(LOOPBACK_HOSTS)}')
  return None


def main(argv: list[str]) -> int | None:
  parser = base.args.Parser(description='trails recording server')
  parser.add_argument('--host', default='0.0.0.0')
  parser.add_argument('--port', type=int, default=DEFAULT_PORT)
  parser.add_argument('--trails-bearer-token', default=None, secret=True)
  parser.add_argument('--trails-allow-no-auth', action='store_true')
  parser.add_argument('--trails-table', required=True)
  parser.add_argument('--steps-table', required=True)
  parser.add_argument('--spillover-bucket', required=True)
  parser.add_argument('--aws-region', default=os.environ.get('AWS_REGION', 'eu-central-1'))
  args = parser.parse(argv)

  bearer_token = resolve_auth(
    bearer_token=args['trails_bearer_token'],
    allow_no_auth=args['trails_allow_no_auth'],
    host=args['host'],
  )

  session = boto3.Session(region_name=args['aws_region'])
  store = storage.Storage(
    dynamo=session.client('dynamodb'),
    s3=session.client('s3'),
    trails_table=args['trails_table'],
    steps_table=args['steps_table'],
    bucket=args['spillover_bucket'],
  )

  auth_desc = 'bearer auth' if bearer_token is not None else 'NO AUTH'
  log.info(f'starting trails server on {args["host"]}:{args["port"]} ({auth_desc})')
  web.run_app(create_app(store, bearer_token), host=args['host'], port=args['port'])


# deployed via `python -m trails.server.server` (Dockerfile), so it keeps its own
# entry guard; the `trails-server` console script routes through the bridge.
if __name__ == '__main__':
  sys.exit(main(sys.argv))
