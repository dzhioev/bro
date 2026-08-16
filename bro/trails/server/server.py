#!/usr/bin/env python
"""aiohttp API for the universal trails registry."""

import asyncio
import contextlib
import hmac
import json
import sys
from collections.abc import Callable, Coroutine
from typing import Any, Optional

from aiohttp import web

import bro.base.args as base_args
from bro.base import log
from bro.trails.model import (
  LOOPBACK_HOSTS,
  MESSAGE_TYPES,
  BlazeRequest,
  validate_end,
)
from bro.trails.server.dynamo import BodyTooLarge, DynamoStore
from bro.trails.store import AppendConflict, TrailNotFound, TrailsStore, default_store

__cli_name__ = 'trails-server'

DEFAULT_PORT = 8004
SWEEP_INTERVAL_SECONDS = 600.0
CHECK_HEARTBEAT_INTERVAL_SECONDS = 5.0
MAX_REQUEST_BYTES = 64 * 1024 * 1024


async def _dispatch[Result](operation: Callable[..., Result], *args: Any, **kwargs: Any) -> Result:
  return await asyncio.to_thread(operation, *args, **kwargs)


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


async def _stream_json_with_heartbeats(
  request: web.Request,
  operation: Coroutine[Any, Any, dict],
) -> web.StreamResponse:
  response = web.StreamResponse(status=200)
  response.content_type = 'application/json'
  await response.prepare(request)
  async with asyncio.TaskGroup() as task_group:
    operation_task = task_group.create_task(operation)
    while True:
      try:
        result = await asyncio.wait_for(
          asyncio.shield(operation_task), timeout=CHECK_HEARTBEAT_INTERVAL_SECONDS
        )
        break
      except TimeoutError:
        await response.write(b'\n')
  await response.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
  await response.write_eof()
  return response


def _pointer(payload: dict, field: str, *, step_optional: bool) -> Optional[web.Response]:
  value = payload.get(field)
  if value is None:
    return None
  allowed = {'trail_id', 'step_id', 'index'}
  required = {'trail_id'} if step_optional else {'trail_id', 'step_id'}
  if (
    not isinstance(value, dict) or not required.issubset(value) or not set(value).issubset(allowed)
  ):
    suffix = '{trail_id, step_id?, index?}' if step_optional else '{trail_id, step_id, index?}'
    return _error(f'{field} must be {suffix}', 400)
  trail_id = value.get('trail_id')
  if not isinstance(trail_id, str) or len(trail_id) == 0:
    return _error(f'{field}.trail_id must be a non-empty string', 400)
  step_id = value.get('step_id')
  if step_id is not None and (
    not isinstance(step_id, int) or isinstance(step_id, bool) or step_id < 0
  ):
    return _error(f'{field}.step_id must be a non-negative int', 400)
  index = value.get('index')
  if index is not None and (not isinstance(index, int) or isinstance(index, bool) or index < 0):
    return _error(f'{field}.index must be a non-negative int', 400)
  return None


async def _handle_health(_: web.Request) -> web.Response:
  return web.json_response({'status': 'ok'})


async def _handle_blaze(request: web.Request) -> web.Response:
  payload = await _read_json(request)
  if not isinstance(payload, dict):
    return _error('invalid json', 400)
  try:
    blaze_request = BlazeRequest.from_wire(payload)
  except ValueError as exception:
    return _error(str(exception), 400)
  store: TrailsStore = request.app['store']
  try:
    result = await _dispatch(store.blaze, blaze_request)
  except ValueError as exception:
    return _error(str(exception), 400)
  # a resolver that declines to adopt creates nothing, so the response is not 201
  return web.json_response(result, status=200 if result.get('adopted') is False else 201)


async def _handle_append_records(request: web.Request) -> web.Response:
  trail_id = request.match_info['trail_id']
  payload = await _read_json(request)
  if not isinstance(payload, dict):
    return _error('invalid json', 400)
  unknown = set(payload) - {'offset', 'records', 'tools'}
  if len(unknown) > 0:
    return _error(f'unknown fields: {sorted(unknown)}', 400)
  offset = payload.get('offset')
  if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
    return _error('offset must be a non-negative int', 400)
  records = payload.get('records')
  if not isinstance(records, list):
    return _error('records must be a list', 400)
  tools = payload.get('tools', {})
  if not isinstance(tools, dict):
    return _error('tools must be an object', 400)
  store: TrailsStore = request.app['store']
  try:
    result = await _dispatch(store.append_records, trail_id, offset, records, tools=tools)
  except BodyTooLarge as exception:
    return _error(str(exception), 413)
  except TrailNotFound:
    return _error(f'trail not found: {trail_id}', 404)
  except AppendConflict as exception:
    return web.json_response(
      {'error': str(exception), 'expected': exception.expected, 'extent': exception.actual},
      status=409,
    )
  except ValueError as exception:
    return _error(str(exception), 400)
  return web.json_response(result)


