import pytest

from trails.server import storage
from trails.server.server import create_app, resolve_auth

TOKEN = 'secret-test-token'


def _auth(token: str = TOKEN) -> dict[str, str]:
  return {'Authorization': f'Bearer {token}'}


class FakeStorage:
  """in-memory storage matching the surface of `storage.Storage`.

  Reproduces the contract handlers depend on (returns + raises), not the
  DynamoDB / S3 mechanics. The real Storage gets exercised against a deployed
  stack via the curl smoke test in the task's exit criteria.
  """

  def __init__(self):
    self.trails: dict[str, dict] = {}
    self.steps: dict[str, list[dict]] = {}
    self._counter = 0
    self.raise_body_too_large = False

  def _new_id(self) -> str:
    self._counter += 1
    return f'01ID{self._counter:022d}'

  def _now(self) -> str:
    return f'2026-06-07T00:00:{self._counter:02d}.000000Z'

  async def create_trail(
    self, *, bro, bro_version, llm_spec, system_prompt, parent, interactive, entry_point
  ):
    trail_id = self._new_id()
    started_at = self._now()
    self.trails[trail_id] = {
      'trail_id': trail_id,
      'bro': bro,
      'bro_version': bro_version,
      'llm_spec': llm_spec,
      'started_at': started_at,
      'ended_at': None,
      'end_reason': None,
      'interactive': interactive,
      'entry_point': entry_point,
      'parent': parent,
      'continuation': None,
      'aggregates': {
        'turn_count': 0,
        'tool_call_count': 0,
        'tokens_in': 0,
        'tokens_out': 0,
        'tokens_reasoning': 0,
        'step_counts_by_kind': {k: 0 for k in storage.STEP_KINDS} | {'system_prompt': 1},
      },
    }
    self.steps[trail_id] = [
      {
        'trail_id': trail_id,
        'step_id': self._new_id(),
        'ts': started_at,
        'kind': 'system_prompt',
        'body': system_prompt,
        'turn_index': 0,
      }
    ]
    return {'trail_id': trail_id, 'started_at': started_at}

  async def put_step(self, *, trail_id, kind, body, extras):
    if self.raise_body_too_large:
      raise storage.BodyTooLarge('too big')
    if trail_id not in self.trails:
      raise storage.TrailNotFound(trail_id)
    step_id = self._new_id()
    ts = self._now()
    self.steps[trail_id].append(
      {'trail_id': trail_id, 'step_id': step_id, 'ts': ts, 'kind': kind, 'body': body, **extras}
    )
    counts = self.trails[trail_id]['aggregates']['step_counts_by_kind']
    counts[kind] = counts.get(kind, 0) + 1
    return {'step_id': step_id, 'ts': ts}

  async def end_trail(self, *, trail_id, reason, continuation):
    if trail_id not in self.trails:
      raise storage.TrailNotFound(trail_id)
    ts = self._now()
    self.trails[trail_id]['ended_at'] = ts
    self.trails[trail_id]['end_reason'] = reason
    if continuation is not None:
      self.trails[trail_id]['continuation'] = continuation
    self.steps[trail_id].append(
      {
        'trail_id': trail_id,
        'step_id': self._new_id(),
        'ts': ts,
        'kind': 'end',
        'body': {'reason': reason},
      }
    )
    return {'ended_at': ts}

  async def get_trail(self, trail_id):
    return self.trails.get(trail_id)

  async def query_steps(self, trail_id, *, after, limit):
    items = self.steps.get(trail_id, [])
    if after is not None:
      after_idx = next((i for i, s in enumerate(items) if s['step_id'] == after), -1)
      items = items[after_idx + 1 :]
    truncated = items[:limit]
    next_cursor = truncated[-1]['step_id'] if len(items) > limit else None
    return {'steps': truncated, 'next': next_cursor}

  async def list_trails(self, *, bro, parent, since, until, cursor, limit):
    items = list(self.trails.values())
    if bro is not None:
      items = [t for t in items if t['bro'] == bro]
    if parent is not None:
      items = [
        t for t in items if t.get('parent') is not None and t['parent']['trail_id'] == parent
      ]
    if since is not None:
      items = [t for t in items if t['started_at'] >= since]
    if until is not None:
      items = [t for t in items if t['started_at'] <= until]
    items.sort(key=lambda t: t['started_at'], reverse=True)
    start = 0
    if cursor is not None:
      start = next((i for i, t in enumerate(items) if t['trail_id'] == cursor), -1) + 1
    truncated = items[start : start + limit]
    next_cursor = truncated[-1]['trail_id'] if len(items) - start > limit else None
    return {'trails': truncated, 'next': next_cursor}


