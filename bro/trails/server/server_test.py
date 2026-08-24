import json
import threading
from typing import Any, Optional, cast

import pytest
from aiohttp import web

from bro.trails.local import LocalStore
from bro.trails.model import BlazeRequest, reported_forks, reported_missing_trail
from bro.trails.server.auth import (
  TOKENS_SECRET,
  Permission,
  TokenTable,
  declared_permission,
  resolve_auth,
)
from bro.trails.server.dynamo import DynamoStore
from bro.trails.server.server import HEALTH_PATH, create_app, main

TOKEN = 'secret-test-token'


def _tokens(*permissions: str, name: str = 'test') -> TokenTable:
  return TokenTable.from_config(
    {'tokens': {name: {'token': TOKEN, 'permissions': list(permissions)}}}
  )


FULL_ACCESS = _tokens('read', 'write', 'admin')


def _auth(token: str = TOKEN) -> dict[str, str]:
  return {'Authorization': f'Bearer {token}'}


def _blaze_payload(**overrides) -> dict:
  payload = {
    'harness': 'bro',
    'bro': 'dev',
    'version': '2',
    'interactive': False,
    'surface': 'ask',
    'hold': 'unattended',
    'native': {'llm': {'type': 'openai', 'model': 'gpt-5'}},
    'body': {'records': [{'kind': 'system_prompt', 'body': 'hello'}]},
  }
  payload.update(overrides)
  return payload


@pytest.fixture
def store(tmp_path):
  with LocalStore(tmp_path) as local:
    yield local


@pytest.fixture
def client(aiohttp_client, store):
  return aiohttp_client(create_app(store, FULL_ACCESS))


@pytest.mark.asyncio
async def test_health_needs_no_auth(client):
  response = await (await client).get('/health')
  assert response.status == 200


@pytest.mark.asyncio
async def test_auth_is_required(client):
  response = await (await client).post('/v1/trails', json=_blaze_payload())
  assert response.status == 401


@pytest.mark.asyncio
async def test_a_write_token_records_without_reading(aiohttp_client, store):
  client = await aiohttp_client(create_app(store, _tokens('write')))

  created = await client.post('/v1/trails', json=_blaze_payload(), headers=_auth())
  trail_id = (await created.json())['id']
  listed = await client.get('/v1/trails', headers=_auth())
  read = await client.get(f'/v1/trails/{trail_id}/messages', headers=_auth())
  ended = await client.post(f'/v1/trails/{trail_id}/end', json={'reason': 'ok'}, headers=_auth())

  assert created.status == 201
  assert ended.status == 204
  assert listed.status == 403
  assert read.status == 403
  assert 'may not read' in (await read.json())['error']


@pytest.mark.asyncio
async def test_a_read_token_neither_records_nor_administers(aiohttp_client, store):
  trail_id = store.blaze(BlazeRequest(**_blaze_payload()))['id']
  client = await aiohttp_client(create_app(store, _tokens('read')))

  read = await client.get(f'/v1/trails/{trail_id}', headers=_auth())
  blazed = await client.post('/v1/trails', json=_blaze_payload(), headers=_auth())
  keepalive = await client.post(f'/v1/trails/{trail_id}/keepalive', headers=_auth())
  administered = await client.post('/v1/admin/trails/check', json={}, headers=_auth())

  assert read.status == 200
  assert blazed.status == 403
  assert keepalive.status == 403
  assert administered.status == 403


def test_every_route_but_the_health_probe_declares_a_permission(store):
  app = create_app(store, FULL_ACCESS)

  undeclared = {
    resource.canonical
    for route in app.router.routes()
    if (resource := route.resource) is not None
    and resource.canonical != HEALTH_PATH
    and declared_permission(route.handler) is None
  }

  assert undeclared == set()


@pytest.mark.asyncio
async def test_an_undeclared_route_is_refused_rather_than_served_open(aiohttp_client, store):
  async def undeclared(_: web.Request) -> web.Response:
    return web.json_response({})

  app = create_app(store, FULL_ACCESS)
  app.router.add_get('/v1/undeclared', undeclared)

  response = await (await aiohttp_client(app)).get('/v1/undeclared', headers=_auth())

  assert response.status == 500