async def _handle_set_subject(request: web.Request) -> web.Response:
  trail_id = request.match_info['trail_id']
  payload = await _read_json(request)
  if not isinstance(payload, dict):
    return _error('invalid json', 400)
  unknown = set(payload) - {'subject'}
  if len(unknown) > 0:
    return _error(f'immutable or unknown header fields: {sorted(unknown)}', 400)
  subject = payload.get('subject')
  if subject is not None and not isinstance(subject, str):
    return _error('subject must be a string or null', 400)
  store: TrailsStore = request.app['store']
  try:
    updated = await _dispatch(store.set_subject, trail_id, subject)
  except TrailNotFound:
    return _error(f'trail not found: {trail_id}', 404)
  except ValueError as exception:
    return _error(str(exception), 400)
  return web.json_response(updated)


async def _handle_end_trail(request: web.Request) -> web.Response:
  trail_id = request.match_info['trail_id']
  payload = await _read_json(request)
  if not isinstance(payload, dict):
    return _error('invalid json', 400)
  unknown_fields = set(payload) - {'reason', 'detail'}
  if len(unknown_fields) > 0:
    return _error(f'unknown fields: {sorted(unknown_fields)}', 400)
  store: TrailsStore = request.app['store']
  try:
    reason, detail = validate_end(payload.get('reason'), payload.get('detail'))
    await _dispatch(store.end_trail, trail_id, reason, detail)
  except TrailNotFound:
    return _error(f'trail not found: {trail_id}', 404)
  except ValueError as exception:
    return _error(str(exception), 400)
  return web.Response(status=204)


async def _handle_keepalive(request: web.Request) -> web.Response:
  trail_id = request.match_info['trail_id']
  store: TrailsStore = request.app['store']
  try:
    await _dispatch(store.keepalive, trail_id)
  except TrailNotFound:
    return _error(f'trail not found: {trail_id}', 404)
  return web.Response(status=204)


async def _handle_get_trail(request: web.Request) -> web.Response:
  trail_id = request.match_info['trail_id']
  store: TrailsStore = request.app['store']
  try:
    trail = await _dispatch(store.get_trail, trail_id)
  except TrailNotFound:
    return _error(f'trail not found: {trail_id}', 404)
  return web.json_response(trail)


async def _handle_get_context(request: web.Request) -> web.Response:
  trail_id = request.match_info['trail_id']
  store: TrailsStore = request.app['store']
  try:
    context = await _dispatch(store.get_launch_context, trail_id)
  except TrailNotFound:
    return _error(f'trail not found: {trail_id}', 404)
  return web.json_response({'launch_context': context})


async def _handle_get_step(request: web.Request) -> web.Response:
  trail_id = request.match_info['trail_id']
  step_id = _parse_ordinal(request.match_info['step_id'], name='step_id')
  store: TrailsStore = request.app['store']
  try:
    step = await _dispatch(store.get_step, trail_id, step_id)
  except TrailNotFound:
    return _error(f'trail not found: {trail_id}', 404)
  except ValueError as exception:
    return _error(str(exception), 400)
  return web.json_response(step)


async def _handle_get_steps(request: web.Request) -> web.Response:
  trail_id = request.match_info['trail_id']
  after = _parse_optional_ordinal(request.query.get('after'), name='after')
  limit = _parse_limit(request.query.get('limit'), default=100, ceiling=500)
  store: TrailsStore = request.app['store']
  try:
    result = await _dispatch(store.get_steps, trail_id, after=after, limit=limit)
  except TrailNotFound:
    return _error(f'trail not found: {trail_id}', 404)
  except ValueError as exception:
    return _error(str(exception), 400)
  return web.json_response(result)