@pytest.fixture
def store():
  return FakeStorage()


@pytest.fixture
def client(aiohttp_client, store):
  return aiohttp_client(create_app(store, TOKEN))


def _create_payload(**overrides) -> dict:
  payload = {
    'bro': 'ppp-dev',
    'bro_version': 1,
    'llm_spec': {'type': 'chat_gpt', 'model': 'gpt-5'},
    'system_prompt': 'hello',
    'interactive': False,
    'entry_point': 'cli:bro_run',
  }
  payload.update(overrides)
  return payload


class TestHealth:
  @pytest.mark.asyncio
  async def test_no_auth_required(self, client):
    cli = await client
    resp = await cli.get('/health')
    assert resp.status == 200
    assert (await resp.json()) == {'status': 'ok'}


class TestAuth:
  @pytest.mark.asyncio
  async def test_missing_token_rejected(self, client):
    cli = await client
    resp = await cli.post('/v1/trails', json=_create_payload())
    assert resp.status == 401

  @pytest.mark.asyncio
  async def test_wrong_token_rejected(self, client):
    cli = await client
    resp = await cli.post('/v1/trails', json=_create_payload(), headers=_auth('nope'))
    assert resp.status == 401

  @pytest.mark.asyncio
  async def test_correct_token_accepted(self, client):
    cli = await client
    resp = await cli.post('/v1/trails', json=_create_payload(), headers=_auth())
    assert resp.status == 201


class TestCreateTrail:
  @pytest.mark.asyncio
  async def test_happy_path_returns_trail_id(self, client, store):
    cli = await client
    resp = await cli.post('/v1/trails', json=_create_payload(), headers=_auth())
    assert resp.status == 201
    data = await resp.json()
    assert data['trail_id'] in store.trails

  @pytest.mark.asyncio
  async def test_system_prompt_emitted_as_first_step(self, client, store):
    cli = await client
    resp = await cli.post(
      '/v1/trails',
      json=_create_payload(system_prompt='you are a bro'),
      headers=_auth(),
    )
    trail_id = (await resp.json())['trail_id']
    steps = store.steps[trail_id]
    assert len(steps) == 1
    assert steps[0]['kind'] == 'system_prompt'
    assert steps[0]['body'] == 'you are a bro'
    assert steps[0]['turn_index'] == 0

  @pytest.mark.asyncio
  async def test_missing_field_rejected(self, client):
    cli = await client
    payload = _create_payload()
    del payload['bro']
    resp = await cli.post('/v1/trails', json=payload, headers=_auth())
    assert resp.status == 400

  @pytest.mark.asyncio
  async def test_invalid_json_rejected(self, client):
    cli = await client
    resp = await cli.post(
      '/v1/trails',
      data='not json',
      headers={**_auth(), 'Content-Type': 'application/json'},
    )
    assert resp.status == 400

  @pytest.mark.asyncio
  async def test_parent_required_fields_validated(self, client):
    cli = await client
    payload = _create_payload(parent={'trail_id': 't1', 'step_id': 's1'})
    resp = await cli.post('/v1/trails', json=payload, headers=_auth())
    assert resp.status == 400

  @pytest.mark.asyncio
  async def test_parent_accepted_when_complete(self, client, store):
    cli = await client
    parent = {'trail_id': 't1', 'step_id': 's1', 'relationship': 'fork'}
    resp = await cli.post('/v1/trails', json=_create_payload(parent=parent), headers=_auth())
    assert resp.status == 201
    trail_id = (await resp.json())['trail_id']
    assert store.trails[trail_id]['parent'] == parent


