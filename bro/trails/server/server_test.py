import asyncio
import json

import pytest

from bro.trails import backends
from bro.trails.model import BlazeRequest, validate_end
from bro.trails.server import storage
from bro.trails.server.server import create_app, resolve_auth

TOKEN = 'secret-test-token'


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
    'native': {'llm': {'type': 'chat_gpt', 'model': 'gpt-5'}},
    'body': {'records': [{'kind': 'system_prompt', 'body': 'hello'}]},
  }
  payload.update(overrides)
  return payload


class FakeStorage:
  def __init__(self):
    self.trails: dict[str, dict] = {}
    self.steps: dict[str, list[dict]] = {}
    self.contexts: dict[str, object] = {}
    self._counter = 0
    self.raise_body_too_large = False
    self.check_delay_seconds = 0.0
    self.sweep_calls = 0

  def _new_id(self) -> str:
    self._counter += 1
    return f'id-{self._counter}'

  def _now(self) -> str:
    return f'2026-06-07T00:00:{self._counter:02d}.000000Z'

  async def blaze(self, request: BlazeRequest):
    adapter = backends.BACKENDS[request.harness]
    adapter.validate_create(request.native)
    if request.harness == 'bro' and request.bro is None:
      raise ValueError('bro is required for the bro harness')
    payload = request.to_wire()
    trail_id = self._new_id()
    started_at = self._now()
    native = request.native
    header = {
      'id': trail_id,
      'harness': payload['harness'],
      'version': payload['version'],
      'started_at': started_at,
      'end': None,
      'last_alive_at': started_at,
      'interactive': payload['interactive'],
      'surface': payload['surface'],
      'turn_count': 0,
      'native': native,
      'usage': {},
      'models': [],
    }
    for field in ('bro', 'hold', 'forked_from', 'summoned_by', 'subject', 'location'):
      if payload.get(field) is not None:
        header[field] = payload[field]
    self.trails[trail_id] = header
    self.steps[trail_id] = []
    if 'launch_context' in payload.get('body', {}):
      self.contexts[trail_id] = payload['body']['launch_context']
    for record in payload['body'].get('records', []):
      self.steps[trail_id].append(
        {
          'trail_id': trail_id,
          'step_id': len(self.steps[trail_id]),
          'ts': started_at,
          **record,
        }
      )
    return {'id': trail_id, 'started_at': started_at}

  async def append_records(self, trail_id, *, offset, records, tools):
    if trail_id not in self.trails:
      raise storage.TrailNotFound(trail_id)
    extent = len(self.steps[trail_id])
    if extent != offset:
      if extent == offset + len(records):
        return {'extent': extent, 'appended': 0, 'duplicate': True}
      raise storage.AppendConflict(offset, extent)
    for record in records:
      self.steps[trail_id].append(
        {
          'trail_id': trail_id,
          'step_id': len(self.steps[trail_id]),
          'ts': self._now(),
          **record,
        }
      )
    self.trails[trail_id]['extent'] = len(self.steps[trail_id])
    return {'extent': len(self.steps[trail_id]), 'appended': len(records)}

  async def recompute(self, trail_id):
    if trail_id not in self.trails:
      raise storage.TrailNotFound(trail_id)
    return {'trail_id': trail_id, 'extent': len(self.steps[trail_id])}

  async def check(self, trail_id=None):
    await asyncio.sleep(self.check_delay_seconds)
    return {'ok': True, 'trails': [] if trail_id is None else [{'trail_id': trail_id, 'ok': True}]}

  async def relink(self, trail_id, forked_from, delete_count):
    if trail_id not in self.trails:
      raise storage.TrailNotFound(trail_id)
    self.trails[trail_id]['forked_from'] = forked_from
    self.steps[trail_id] = self.steps[trail_id][delete_count:]
    return {'trail_id': trail_id, 'forked_from': forked_from, 'extent': len(self.steps[trail_id])}

  async def update_header(self, trail_id, changes):
    if trail_id not in self.trails:
      raise storage.TrailNotFound(trail_id)
    if set(changes) - {'subject', 'last_alive_at', 'turn_count', 'native'}:
      raise ValueError('immutable or unknown header fields')
    self.trails[trail_id].update({key: value for key, value in changes.items() if key != 'native'})
    self.trails[trail_id]['native'].update(changes.get('native', {}))
    return self.trails[trail_id]

  async def end_trail(self, *, trail_id, reason, detail):
    validate_end(reason, detail)
    if trail_id not in self.trails:
      raise storage.TrailNotFound(trail_id)
    end = {'at': self._now(), 'reason': reason}
    if detail is not None:
      end['detail'] = detail
    self.trails[trail_id]['end'] = end
    return {}

  async def keepalive(self, trail_id):
    if trail_id not in self.trails:
      raise storage.TrailNotFound(trail_id)
    self.trails[trail_id]['last_alive_at'] = self._now()
    return {}

  async def get_launch_context(self, trail_id):
    if trail_id not in self.trails:
      raise storage.TrailNotFound(trail_id)
    return self.contexts.get(trail_id)

  async def sweep_unreported(self):
    self.sweep_calls += 1
    return []

  async def get_trail(self, trail_id):
    return self.trails.get(trail_id)

  async def find_steps_by_uuid(self, uuids):
    return [
      {'trail_id': trail_id, 'step_id': step['step_id'], 'uuid': step['uuid']}
      for trail_id, steps in self.steps.items()
      for step in steps
      if step.get('uuid') in uuids
    ]

  async def get_step(self, trail_id, step_id):
    if trail_id not in self.trails:
      raise storage.TrailNotFound(trail_id)
    return next(
      (step for step in self.steps[trail_id] if step['step_id'] == step_id),
      None,
    )

  async def query_step_uuids(self, trail_id, *, through):
    if trail_id not in self.trails:
      raise storage.TrailNotFound(trail_id)
    return [
      {'step_id': step['step_id'], 'uuid': step['uuid']}
      for step in self.steps[trail_id]
      if 'uuid' in step and (through is None or step['step_id'] <= through)
    ]

  async def query_steps(self, trail_id, *, after, limit):
    if trail_id not in self.trails:
      raise storage.TrailNotFound(trail_id)
    items = self.steps[trail_id]
    start = 0
    if after is not None:
      start = next(index for index, item in enumerate(items) if item['step_id'] == after) + 1
    page = items[start : start + limit]
    next_cursor = page[-1]['step_id'] if start + limit < len(items) else None
    return {'steps': page, 'next': next_cursor}

  async def query_messages(self, trail_id, *, after, limit, types):
    page = await self.query_steps(trail_id, after=after, limit=limit)
    messages = [
      {
        'type': item['kind'],
        'ts': item['ts'],
        'source': {'step_id': item['step_id'], 'index': 0},
        'content': item.get('body'),
      }
      for item in page['steps']
    ]
    if types is not None:
      messages = [message for message in messages if message['type'] in types]
    return {'messages': messages, 'next': page['next']}

  async def list_trails(self, *, harness, bro, forked_from, since, until, cursor, limit):
    items = list(self.trails.values())
    if harness is not None:
      items = [item for item in items if item['harness'] == harness]
    if bro is not None:
      items = [item for item in items if item.get('bro') == bro]
    if forked_from is not None:
      items = [item for item in items if item.get('forked_from', {}).get('trail_id') == forked_from]
    items.sort(key=lambda item: item['started_at'], reverse=True)
    return {'trails': items[:limit], 'next': None}


