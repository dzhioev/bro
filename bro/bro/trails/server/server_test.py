import asyncio

import pytest

from trails.server import storage
from trails.server.server import create_app, resolve_auth

TOKEN = 'secret-test-token'


def _auth(token: str = TOKEN) -> dict[str, str]:
  return {'Authorization': f'Bearer {token}'}


def _create_payload(**overrides) -> dict:
  payload = {
    'harness': 'bro',
    'bro': 'ppp-dev',
    'version': '2',
    'interactive': False,
    'surface': 'ask',
    'hold': 'unattended',
    'native': {'llm': {'type': 'chat_gpt', 'model': 'gpt-5'}},
    'body': {'system_prompt': 'hello'},
  }
  payload.update(overrides)
  return payload


class FakeStorage:
  def __init__(self):
    self.trails: dict[str, dict] = {}
    self.steps: dict[str, list[dict]] = {}
    self.artifacts: dict[str, str] = {}
    self._counter = 0
    self.raise_body_too_large = False
    self.sweep_calls = 0

  def _new_id(self) -> str:
    self._counter += 1
    return f'id-{self._counter}'

  def _now(self) -> str:
    return f'2026-06-07T00:00:{self._counter:02d}.000000Z'

  async def create_trail(self, **payload):
    trail_id = payload.get('trail_id') or self._new_id()
    started_at = self._now()
    native = payload.get('native') or {}
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
    if payload['harness'] == 'bro':
      self.steps[trail_id].append(
        {
          'trail_id': trail_id,
          'step_id': self._new_id(),
          'ts': started_at,
          'kind': 'system_prompt',
          'body': payload['body']['system_prompt'],
        }
      )
    return {'id': trail_id, 'started_at': started_at}

  async def put_step(self, *, trail_id, kind, body, extras, step_id=None):
    if self.raise_body_too_large:
      raise storage.BodyTooLarge('too big')
    if trail_id not in self.trails:
      raise storage.TrailNotFound(trail_id)
    self.steps[trail_id].append(
      {
        'trail_id': trail_id,
        'step_id': step_id if step_id is not None else self._new_id(),
        'ts': self._now(),
        'kind': kind,
        'body': body,
        **extras,
      }
    )
    return {}

  async def replace_artifact(self, trail_id, artifact, metadata):
    if trail_id not in self.trails:
      raise storage.TrailNotFound(trail_id)
    if self.trails[trail_id]['harness'] != 'claude':
      raise ValueError('artifact replacement is available only for claude trails')
    self.artifacts[trail_id] = artifact
    updates = {
      'line_count': len(artifact.splitlines()),
      'size_bytes': len(artifact.encode()),
      **metadata,
    }
    self.trails[trail_id]['native'].update(updates)
    return updates

  async def update_header(self, trail_id, changes):
    if trail_id not in self.trails:
      raise storage.TrailNotFound(trail_id)
    if set(changes) - {'subject', 'last_alive_at', 'turn_count', 'native'}:
      raise ValueError('immutable or unknown header fields')
    self.trails[trail_id].update({key: value for key, value in changes.items() if key != 'native'})
    self.trails[trail_id]['native'].update(changes.get('native', {}))
    return self.trails[trail_id]

  async def end_trail(self, *, trail_id, reason, detail, step_id=None):
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

  async def sweep_lost(self):
    self.sweep_calls += 1
    return []

  async def get_trail(self, trail_id):
    return self.trails.get(trail_id)

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
  response = await (await client).post('/v1/trails', json=_create_payload())
  assert response.status == 401


@pytest.mark.asyncio
async def test_create_bro_trail(client, store):
  response = await (await client).post('/v1/trails', json=_create_payload(), headers=_auth())
  assert response.status == 201
  trail_id = (await response.json())['id']
  assert store.trails[trail_id]['harness'] == 'bro'
  assert store.trails[trail_id]['surface'] == 'ask'
  assert store.steps[trail_id][0]['kind'] == 'system_prompt'


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['harness', 'version', 'interactive', 'surface', 'body'])
async def test_create_requires_universal_fields(client, field):
  payload = _create_payload()
  del payload[field]
  response = await (await client).post('/v1/trails', json=payload, headers=_auth())
  assert response.status == 400