@pytest.mark.asyncio
async def test_blaze_dispatches_the_real_store_off_loop(client, store, monkeypatch):
  event_loop_thread = threading.get_ident()
  operation_threads = []
  blaze = store.blaze

  def observed_blaze(request):
    operation_threads.append(threading.get_ident())
    return blaze(request)

  monkeypatch.setattr(store, 'blaze', observed_blaze)
  response = await (await client).post('/v1/trails', json=_blaze_payload(), headers=_auth())

  assert response.status == 201
  trail_id = (await response.json())['id']
  assert store.get_trail(trail_id)['surface'] == 'ask'
  assert len(operation_threads) == 1
  assert operation_threads[0] != event_loop_thread


@pytest.mark.asyncio
@pytest.mark.parametrize(
  'field', ['harness', 'version', 'interactive', 'surface', 'body', 'native']
)
async def test_blaze_requires_universal_fields(client, field):
  payload = _blaze_payload()
  del payload[field]
  response = await (await client).post('/v1/trails', json=payload, headers=_auth())
  assert response.status == 400


@pytest.mark.asyncio
async def test_blaze_validates_lineage_and_harness_data(client):
  client = await client
  invalid_pointer = await client.post(
    '/v1/trails',
    json=_blaze_payload(forked_from={'trail_id': 'parent'}),
    headers=_auth(),
  )
  missing_bro = await client.post(
    '/v1/trails',
    json=_blaze_payload(bro=None),
    headers=_auth(),
  )

  assert invalid_pointer.status == 400
  assert missing_bro.status == 400


@pytest.mark.asyncio
async def test_append_maps_conflicts_and_bad_payloads(client):
  client = await client
  created = await client.post('/v1/trails', json=_blaze_payload(), headers=_auth())
  trail_id = (await created.json())['id']
  appended = await client.post(
    f'/v1/trails/{trail_id}/records',
    json={'offset': 1, 'records': [{'kind': 'user_input', 'body': 'hello'}]},
    headers=_auth(),
  )
  conflict = await client.post(
    f'/v1/trails/{trail_id}/records',
    json={'offset': 0, 'records': [{'kind': 'error', 'body': 'late'}]},
    headers=_auth(),
  )
  malformed = await client.post(
    f'/v1/trails/{trail_id}/records',
    json={'offset': 'one', 'records': []},
    headers=_auth(),
  )

  assert await appended.json() == {'extent': 2, 'appended': 1}
  assert conflict.status == 409
  assert (await conflict.json())['extent'] == 2
  assert malformed.status == 400


@pytest.mark.asyncio
async def test_read_handlers_dispatch_local_store_semantics(client):
  client = await client
  claude = _blaze_payload(
    harness='claude',
    bro=None,
    surface='ride',
    native={'segment': 'segment', 'llm': {}, 'ride_command': 'ride along', 'harness_version': '2'},
    body={
      'records': [
        json.dumps({'type': 'system', 'uuid': 'first'}),
        json.dumps({'type': 'user', 'uuid': 'second', 'message': {'content': 'hello'}}),
      ],
      'launch_context': {'cwd': '/workspace'},
    },
  )
  created = await client.post('/v1/trails', json=claude, headers=_auth())
  trail_id = (await created.json())['id']

  point = await client.get(f'/v1/trails/{trail_id}/steps/1', headers=_auth())
  messages = await client.get(f'/v1/trails/{trail_id}/messages?type=user_input', headers=_auth())
  context = await client.get(f'/v1/trails/{trail_id}/context', headers=_auth())

  assert (await point.json())['uuid'] == 'second'
  assert [message['type'] for message in (await messages.json())['messages']] == ['user_input']
  assert await context.json() == {'launch_context': {'cwd': '/workspace'}}