@pytest.fixture
def store():
  return FakeStorage()


@pytest.fixture
def client(aiohttp_client, store):
  return aiohttp_client(create_app(store, TOKEN))


@pytest.mark.asyncio
async def test_health_needs_no_auth(client):
  response = await (await client).get('/health')
  assert response.status == 200


@pytest.mark.asyncio
async def test_auth_is_required(client):
  response = await (await client).post('/v1/trails', json=_blaze_payload())
  assert response.status == 401


@pytest.mark.asyncio
async def test_blaze_bro_trail(client, store):
  response = await (await client).post('/v1/trails', json=_blaze_payload(), headers=_auth())
  assert response.status == 201
  trail_id = (await response.json())['id']
  assert store.trails[trail_id]['harness'] == 'bro'
  assert store.trails[trail_id]['surface'] == 'ask'
  assert store.steps[trail_id][0]['kind'] == 'system_prompt'


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
async def test_blaze_validates_lineage_and_provenance(client):
  cli = await client
  response = await cli.post(
    '/v1/trails',
    json=_blaze_payload(forked_from={'trail_id': 'parent'}),
    headers=_auth(),
  )
  assert response.status == 400
  response = await cli.post(
    '/v1/trails',
    json=_blaze_payload(forked_from={'trail_id': 'parent', 'step_id': '4'}),
    headers=_auth(),
  )
  assert response.status == 400
  response = await cli.post(
    '/v1/trails',
    json=_blaze_payload(summoned_by={'trail_id': 'parent'}),
    headers=_auth(),
  )
  assert response.status == 201


