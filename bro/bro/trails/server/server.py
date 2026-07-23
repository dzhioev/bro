#!/usr/bin/env python
"""aiohttp API for the universal trails registry."""

import asyncio
import contextlib
import hmac
import json
import os
import sys
from typing import Any, Optional

import boto3
from aiohttp import web

import base.args
from base import log
from trails.server import storage

__cli_name__ = 'trails-server'

DEFAULT_PORT = 8004
LOOPBACK_HOSTS = frozenset({'127.0.0.1', 'localhost'})
VALID_STEP_KINDS = frozenset(storage.STEP_KINDS) - {'system_prompt', 'end'}
VALID_END_REASONS = frozenset({'ok', 'raised', 'error'})
VALID_HOLDS = frozenset({'guided', 'attended', 'detached', 'unattended'})
SWEEP_INTERVAL_SECONDS = 600.0


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
  except (json.JSONDecodeError, UnicodeDecodeError):
    return None


def _error(message: str, status: int) -> web.Response:
  return web.json_response({'error': message}, status=status)


def _required(payload: dict, fields: tuple[str, ...]) -> Optional[web.Response]:
  for field in fields:
    if field not in payload:
      return _error(f'missing field: {field}', 400)
  return None


def _string(payload: dict, field: str, *, optional: bool = False) -> Optional[web.Response]:
  value = payload.get(field)
  if optional and value is None:
    return None
  if not isinstance(value, str) or len(value) == 0:
    return _error(f'{field} must be a non-empty string', 400)
  return None


def _pointer(payload: dict, field: str, *, step_optional: bool) -> Optional[web.Response]:
  value = payload.get(field)
  if value is None:
    return None
  allowed = {'trail_id', 'step_id'} if step_optional else {'trail_id', 'step_id'}
  required = {'trail_id'} if step_optional else allowed
  if (
    not isinstance(value, dict) or not required.issubset(value) or not set(value).issubset(allowed)
  ):
    suffix = '{trail_id, step_id?}' if step_optional else '{trail_id, step_id}'
    return _error(f'{field} must be {suffix}', 400)
  for key in value:
    if not isinstance(value[key], str) or len(value[key]) == 0:
      return _error(f'{field}.{key} must be a non-empty string', 400)
  return None


async def _handle_health(_: web.Request) -> web.Response:
  return web.json_response({'status': 'ok'})


async def _handle_create_trail(request: web.Request) -> web.Response:
  payload = await _read_json(request)
  if not isinstance(payload, dict):
    return _error('invalid json', 400)
  missing = _required(payload, ('harness', 'version', 'interactive', 'surface', 'body'))
  if missing is not None:
    return missing
  allowed_fields = {
    'harness',
    'version',
    'interactive',
    'surface',
    'body',
    'bro',
    'hold',
    'forked_from',
    'summoned_by',
    'subject',
    'location',
    'native',
  }
  unknown_fields = set(payload) - allowed_fields
  if len(unknown_fields) > 0:
    return _error(f'unknown fields: {sorted(unknown_fields)}', 400)
  for field in ('harness', 'version', 'surface'):
    invalid = _string(payload, field)
    if invalid is not None:
      return invalid
  if not isinstance(payload['interactive'], bool):
    return _error('interactive must be a bool', 400)
  if not isinstance(payload['body'], dict):
    return _error('body must be an object', 400)
  for field in ('bro', 'subject'):
    invalid = _string(payload, field, optional=True)
    if invalid is not None:
      return invalid
  if payload.get('hold') is not None and payload['hold'] not in VALID_HOLDS:
    return _error(f'hold must be one of {sorted(VALID_HOLDS)}', 400)
  for field, step_optional in (('forked_from', False), ('summoned_by', True)):
    invalid = _pointer(payload, field, step_optional=step_optional)
    if invalid is not None:
      return invalid
  native = payload.get('native')
  if not isinstance(native, dict):
    return _error('native must be an object', 400)
  if payload['harness'] == 'bro':
    if not isinstance(payload.get('bro'), str):
      return _error('bro is required for the bro harness', 400)
    if not isinstance(native.get('llm'), dict):
      return _error('native.llm is required for the bro harness', 400)
  elif payload['harness'] == 'claude':
    for field, expected_type in (
      ('llm', dict),
      ('segment', str),
      ('cw_command', str),
      ('harness_version', str),
    ):
      if not isinstance(native.get(field), expected_type):
        return _error(f'native.{field} is required for the claude harness', 400)
  else:
    return _error(f'unsupported harness: {payload["harness"]}', 400)
  location = payload.get('location')
  if location is not None:
    if not isinstance(location, dict) or not set(location).issubset(
      {'host', 'workspace', 'dir', 'is_container'}
    ):
      return _error('location has unknown fields', 400)
    for field in ('host', 'workspace', 'dir'):
      if location.get(field) is not None and not isinstance(location[field], str):
        return _error(f'location.{field} must be a string', 400)
    if location.get('is_container') is not None and not isinstance(location['is_container'], bool):
      return _error('location.is_container must be a bool', 400)

  store: storage.Storage = request.app['storage']
  try:
    result = await store.create_trail(
      harness=payload['harness'],
      version=payload['version'],
      interactive=payload['interactive'],
      surface=payload['surface'],
      body=payload['body'],
      bro=payload.get('bro'),
      hold=payload.get('hold'),
      forked_from=payload.get('forked_from'),
      summoned_by=payload.get('summoned_by'),
      subject=payload.get('subject'),
      location=payload.get('location'),
      native=payload.get('native'),
    )
  except ValueError as exception:
    return _error(str(exception), 400)
  return web.json_response(result, status=201)