class TestPutStep:
  async def _make_trail(self, cli) -> str:
    resp = await cli.post('/v1/trails', json=_create_payload(), headers=_auth())
    return (await resp.json())['trail_id']

  @pytest.mark.asyncio
  async def test_happy_path_returns_204(self, client, store):
    cli = await client
    trail_id = await self._make_trail(cli)
    resp = await cli.post(
      f'/v1/trails/{trail_id}/steps',
      json={'kind': 'user_input', 'body': 'hello', 'turn_index': 0},
      headers=_auth(),
    )
    assert resp.status == 204
    assert store.steps[trail_id][-1]['kind'] == 'user_input'
    assert store.steps[trail_id][-1]['body'] == 'hello'

  @pytest.mark.asyncio
  async def test_unknown_trail_404(self, client):
    cli = await client
    resp = await cli.post(
      '/v1/trails/missing/steps',
      json={'kind': 'user_input', 'body': 'hi'},
      headers=_auth(),
    )
    assert resp.status == 404

  @pytest.mark.asyncio
  async def test_body_too_large_returns_413(self, client, store):
    cli = await client
    trail_id = await self._make_trail(cli)
    store.raise_body_too_large = True
    resp = await cli.post(
      f'/v1/trails/{trail_id}/steps',
      json={'kind': 'assistant', 'body': 'x'},
      headers=_auth(),
    )
    assert resp.status == 413

  @pytest.mark.asyncio
  async def test_invalid_kind_rejected(self, client):
    cli = await client
    trail_id = await self._make_trail(await client) if False else await self._make_trail(cli)
    resp = await cli.post(
      f'/v1/trails/{trail_id}/steps',
      json={'kind': 'system_prompt', 'body': 'x'},
      headers=_auth(),
    )
    assert resp.status == 400

  @pytest.mark.asyncio
  async def test_end_kind_rejected(self, client):
    cli = await client
    trail_id = await self._make_trail(cli)
    resp = await cli.post(
      f'/v1/trails/{trail_id}/steps',
      json={'kind': 'end', 'body': {}},
      headers=_auth(),
    )
    assert resp.status == 400

  @pytest.mark.asyncio
  async def test_extras_passed_through(self, client, store):
    cli = await client
    trail_id = await self._make_trail(cli)
    await cli.post(
      f'/v1/trails/{trail_id}/steps',
      json={
        'kind': 'tool_call',
        'body': None,
        'tool_name': 'foo',
        'arguments': {'a': 1},
        'call_id': 'c1',
        'turn_index': 1,
      },
      headers=_auth(),
    )
    step = store.steps[trail_id][-1]
    assert step['tool_name'] == 'foo'
    assert step['arguments'] == {'a': 1}
    assert step['call_id'] == 'c1'


class TestEndTrail:
  async def _make_trail(self, cli) -> str:
    resp = await cli.post('/v1/trails', json=_create_payload(), headers=_auth())
    return (await resp.json())['trail_id']

  @pytest.mark.asyncio
  async def test_happy_path_updates_header(self, client, store):
    cli = await client
    trail_id = await self._make_trail(cli)
    resp = await cli.post(
      f'/v1/trails/{trail_id}/end',
      json={'reason': 'terminal'},
      headers=_auth(),
    )
    assert resp.status == 204
    assert store.trails[trail_id]['end_reason'] == 'terminal'
    assert store.trails[trail_id]['ended_at'] is not None

  @pytest.mark.asyncio
  async def test_continuation_stored(self, client, store):
    cli = await client
    trail_id = await self._make_trail(cli)
    cont = {'provider': 'openai', 'response_id': 'resp_xyz'}
    resp = await cli.post(
      f'/v1/trails/{trail_id}/end',
      json={'reason': 'terminal', 'continuation': cont},
      headers=_auth(),
    )
    assert resp.status == 204
    assert store.trails[trail_id]['continuation'] == cont

  @pytest.mark.asyncio
  async def test_invalid_reason_rejected(self, client):
    cli = await client
    trail_id = await self._make_trail(cli)
    resp = await cli.post(
      f'/v1/trails/{trail_id}/end',
      json={'reason': 'whatever'},
      headers=_auth(),
    )
    assert resp.status == 400

  @pytest.mark.asyncio
  async def test_unknown_trail_404(self, client):
    cli = await client
    resp = await cli.post(
      '/v1/trails/missing/end',
      json={'reason': 'terminal'},
      headers=_auth(),
    )
    assert resp.status == 404