async def _handle_get_messages(request: web.Request) -> web.Response:
  trail_id = request.match_info['trail_id']
  after = _parse_optional_ordinal(request.query.get('after'), name='after')
  limit = _parse_limit(request.query.get('limit'), default=100, ceiling=500)
  requested_types = set(request.query.getall('type', []))
  unknown = requested_types - MESSAGE_TYPES
  if len(unknown) > 0:
    return _error(f'unknown message types: {sorted(unknown)}', 400)
  store: TrailsStore = request.app['store']
  try:
    result = await _dispatch(
      store.get_messages,
      trail_id,
      after=after,
      limit=limit,
      types=requested_types if len(requested_types) > 0 else None,
    )
  except TrailNotFound:
    return _error(f'trail not found: {trail_id}', 404)
  except ValueError as exception:
    return _error(str(exception), 400)
  return web.json_response(result)


async def _handle_recompute(request: web.Request) -> web.Response:
  trail_id = request.match_info['trail_id']
  admin: DynamoStore = request.app['admin']
  try:
    return web.json_response(await _dispatch(admin.recompute, trail_id))
  except TrailNotFound:
    return _error(f'trail not found: {trail_id}', 404)
  except ValueError as exception:
    return _error(str(exception), 400)


async def _handle_check(request: web.Request) -> web.StreamResponse:
  payload = await _read_json(request)
  if payload is None:
    payload = {}
  if not isinstance(payload, dict) or set(payload) - {'trail_id'}:
    return _error('body must contain only optional trail_id', 400)
  trail_id = payload.get('trail_id')
  if trail_id is not None and (not isinstance(trail_id, str) or len(trail_id) == 0):
    return _error('trail_id must be a non-empty string', 400)
  admin: DynamoStore = request.app['admin']
  try:
    if trail_id is None:
      return await _stream_json_with_heartbeats(request, _dispatch(admin.check))
    return web.json_response(await _dispatch(admin.check, trail_id))
  except TrailNotFound:
    return _error(f'trail not found: {trail_id}', 404)
  except ValueError as exception:
    return _error(str(exception), 400)


async def _handle_relink(request: web.Request) -> web.Response:
  trail_id = request.match_info['trail_id']
  payload = await _read_json(request)
  if not isinstance(payload, dict) or set(payload) != {'forked_from', 'delete_count'}:
    return _error('body must contain forked_from and delete_count', 400)
  pointer_payload = {'forked_from': payload['forked_from']}
  invalid = _pointer(pointer_payload, 'forked_from', step_optional=False)
  if invalid is not None:
    return invalid
  delete_count = payload['delete_count']
  if not isinstance(delete_count, int) or isinstance(delete_count, bool) or delete_count < 0:
    return _error('delete_count must be a non-negative int', 400)
  admin: DynamoStore = request.app['admin']
  try:
    result = await _dispatch(admin.relink, trail_id, payload['forked_from'], delete_count)
    return web.json_response(result)
  except TrailNotFound:
    return _error(f'trail not found: {trail_id}', 404)
  except ValueError as exception:
    return _error(str(exception), 400)


async def _handle_repair_llm_spec(request: web.Request) -> web.Response:
  trail_id = request.match_info['trail_id']
  payload = await _read_json(request)
  if not isinstance(payload, dict) or set(payload) != {'expected', 'replacement'}:
    return _error('body must contain expected and replacement', 400)
  replacement = payload['replacement']
  if not isinstance(replacement, dict):
    return _error('replacement must be an object', 400)
  admin: DynamoStore = request.app['admin']
  try:
    return web.json_response(
      await _dispatch(admin.repair_llm_spec, trail_id, payload['expected'], replacement)
    )
  except TrailNotFound:
    return _error(f'trail not found: {trail_id}', 404)
  except ValueError as exception:
    return _error(str(exception), 409)


async def _handle_list_trails(request: web.Request) -> web.Response:
  harness = request.query.get('harness')
  bro = request.query.get('bro')
  forked_from = request.query.get('forked_from')
  if sum(value is not None for value in (harness, bro, forked_from)) > 1:
    return _error('only one of harness/bro/forked_from may be set', 400)
  store: TrailsStore = request.app['store']
  result = await _dispatch(
    store.list_trails,
    harness=harness,
    bro=bro,
    forked_from=forked_from,
    since=request.query.get('since'),
    until=request.query.get('until'),
    cursor=request.query.get('cursor'),
    limit=_parse_limit(request.query.get('limit'), default=20, ceiling=100),
  )
  return web.json_response(result)


def _parse_ordinal(raw: str, *, name: str) -> int:
  try:
    return int(raw)
  except ValueError as exception:
    raise web.HTTPBadRequest(reason=f'{name} must be an ordinal') from exception