async def _handle_put_step(request: web.Request) -> web.Response:
  trail_id = request.match_info['trail_id']
  payload = await _read_json(request)
  if not isinstance(payload, dict):
    return _error('invalid json', 400)
  kind = payload.get('kind')
  if not isinstance(kind, str) or kind not in VALID_STEP_KINDS:
    return _error(f'kind must be one of {sorted(VALID_STEP_KINDS)}', 400)
  step_id = payload.get('step_id')
  if step_id is not None and not isinstance(step_id, str):
    return _error('step_id must be a string', 400)
  extras = {key: value for key, value in payload.items() if key not in {'kind', 'body', 'step_id'}}
  store: storage.Storage = request.app['storage']
  try:
    await store.put_step(
      trail_id=trail_id,
      kind=kind,
      body=payload.get('body'),
      extras=extras,
      step_id=step_id,
    )
  except storage.BodyTooLarge as exception:
    return _error(str(exception), 413)
  except storage.TrailNotFound:
    return _error(f'trail not found: {trail_id}', 404)
  except ValueError as exception:
    return _error(str(exception), 400)
  return web.Response(status=204)


async def _handle_replace_artifact(request: web.Request) -> web.Response:
  trail_id = request.match_info['trail_id']
  payload = await _read_json(request)
  if not isinstance(payload, dict):
    return _error('invalid json', 400)
  unknown_fields = set(payload) - {'artifact', 'native'}
  if len(unknown_fields) > 0:
    return _error(f'unknown fields: {sorted(unknown_fields)}', 400)
  artifact = payload.get('artifact')
  if not isinstance(artifact, str):
    return _error('artifact must be a string', 400)
  native = payload.get('native', {})
  if not isinstance(native, dict):
    return _error('native must be an object', 400)
  store: storage.Storage = request.app['storage']
  try:
    updates = await store.replace_artifact(trail_id, artifact, native)
  except storage.TrailNotFound:
    return _error(f'trail not found: {trail_id}', 404)
  except ValueError as exception:
    return _error(str(exception), 400)
  return web.json_response({'native': updates})