@pytest.mark.asyncio
async def test_create_validates_lineage_and_provenance(client):
  cli = await client
  response = await cli.post(
    '/v1/trails',
    json=_create_payload(forked_from={'trail_id': 'parent'}),
    headers=_auth(),
  )
  assert response.status == 400
  response = await cli.post(
    '/v1/trails',
    json=_create_payload(summoned_by={'trail_id': 'parent'}),
    headers=_auth(),
  )
  assert response.status == 201


@pytest.mark.asyncio
async def test_step_append_and_native_read(client, store):
  cli = await client
  created = await cli.post('/v1/trails', json=_create_payload(), headers=_auth())
  trail_id = (await created.json())['id']
  response = await cli.post(
    f'/v1/trails/{trail_id}/steps',
    json={'kind': 'user_input', 'body': 'hello', 'step_id': 'user-1'},
    headers=_auth(),
  )
  assert response.status == 204
  response = await cli.get(f'/v1/trails/{trail_id}/steps', headers=_auth())
  assert [item['kind'] for item in (await response.json())['steps']] == [
    'system_prompt',
    'user_input',
  ]
  assert store.steps[trail_id][-1]['step_id'] == 'user-1'


@pytest.mark.asyncio
async def test_messages_support_repeated_type_filter(client):
  cli = await client
  created = await cli.post('/v1/trails', json=_create_payload(), headers=_auth())
  trail_id = (await created.json())['id']
  for kind in ('user_input', 'reasoning', 'assistant'):
    await cli.post(
      f'/v1/trails/{trail_id}/steps',
      json={'kind': kind, 'body': kind},
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
async def test_claude_artifact_replace(client, store):
  cli = await client
  payload = _create_payload(
    harness='claude',
    bro=None,
    surface='cw',
    native={'segment': 'uuid', 'llm': {}, 'cw_command': 'cw ss', 'harness_version': '2.1.0'},
    body={'artifact': '', 'launch_context': {'command': 'cw ss'}},
  )
  created = await cli.post('/v1/trails', json=payload, headers=_auth())
  trail_id = (await created.json())['id']
  response = await cli.put(
    f'/v1/trails/{trail_id}/artifact',
    json={'artifact': '{}\n{}\n', 'native': {'harness_version': '2.1.0'}},
    headers=_auth(),
  )
  assert response.status == 200
  assert store.artifacts[trail_id] == '{}\n{}\n'
  assert store.trails[trail_id]['native']['line_count'] == 2


@pytest.mark.asyncio
async def test_constrained_header_upsert(client, store):
  cli = await client
  created = await cli.post('/v1/trails', json=_create_payload(), headers=_auth())
  trail_id = (await created.json())['id']
  response = await cli.patch(f'/v1/trails/{trail_id}', json={'subject': 'renamed'}, headers=_auth())
  assert response.status == 200
  assert store.trails[trail_id]['subject'] == 'renamed'
  response = await cli.patch(f'/v1/trails/{trail_id}', json={'surface': 'other'}, headers=_auth())
  assert response.status == 400


@pytest.mark.asyncio
async def test_end_records_map_and_detail(client, store):
  cli = await client
  created = await cli.post('/v1/trails', json=_create_payload(), headers=_auth())
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
  created = await cli.post('/v1/trails', json=_create_payload(), headers=_auth())
  trail_id = (await created.json())['id']
  response = await cli.post(f'/v1/trails/{trail_id}/end', json={'reason': reason}, headers=_auth())
  assert response.status == 400


@pytest.mark.asyncio
async def test_list_filters_by_harness(client):
  cli = await client
  await cli.post('/v1/trails', json=_create_payload(), headers=_auth())
  await cli.post(
    '/v1/trails',
    json=_create_payload(
      harness='claude',
      bro=None,
      surface='cw',
      native={'segment': 'uuid', 'llm': {}, 'cw_command': 'cw ss', 'harness_version': '2.1.0'},
      body={'artifact': ''},
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