def _parse_optional_ordinal(raw: Optional[str], *, name: str) -> Optional[int]:
  if raw is None:
    return None
  return _parse_ordinal(raw, name=name)


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


async def _sweep_loop(admin: DynamoStore, interval_seconds: float) -> None:
  while True:
    await asyncio.sleep(interval_seconds)
    try:
      swept = await _dispatch(admin.sweep_unreported)
      if len(swept) > 0:
        log.info('sweep inferred %d trails as unreported: %s', len(swept), ', '.join(swept))
    except Exception as exception:
      log.warning('unreported-trail sweep failed: %s', exception)


def create_app(
  store: TrailsStore,
  bearer_token: Optional[str],
  *,
  admin: Optional[DynamoStore] = None,
  sweep_interval_seconds: Optional[float] = None,
) -> web.Application:
  if admin is not None and admin is not store:
    raise ValueError('the trails admin surface must be the hosted store')
  if sweep_interval_seconds is not None and admin is None:
    raise ValueError('a trails sweep requires a DynamoStore admin surface')
  app = web.Application(middlewares=[_auth_middleware], client_max_size=MAX_REQUEST_BYTES)
  app['store'] = store
  app['bearer_token'] = bearer_token
  app.router.add_get('/health', _handle_health)
  app.router.add_post('/v1/trails', _handle_blaze)
  app.router.add_get('/v1/trails', _handle_list_trails)
  app.router.add_get('/v1/trails/{trail_id}', _handle_get_trail)
  app.router.add_patch('/v1/trails/{trail_id}', _handle_set_subject)
  app.router.add_post('/v1/trails/{trail_id}/records', _handle_append_records)
  app.router.add_get('/v1/trails/{trail_id}/steps', _handle_get_steps)
  app.router.add_get('/v1/trails/{trail_id}/steps/{step_id}', _handle_get_step)
  app.router.add_get('/v1/trails/{trail_id}/messages', _handle_get_messages)
  app.router.add_get('/v1/trails/{trail_id}/context', _handle_get_context)
  app.router.add_post('/v1/trails/{trail_id}/end', _handle_end_trail)
  app.router.add_post('/v1/trails/{trail_id}/keepalive', _handle_keepalive)
  if admin is not None:
    app['admin'] = admin
    app.router.add_post('/v1/admin/trails/check', _handle_check)
    app.router.add_post('/v1/admin/trails/{trail_id}/recompute', _handle_recompute)
    app.router.add_post('/v1/admin/trails/{trail_id}/relink', _handle_relink)
    app.router.add_post('/v1/admin/trails/{trail_id}/repair-llm-spec', _handle_repair_llm_spec)
  if sweep_interval_seconds is not None:
    assert admin is not None

    async def _sweep_context(_: web.Application):
      task = asyncio.create_task(_sweep_loop(admin, sweep_interval_seconds))
      yield
      task.cancel()
      with contextlib.suppress(asyncio.CancelledError):
        await task

    app.cleanup_ctx.append(_sweep_context)

  async def _close_store(_: web.Application) -> None:
    await _dispatch(store.close)

  app.on_cleanup.append(_close_store)
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
  parser = base_args.Parser(description='trails recording server')
  parser.add_argument('--host', default='0.0.0.0')
  parser.add_argument('--port', type=int, default=DEFAULT_PORT)
  parser.add_argument('--trails-bearer-token', default=None, secret=True)
  parser.add_argument('--trails-allow-no-auth', action='store_true')
  args = parser.parse(argv)
  bearer_token = resolve_auth(
    bearer_token=args['trails_bearer_token'],
    allow_no_auth=args['trails_allow_no_auth'],
    host=args['host'],
  )
  store = default_store()
  admin = store if isinstance(store, DynamoStore) else None
  auth_description = 'bearer auth' if bearer_token is not None else 'NO AUTH'
  log.info(
    'starting trails server on %s:%s with %s (%s)',
    args['host'],
    args['port'],
    type(store).__name__,
    auth_description,
  )
  web.run_app(
    create_app(
      store,
      bearer_token,
      admin=admin,
      sweep_interval_seconds=SWEEP_INTERVAL_SECONDS if admin is not None else None,
    ),
    host=args['host'],
    port=args['port'],
  )


if __name__ == '__main__':
  sys.exit(main(sys.argv))