async def _handle_update_header(request: web.Request) -> web.Response:
  trail_id = request.match_info['trail_id']
  payload = await _read_json(request)
  if not isinstance(payload, dict):
    return _error('invalid json', 400)
  subject = payload.get('subject')
  if 'subject' in payload and subject is not None and not isinstance(subject, str):
    return _error('subject must be a string or null', 400)
  if 'last_alive_at' in payload and not isinstance(payload['last_alive_at'], str):
    return _error('last_alive_at must be a string', 400)
  turn_count = payload.get('turn_count')
  if 'turn_count' in payload and (
    not isinstance(turn_count, int) or isinstance(turn_count, bool) or turn_count < 0
  ):
    return _error('turn_count must be a non-negative int', 400)
  if 'native' in payload and not isinstance(payload['native'], dict):
    return _error('native must be an object', 400)
  store: storage.Storage = request.app['storage']
  try:
    updated = await store.update_header(trail_id, payload)
  except storage.TrailNotFound:
    return _error(f'trail not found: {trail_id}', 404)
  except ValueError as exception:
    return _error(str(exception), 400)
  return web.json_response(updated)


async def _handle_end_trail(request: web.Request) -> web.Response:
  trail_id = request.match_info['trail_id']
  payload = await _read_json(request)
  if not isinstance(payload, dict):
    return _error('invalid json', 400)
  unknown_fields = set(payload) - {'reason', 'detail', 'step_id'}
  if len(unknown_fields) > 0:
    return _error(f'unknown fields: {sorted(unknown_fields)}', 400)
  reason = payload.get('reason')
  if reason not in VALID_END_REASONS:
    return _error(f'reason must be one of {sorted(VALID_END_REASONS)}', 400)
  detail = payload.get('detail')
  if detail is not None and not isinstance(detail, str):
    return _error('detail must be a string or null', 400)
  if reason in {'raised', 'error'} and (not isinstance(detail, str) or len(detail) == 0):
    return _error(f'detail is required for {reason}', 400)
  step_id = payload.get('step_id')
  if step_id is not None and not isinstance(step_id, str):
    return _error('step_id must be a string', 400)
  store: storage.Storage = request.app['storage']
  try:
    await store.end_trail(trail_id=trail_id, reason=reason, detail=detail, step_id=step_id)
  except storage.TrailNotFound:
    return _error(f'trail not found: {trail_id}', 404)
  return web.Response(status=204)


async def _handle_keepalive(request: web.Request) -> web.Response:
  trail_id = request.match_info['trail_id']
  store: storage.Storage = request.app['storage']
  try:
    await store.keepalive(trail_id)
  except storage.TrailNotFound:
    return _error(f'trail not found: {trail_id}', 404)
  return web.Response(status=204)


async def _handle_get_trail(request: web.Request) -> web.Response:
  trail_id = request.match_info['trail_id']
  store: storage.Storage = request.app['storage']
  trail = await store.get_trail(trail_id)
  if trail is None:
    return _error(f'trail not found: {trail_id}', 404)
  return web.json_response(trail)


async def _handle_get_steps(request: web.Request) -> web.Response:
  trail_id = request.match_info['trail_id']
  after = request.query.get('after')
  limit = _parse_limit(request.query.get('limit'), default=100, ceiling=500)
  store: storage.Storage = request.app['storage']
  try:
    result = await store.query_steps(trail_id, after=after, limit=limit)
  except storage.TrailNotFound:
    return _error(f'trail not found: {trail_id}', 404)
  return web.json_response(result)


async def _handle_get_messages(request: web.Request) -> web.Response:
  trail_id = request.match_info['trail_id']
  after = request.query.get('after')
  limit = _parse_limit(request.query.get('limit'), default=100, ceiling=500)
  requested_types = set(request.query.getall('type', []))
  unknown = requested_types - storage.MESSAGE_TYPES
  if len(unknown) > 0:
    return _error(f'unknown message types: {sorted(unknown)}', 400)
  store: storage.Storage = request.app['storage']
  try:
    result = await store.query_messages(
      trail_id,
      after=after,
      limit=limit,
      types=requested_types if len(requested_types) > 0 else None,
    )
  except storage.TrailNotFound:
    return _error(f'trail not found: {trail_id}', 404)
  return web.json_response(result)