@pytest.mark.asyncio
async def test_list_filters_and_rejects_invalid_queries(client):
  client = await client
  await client.post('/v1/trails', json=_blaze_payload(), headers=_auth())
  await client.post(
    '/v1/trails',
    json=_blaze_payload(
      harness='claude',
      bro=None,
      surface='ride',
      native={
        'segment': 'segment',
        'llm': {},
        'ride_command': 'ride along',
        'harness_version': '2',
      },
      body={'records': []},
    ),
    headers=_auth(),
  )

  filtered = await client.get('/v1/trails?harness=claude', headers=_auth())
  conflicting = await client.get('/v1/trails?harness=claude&bro=dev', headers=_auth())
  malformed_limit = await client.get('/v1/trails?limit=lots', headers=_auth())
  malformed_step = await client.get('/v1/trails/missing/steps/two', headers=_auth())

  assert [trail['harness'] for trail in (await filtered.json())['trails']] == ['claude']
  assert conflicting.status == 400
  assert malformed_limit.status == 400
  assert malformed_step.status == 400


@pytest.mark.asyncio
async def test_context_distinguishes_absence_from_a_missing_trail(client):
  client = await client
  created = await client.post('/v1/trails', json=_blaze_payload(), headers=_auth())
  trail_id = (await created.json())['id']

  absent = await client.get(f'/v1/trails/{trail_id}/context', headers=_auth())
  missing = await client.get('/v1/trails/missing/context', headers=_auth())

  assert absent.status == 200
  assert await absent.json() == {'launch_context': None}
  assert missing.status == 404


@pytest.mark.asyncio
async def test_header_and_end_handlers_preserve_store_validation(client, store):
  client = await client
  created = await client.post('/v1/trails', json=_blaze_payload(), headers=_auth())
  trail_id = (await created.json())['id']
  updated = await client.patch(
    f'/v1/trails/{trail_id}', json={'subject': 'renamed'}, headers=_auth()
  )
  immutable = await client.patch(
    f'/v1/trails/{trail_id}', json={'surface': 'other'}, headers=_auth()
  )
  ended = await client.post(
    f'/v1/trails/{trail_id}/end',
    json={'reason': 'raised', 'detail': 'missing access'},
    headers=_auth(),
  )
  invalid_end = await client.post(
    f'/v1/trails/{trail_id}/end', json={'reason': 'lost'}, headers=_auth()
  )

  assert (await updated.json())['subject'] == 'renamed'
  assert immutable.status == 400
  assert ended.status == 204
  assert store.get_trail(trail_id)['end']['detail'] == 'missing access'
  assert invalid_end.status == 400


@pytest.mark.asyncio
async def test_missing_resources_are_404_reporting_the_trail(client):
  client = await client
  for path in (
    '/v1/trails/missing',
    '/v1/trails/missing/steps',
    '/v1/trails/missing/steps/0',
    '/v1/trails/missing/context',
  ):
    response = await client.get(path, headers=_auth())
    assert response.status == 404
    assert reported_missing_trail(await response.read()) == 'missing'


@pytest.mark.asyncio
async def test_an_unrouted_path_is_not_a_missing_trail(client):
  response = await (await client).get('/v1/trails/T1/nowhere', headers=_auth())
  assert response.status == 404
  assert reported_missing_trail(await response.read()) is None


@pytest.mark.asyncio
async def test_delete_is_administered_and_reports_a_refusal_as_a_conflict(aiohttp_client, store):
  parent = store.blaze(BlazeRequest(**_blaze_payload()))['id']
  child = store.blaze(
    BlazeRequest(**_blaze_payload(forked_from={'trail_id': parent, 'step_id': 0}))
  )['id']
  full = await aiohttp_client(create_app(store, FULL_ACCESS))
  writer = await aiohttp_client(create_app(store, _tokens('write')))

  unprivileged = await writer.delete(f'/v1/admin/trails/{parent}', headers=_auth())
  forked = await full.delete(f'/v1/admin/trails/{parent}', headers=_auth())
  removed = await full.delete(f'/v1/admin/trails/{child}', headers=_auth())
  missing = await full.delete('/v1/admin/trails/absent', headers=_auth())

  assert unprivileged.status == 403
  assert forked.status == 409
  assert reported_forks(await forked.read()) == [child]
  assert (await removed.json())['trail_id'] == child
  assert missing.status == 404
  assert reported_missing_trail(await missing.read()) == 'absent'


@pytest.mark.asyncio
async def test_admin_routes_report_an_unsupported_backend(client):
  response = await (await client).post('/v1/admin/trails/check', json={}, headers=_auth())
  assert response.status == 501
  assert 'administration surface' in (await response.json())['error']