@pytest.mark.asyncio
async def test_universal_append_and_extent_conflict(client):
  cli = await client
  created = await cli.post('/v1/trails', json=_blaze_payload(), headers=_auth())
  trail_id = (await created.json())['id']
  response = await cli.post(
    f'/v1/trails/{trail_id}/records',
    json={'offset': 1, 'records': [{'kind': 'user_input', 'body': 'hello'}]},
    headers=_auth(),
  )
  assert response.status == 200
  assert await response.json() == {'extent': 2, 'appended': 1}
  response = await cli.post(
    f'/v1/trails/{trail_id}/records',
    json={'offset': 0, 'records': [{'kind': 'error', 'body': 'late'}]},
    headers=_auth(),
  )
  assert response.status == 409
  assert (await response.json())['extent'] == 2


@pytest.mark.asyncio
async def test_admin_operations_and_indexed_pointer(client):
  cli = await client
  created = await cli.post(
    '/v1/trails',
    json=_blaze_payload(forked_from={'trail_id': 'parent', 'step_id': 4, 'index': 2}),
    headers=_auth(),
  )
  assert created.status == 201
  trail_id = (await created.json())['id']
  response = await cli.post(f'/v1/admin/trails/{trail_id}/recompute', json={}, headers=_auth())
  assert response.status == 200
  response = await cli.post('/v1/admin/trails/check', json={'trail_id': trail_id}, headers=_auth())
  assert (await response.json())['ok'] is True


@pytest.mark.asyncio
async def test_store_check_streams_heartbeats_then_one_json_verdict(client, store, monkeypatch):
  monkeypatch.setattr('bro.trails.server.server.CHECK_HEARTBEAT_INTERVAL_SECONDS', 0.001)
  store.check_delay_seconds = 0.02

  response = await (await client).post('/v1/admin/trails/check', json={}, headers=_auth())
  body = await response.read()

  assert response.status == 200
  assert body.startswith(b'\n')
  assert json.loads(body) == {'ok': True, 'trails': []}


@pytest.mark.asyncio
async def test_messages_support_repeated_type_filter(client):
  cli = await client
  created = await cli.post('/v1/trails', json=_blaze_payload(), headers=_auth())
  trail_id = (await created.json())['id']
  await cli.post(
    f'/v1/trails/{trail_id}/records',
    json={
      'offset': 1,
      'records': [
        {'kind': kind, 'body': kind} for kind in ('user_input', 'reasoning', 'assistant')
      ],
    },
    headers=_auth(),
  )
  response = await cli.get(
    f'/v1/trails/{trail_id}/messages?type=user_input&type=assistant', headers=_auth()
  )
  assert [item['type'] for item in (await response.json())['messages']] == [
    'user_input',
    'assistant',
  ]


@pytest.mark.asyncio
async def test_uuid_lookup_returns_only_step_identities(client, store):
  store.steps['trail-1'] = [
    {'trail_id': 'trail-1', 'step_id': 0, 'uuid': 'wanted', 'body': 'not returned'},
    {'trail_id': 'trail-1', 'step_id': 1, 'uuid': 'other'},
  ]
  response = await (await client).get('/v1/steps?uuid=wanted', headers=_auth())
  assert response.status == 200
  assert await response.json() == {
    'steps': [{'trail_id': 'trail-1', 'step_id': 0, 'uuid': 'wanted'}]
  }


@pytest.mark.asyncio
async def test_uuid_lookup_requires_a_bounded_nonempty_query(client):
  cli = await client
  assert (await cli.get('/v1/steps', headers=_auth())).status == 400
  assert (await cli.get('/v1/steps?uuid=', headers=_auth())).status == 400


@pytest.mark.asyncio
async def test_point_step_and_uuid_projection_reads(client, store):
  store.trails['trail-1'] = {'id': 'trail-1'}
  store.steps['trail-1'] = [
    {'trail_id': 'trail-1', 'step_id': 0, 'uuid': 'first', 'body': 'one'},
    {'trail_id': 'trail-1', 'step_id': 1, 'uuid': 'second', 'body': 'two'},
  ]
  cli = await client
  point = await cli.get('/v1/trails/trail-1/steps/1', headers=_auth())
  assert point.status == 200
  assert (await point.json())['body'] == 'two'
  projected = await cli.get('/v1/trails/trail-1/steps/uuids?through=0', headers=_auth())
  assert projected.status == 200
  assert await projected.json() == {'steps': [{'step_id': 0, 'uuid': 'first'}]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
  'path',
  [
    '/v1/trails/trail-1/steps/two',
    '/v1/trails/trail-1/steps?after=two',
    '/v1/trails/trail-1/steps/uuids?through=two',
    '/v1/trails/trail-1/messages?after=two',
  ],
)
async def test_step_selectors_reject_non_ordinals(client, store, path):
  store.trails['trail-1'] = {'id': 'trail-1'}
  store.steps['trail-1'] = []
  assert (await (await client).get(path, headers=_auth())).status == 400