async def _handle_list_trails(request: web.Request) -> web.Response:
  harness = request.query.get('harness')
  bro = request.query.get('bro')
  forked_from = request.query.get('forked_from')
  if sum(value is not None for value in (harness, bro, forked_from)) > 1:
    return _error('only one of harness/bro/forked_from may be set', 400)
  store: storage.Storage = request.app['storage']
  result = await store.list_trails(
    harness=harness,
    bro=bro,
    forked_from=forked_from,
    since=request.query.get('since'),
    until=request.query.get('until'),
    cursor=request.query.get('cursor'),
    limit=_parse_limit(request.query.get('limit'), default=20, ceiling=100),
  )
  return web.json_response(result)


def _parse_limit(raw: Optional[str], *, default: int, ceiling: int) -> int:
  if raw is None:
    return default
  try:
    value = int(raw)
  except ValueError as exception:
    raise web.HTTPBadRequest(reason='limit must be an integer') from exception
  if value < 1 or value > ceiling:
    raise web.HTTPBadRequest(reason=f'limit must be between 1 and {ceiling}')
  return value


async def _sweep_loop(store: storage.Storage, interval_seconds: float) -> None:
  while True:
    await asyncio.sleep(interval_seconds)
    try:
      swept = await store.sweep_lost()
      if len(swept) > 0:
        log.info('sweep stamped %d trails as lost: %s', len(swept), ', '.join(swept))
    except Exception as exception:
      log.warning('lost-trail sweep failed: %s', exception)


def create_app(
  store: storage.Storage,
  bearer_token: Optional[str],
  *,
  sweep_interval_seconds: Optional[float] = None,
) -> web.Application:
  app = web.Application(
    middlewares=[_auth_middleware], client_max_size=storage.MAX_BODY_BYTES + 64 * 1024
  )
  app['storage'] = store
  app['bearer_token'] = bearer_token
  app.router.add_get('/health', _handle_health)
  app.router.add_post('/v1/trails', _handle_create_trail)
  app.router.add_get('/v1/trails', _handle_list_trails)
  app.router.add_get('/v1/trails/{trail_id}', _handle_get_trail)
  app.router.add_patch('/v1/trails/{trail_id}', _handle_update_header)
  app.router.add_post('/v1/trails/{trail_id}/steps', _handle_put_step)
  app.router.add_get('/v1/trails/{trail_id}/steps', _handle_get_steps)
  app.router.add_get('/v1/trails/{trail_id}/messages', _handle_get_messages)
  app.router.add_put('/v1/trails/{trail_id}/artifact', _handle_replace_artifact)
  app.router.add_post('/v1/trails/{trail_id}/end', _handle_end_trail)
  app.router.add_post('/v1/trails/{trail_id}/keepalive', _handle_keepalive)
  if sweep_interval_seconds is not None:

    async def _sweep_context(app: web.Application):
      task = asyncio.create_task(_sweep_loop(app['storage'], sweep_interval_seconds))
      yield
      task.cancel()
      with contextlib.suppress(asyncio.CancelledError):
        await task

    app.cleanup_ctx.append(_sweep_context)
  return app


def resolve_auth(bearer_token: Optional[str], allow_no_auth: bool, host: str) -> Optional[str]:
  if bearer_token is not None:
    return bearer_token
  if not allow_no_auth:
    raise RuntimeError(
      'TRAILS_BEARER_TOKEN is required; set TRAILS_ALLOW_NO_AUTH=1 to disable auth'
    )
  if host not in LOOPBACK_HOSTS:
    raise RuntimeError(f'TRAILS_ALLOW_NO_AUTH=1 requires HOST in {sorted(LOOPBACK_HOSTS)}')
  return None


def main(argv: list[str]) -> Optional[int]:
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
  auth_description = 'bearer auth' if bearer_token is not None else 'NO AUTH'
  log.info(f'starting trails server on {args["host"]}:{args["port"]} ({auth_description})')
  web.run_app(
    create_app(store, bearer_token, sweep_interval_seconds=SWEEP_INTERVAL_SECONDS),
    host=args['host'],
    port=args['port'],
  )


if __name__ == '__main__':
  sys.exit(main(sys.argv))