class TestGetTrail:
  @pytest.mark.asyncio
  async def test_returns_header(self, client):
    cli = await client
    resp = await cli.post('/v1/trails', json=_create_payload(), headers=_auth())
    trail_id = (await resp.json())['trail_id']
    resp = await cli.get(f'/v1/trails/{trail_id}', headers=_auth())
    assert resp.status == 200
    data = await resp.json()
    assert data['trail_id'] == trail_id
    assert data['bro'] == 'ppp-dev'

  @pytest.mark.asyncio
  async def test_unknown_trail_404(self, client):
    cli = await client
    resp = await cli.get('/v1/trails/missing', headers=_auth())
    assert resp.status == 404


class TestGetSteps:
  @pytest.mark.asyncio
  async def test_returns_steps_in_order(self, client):
    cli = await client
    resp = await cli.post('/v1/trails', json=_create_payload(), headers=_auth())
    trail_id = (await resp.json())['trail_id']
    for i, kind in enumerate(['user_input', 'reasoning', 'assistant']):
      await cli.post(
        f'/v1/trails/{trail_id}/steps',
        json={'kind': kind, 'body': f'step {i}', 'turn_index': 1},
        headers=_auth(),
      )
    resp = await cli.get(f'/v1/trails/{trail_id}/steps', headers=_auth())
    data = await resp.json()
    kinds = [s['kind'] for s in data['steps']]
    assert kinds == ['system_prompt', 'user_input', 'reasoning', 'assistant']

  @pytest.mark.asyncio
  async def test_pagination(self, client):
    cli = await client
    resp = await cli.post('/v1/trails', json=_create_payload(), headers=_auth())
    trail_id = (await resp.json())['trail_id']
    for i in range(5):
      await cli.post(
        f'/v1/trails/{trail_id}/steps',
        json={'kind': 'user_input', 'body': f'm{i}', 'turn_index': i},
        headers=_auth(),
      )
    resp = await cli.get(f'/v1/trails/{trail_id}/steps?limit=2', headers=_auth())
    data = await resp.json()
    assert len(data['steps']) == 2
    assert data['next'] is not None
    resp = await cli.get(
      f'/v1/trails/{trail_id}/steps?limit=10&after={data["next"]}', headers=_auth()
    )
    data2 = await resp.json()
    assert len(data2['steps']) == 4


class TestListTrails:
  @pytest.mark.asyncio
  async def test_filter_by_bro(self, client):
    cli = await client
    await cli.post('/v1/trails', json=_create_payload(bro='a'), headers=_auth())
    await cli.post('/v1/trails', json=_create_payload(bro='b'), headers=_auth())
    resp = await cli.get('/v1/trails?bro=a', headers=_auth())
    data = await resp.json()
    assert all(t['bro'] == 'a' for t in data['trails'])
    assert len(data['trails']) == 1

  @pytest.mark.asyncio
  async def test_bro_and_parent_mutex(self, client):
    cli = await client
    resp = await cli.get('/v1/trails?bro=a&parent=p', headers=_auth())
    assert resp.status == 400


class TestResolveAuth:
  def test_token_passes_through(self):
    assert resolve_auth(bearer_token='xyz', allow_no_auth=False, host='0.0.0.0') == 'xyz'

  def test_missing_token_requires_explicit_override(self):
    with pytest.raises(RuntimeError, match='TRAILS_BEARER_TOKEN'):
      resolve_auth(bearer_token=None, allow_no_auth=False, host='127.0.0.1')

  def test_no_auth_requires_loopback(self):
    with pytest.raises(RuntimeError, match='HOST'):
      resolve_auth(bearer_token=None, allow_no_auth=True, host='0.0.0.0')

  def test_no_auth_on_loopback_returns_none(self):
    assert resolve_auth(bearer_token=None, allow_no_auth=True, host='127.0.0.1') is None
    assert resolve_auth(bearer_token=None, allow_no_auth=True, host='localhost') is None


class TestBodySize:
  def test_none(self):
    assert storage._body_size_bytes(None) == 0

  def test_string(self):
    assert storage._body_size_bytes('hello') == 5

  def test_unicode_string(self):
    assert storage._body_size_bytes('héllo') == 6

  def test_dict(self):
    assert storage._body_size_bytes({'a': 1}) == len(b'{"a": 1}')

  def test_above_threshold(self):
    big = 'x' * (storage.SPILLOVER_THRESHOLD_BYTES + 1)
    assert storage._body_size_bytes(big) > storage.SPILLOVER_THRESHOLD_BYTES