def test_admin_routes_serve_a_dynamo_backed_store():
  store = DynamoStore(
    dynamo=object(),
    s3=object(),
    trails_table='headers',
    steps_table='steps',
    uuid_index='uuid-index',
    bucket='spill',
  )
  with store:
    app = create_app(store, FULL_ACCESS, admin=store)
    paths = {
      resource.canonical
      for route in app.router.routes()
      if (resource := route.resource) is not None
    }

  assert app['admin'] is store
  assert '/v1/admin/trails/check' in paths
  assert '/v1/admin/trails/{trail_id}/recompute' in paths
  assert '/v1/admin/trails/{trail_id}/relink' in paths


def test_sweep_requires_the_dynamo_admin_surface(store):
  with pytest.raises(ValueError, match='DynamoStore'):
    create_app(store, FULL_ACCESS, sweep_interval_seconds=1)


class _CredentialStore:
  def __init__(self, config: Optional[dict]):
    self._config = config

  def available(self, name: str) -> bool:
    return name == TOKENS_SECRET and self._config is not None

  def get_json(self, name: str) -> dict:
    assert name == TOKENS_SECRET and self._config is not None
    return self._config


def _resolve(config: Optional[dict] = None, **overrides) -> Optional[TokenTable]:
  arguments: dict[str, Any] = {'allow_no_auth': False, 'host': '127.0.0.1'}
  return resolve_auth(cast(Any, _CredentialStore(config)), **{**arguments, **overrides})


def test_resolve_auth_requires_explicit_loopback_override():
  with pytest.raises(RuntimeError, match=TOKENS_SECRET):
    _resolve()
  with pytest.raises(RuntimeError, match='HOST'):
    _resolve(allow_no_auth=True, host='0.0.0.0')
  for host in ('127.0.0.1', 'localhost', '::1'):
    assert _resolve(allow_no_auth=True, host=host) is None


def test_each_token_carries_its_own_permissions():
  tokens = _resolve(
    {
      'tokens': {
        'sessions': {'token': 'w', 'permissions': ['write']},
        'analyst': {'token': 'rw', 'permissions': ['read', 'write']},
        'operator': {'token': 'a', 'permissions': ['admin']},
      }
    }
  )

  assert tokens is not None
  declared = {token.name: token for token in tokens.tokens}

  assert tokens.names() == ('analyst', 'operator', 'sessions')
  assert declared['sessions'].permissions == frozenset({Permission.WRITE})
  assert declared['analyst'].permissions == frozenset({Permission.READ, Permission.WRITE})
  assert tokens.match('Bearer a') is declared['operator']
  assert tokens.match('Bearer other') is None


@pytest.mark.parametrize(
  ('config', 'message'),
  [
    ({'tokens': {}}, 'at least one token'),
    ({'tokens': {'a': {'token': 't', 'permissions': []}}}, 'at least one permission'),
    ({'tokens': {'a': {'token': 't', 'permissions': ['delete']}}}, "names permission 'delete'"),
    ({'tokens': {'a': {'token': '', 'permissions': ['read']}}}, 'non-empty token'),
    ({'tokens': {'a': {'token': 't'}}}, 'only `token` and `permissions`'),
    (
      {
        'tokens': {
          'a': {'token': 't', 'permissions': ['read']},
          'b': {'token': 't', 'permissions': ['write']},
        }
      },
      'under several names',
    ),
    ({'tokens': {}, 'extra': 1}, 'only `tokens`'),
  ],
)
def test_a_malformed_token_table_is_refused(config, message):
  with pytest.raises(ValueError, match=message):
    TokenTable.from_config(config)


def test_main_resolves_the_hosted_store_from_credentials(monkeypatch, store):
  launched = {}
  monkeypatch.setattr('bro.trails.server.server.configured_store', lambda: store)
  monkeypatch.setattr(
    'bro.trails.server.server.web.run_app',
    lambda app, *, host, port: launched.update(app=app, host=host, port=port),
  )

  main(['trails-server', '--host', '127.0.0.1', '--port', '9000', '--trails-allow-no-auth'])

  assert launched['host'] == '127.0.0.1'
  assert launched['port'] == 9000
  assert launched['app']['store'] is store