@pytest.mark.asyncio
async def test_launch_context_read(client):
  cli = await client
  payload = _blaze_payload(
    harness='claude',
    bro=None,
    surface='cw',
    native={'segment': 'uuid', 'llm': {}, 'cw_command': 'cw ss', 'harness_version': '2.1.0'},
    body={'records': [], 'launch_context': [{'title': 'git state'}]},
  )
  created = await cli.post('/v1/trails', json=payload, headers=_auth())
  trail_id = (await created.json())['id']
  response = await cli.get(f'/v1/trails/{trail_id}/context', headers=_auth())
  assert response.status == 200
  assert await response.json() == {'launch_context': [{'title': 'git state'}]}


@pytest.mark.asyncio
async def test_launch_context_absent_is_404(client):
  cli = await client
  created = await cli.post('/v1/trails', json=_blaze_payload(), headers=_auth())
  trail_id = (await created.json())['id']
  response = await cli.get(f'/v1/trails/{trail_id}/context', headers=_auth())
  assert response.status == 404
  response = await cli.get('/v1/trails/nope/context', headers=_auth())
  assert response.status == 404


@pytest.mark.asyncio
async def test_constrained_header_upsert(client, store):
  cli = await client
  created = await cli.post('/v1/trails', json=_blaze_payload(), headers=_auth())
  trail_id = (await created.json())['id']
  response = await cli.patch(f'/v1/trails/{trail_id}', json={'subject': 'renamed'}, headers=_auth())
  assert response.status == 200
  assert store.trails[trail_id]['subject'] == 'renamed'
  response = await cli.patch(f'/v1/trails/{trail_id}', json={'surface': 'other'}, headers=_auth())
  assert response.status == 400


@pytest.mark.asyncio
async def test_end_records_map_and_detail(client, store):
  cli = await client
  created = await cli.post('/v1/trails', json=_blaze_payload(), headers=_auth())
  trail_id = (await created.json())['id']
  response = await cli.post(
    f'/v1/trails/{trail_id}/end',
    json={'reason': 'raised', 'detail': 'missing access'},
    headers=_auth(),
  )
  assert response.status == 204
  assert store.trails[trail_id]['end']['reason'] == 'raised'
  assert store.trails[trail_id]['end']['detail'] == 'missing access'


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['terminal', 'lost', 'whatever'])
async def test_end_rejects_non_writer_reasons(client, reason):
  cli = await client
  created = await cli.post('/v1/trails', json=_blaze_payload(), headers=_auth())
  trail_id = (await created.json())['id']
  response = await cli.post(f'/v1/trails/{trail_id}/end', json={'reason': reason}, headers=_auth())
  assert response.status == 400


@pytest.mark.asyncio
async def test_list_filters_by_harness(client):
  cli = await client
  await cli.post('/v1/trails', json=_blaze_payload(), headers=_auth())
  await cli.post(
    '/v1/trails',
    json=_blaze_payload(
      harness='claude',
      bro=None,
      surface='cw',
      native={'segment': 'uuid', 'llm': {}, 'cw_command': 'cw ss', 'harness_version': '2.1.0'},
      body={'records': []},
    ),
    headers=_auth(),
  )
  response = await cli.get('/v1/trails?harness=claude', headers=_auth())
  assert [item['harness'] for item in (await response.json())['trails']] == ['claude']


@pytest.mark.asyncio
async def test_malformed_limit_is_rejected(client):
  response = await (await client).get('/v1/trails?limit=lots', headers=_auth())
  assert response.status == 400


@pytest.mark.asyncio
async def test_sweep_loop_runs(aiohttp_client, store):
  await aiohttp_client(create_app(store, TOKEN, sweep_interval_seconds=0.01))
  deadline = asyncio.get_running_loop().time() + 1
  while store.sweep_calls == 0 and asyncio.get_running_loop().time() < deadline:
    await asyncio.sleep(0.01)
  assert store.sweep_calls > 0


def test_resolve_auth_requires_explicit_loopback_override():
  with pytest.raises(RuntimeError, match='TRAILS_BEARER_TOKEN'):
    resolve_auth(None, False, '127.0.0.1')
  with pytest.raises(RuntimeError, match='HOST'):
    resolve_auth(None, True, '0.0.0.0')
  assert resolve_auth(None, True, '127.0.0.1') is None
