import asyncio
import json
import socket
import threading
from collections.abc import Iterator
from concurrent.futures import Future
from contextlib import ExitStack, contextmanager

import pytest
from aiohttp import web

from bro.trails.local import LocalStore
from bro.trails.model import BlazeRequest, payload_sha256
from bro.trails.network import NetworkStore
from bro.trails.server.auth import TokenTable
from bro.trails.server.server import create_app
from bro.trails.store import AppendConflict, TrailHasForks, TrailNotFound, TrailsStore

_TOKEN = 'contract-token'


def _token_table(*permissions: str) -> TokenTable:
  return TokenTable.from_config(
    {'tokens': {'contract': {'token': _TOKEN, 'permissions': list(permissions)}}}
  )


def _bro_request(*, bro='dev', body=None, forked_from=None, subject=None):
  return BlazeRequest(
    harness='bro',
    version='test',
    interactive=False,
    surface='ask',
    body=body if body is not None else {'records': [{'kind': 'system_prompt', 'body': 'prompt'}]},
    native={'llm': {'type': 'echo', 'model': 'echo'}},
    bro=bro,
    hold='unattended',
    forked_from=forked_from,
    subject=subject,
  )


def _lineage(*records, segment='segment'):
  return {
    'segment': segment,
    'lines': [[json.loads(record)['uuid'], payload_sha256(record)] for record in records],
  }


def _claude_request(*records, context=None, lineage=None):
  body = {'records': list(records)}
  if context is not None:
    body['launch_context'] = context
  return BlazeRequest(
    harness='claude',
    version='test',
    interactive=True,
    surface='ride',
    body=body,
    native={
      'llm': {'type': 'claude'},
      'segment': 'segment',
      'ride_command': 'ride along',
      'harness_version': 'test',
    },
    lineage=lineage,
  )


@contextmanager
def _loopback_server(store: TrailsStore) -> Iterator[str]:
  ready: Future[tuple[asyncio.AbstractEventLoop, int]] = Future()

  def run() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    runner = web.AppRunner(create_app(store, _token_table('read', 'write', 'admin')))
    try:
      loop.run_until_complete(runner.setup())
      listener = socket.socket()
      listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
      listener.bind(('127.0.0.1', 0))
      listener.listen()
      port = listener.getsockname()[1]
      site = web.SockSite(runner, listener)
      loop.run_until_complete(site.start())
      ready.set_result((loop, port))
      loop.run_forever()
    except BaseException as exception:
      if not ready.done():
        ready.set_exception(exception)
      raise
    finally:
      loop.run_until_complete(runner.cleanup())
      loop.close()

  thread = threading.Thread(target=run, name='trails-contract-server')
  thread.start()
  loop: asyncio.AbstractEventLoop | None = None
  try:
    loop, port = ready.result(timeout=10)
    yield f'http://127.0.0.1:{port}'
  finally:
    if loop is not None:
      loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=10)
    if thread.is_alive():
      raise RuntimeError('trails contract server did not stop')


@pytest.fixture(params=('local', 'network'), ids=('LocalStore', 'NetworkStore'))
def trails_store(request, tmp_path):
  with ExitStack() as stack:
    local = LocalStore(tmp_path / request.param)
    stack.enter_context(local)
    if request.param == 'local':
      yield local
      return
    base_url = stack.enter_context(_loopback_server(local))
    network = NetworkStore(base_url, _TOKEN, timeout=30)
    stack.enter_context(network)
    yield network


class TestTrailsStoreContract:
  def test_write_read_and_end_lifecycle(self, trails_store):
    created = trails_store.blaze(_bro_request(subject='initial'))
    trail_id = created['id']
    assert isinstance(created['started_at'], str)
    assert trails_store.get_trail(trail_id)['subject'] == 'initial'

    appended = [
      {'kind': 'user_input', 'body': 'hello'},
      {'kind': 'tool_result', 'body': 'result', 'call_id': 'call-1'},
    ]
    assert trails_store.append_records(trail_id, 1, appended) == {'extent': 3, 'appended': 2}
    assert trails_store.append_records(trail_id, 1, appended) == {
      'extent': 3,
      'appended': 0,
      'duplicate': True,
    }
    with pytest.raises(AppendConflict):
      trails_store.append_records(trail_id, 1, [{'kind': 'error', 'body': 'different'}])

    assert trails_store.get_step(trail_id, 1)['body'] == 'hello'
    first_page = trails_store.get_steps(trail_id, limit=1)
    assert [step['step_id'] for step in first_page['steps']] == [0]
    assert first_page['next'] == 0
    assert [step['step_id'] for step in trails_store.iter_steps(trail_id, page_size=1)] == [0, 1, 2]
    messages = trails_store.get_messages(trail_id, types={'user_input'})
    assert [message['content'] for message in messages['messages']] == ['hello']
    assert [message['type'] for message in trails_store.iter_messages(trail_id)] == [
      'system_prompt',
      'user_input',
      'tool_result',
    ]

    trails_store.keepalive(trail_id)
    assert trails_store.set_subject(trail_id, 'updated')['subject'] == 'updated'
    trails_store.end_trail(trail_id, 'raised', 'blocked')
    assert trails_store.get_trail(trail_id)['end']['detail'] == 'blocked'

  def test_listing_lineage_and_pagination(self, trails_store):
    parent = trails_store.blaze(_bro_request(bro='parent'))['id']
    child = trails_store.blaze(
      _bro_request(bro='dev', forked_from={'trail_id': parent, 'step_id': 0})
    )['id']
    sibling = trails_store.blaze(_bro_request(bro='dev'))['id']

    page = trails_store.list_trails(bro='dev', limit=1)
    following = trails_store.list_trails(bro='dev', limit=1, cursor=page['next'])
    assert {page['trails'][0]['id'], following['trails'][0]['id']} == {child, sibling}
    assert [trail['id'] for trail in trails_store.list_trails(forked_from=parent)['trails']] == [
      child
    ]
    assert {trail['id'] for trail in trails_store.iter_trails(harness='bro')} >= {
      parent,
      child,
      sibling,
    }

  def test_launch_context(self, trails_store):
    first = json.dumps({'type': 'system', 'uuid': 'uuid-1'})
    trail_id = trails_store.blaze(_claude_request(first, context={'cwd': '/workspace'}))['id']

    assert trails_store.get_launch_context(trail_id) == {'cwd': '/workspace'}

    no_context = trails_store.blaze(_bro_request())['id']
    assert trails_store.get_launch_context(no_context) is None
    with pytest.raises(TrailNotFound):
      trails_store.get_launch_context('missing')

  def test_blaze_resolves_harness_lineage(self, trails_store):
    first = json.dumps({'type': 'system', 'uuid': 'uuid-1'})
    second = json.dumps({'type': 'user', 'uuid': 'uuid-2', 'message': {'content': 'hello'}})
    trail_id = trails_store.blaze(_claude_request(first, second))['id']
    third = json.dumps({'type': 'user', 'uuid': 'uuid-3', 'message': {'content': 'again'}})

    declined = trails_store.blaze(_claude_request(lineage=_lineage(first, second)))
    verdict = trails_store.blaze(_claude_request(lineage=_lineage(first, second, third)))

    assert declined == {'adopted': False, 'reason': 'no line past the recorded extent yet'}
    assert verdict['adopted'] is True
    assert verdict['forked_from'] == {'trail_id': trail_id, 'step_id': 1}
    assert verdict['chunks'] == [[2, 2]]
    assert trails_store.get_trail(verdict['id'])['forked_from'] == verdict['forked_from']

  def test_large_bodies_are_inline(self, trails_store):
    trail_id = trails_store.blaze(_bro_request())['id']
    body = 'x' * (5 * 1024 * 1024)

    trails_store.append_records(trail_id, 1, [{'kind': 'tool_result', 'body': body}])
    served = trails_store.get_step(trail_id, 1)['body']

    assert served == body
    assert trails_store.resolve_body(served) == body

  def test_delete_takes_a_trail_but_refuses_one_a_fork_points_at(self, trails_store):
    parent = trails_store.blaze(_bro_request())['id']
    trails_store.append_records(parent, 1, [{'kind': 'user_input', 'body': 'hello'}])
    child = trails_store.blaze(_bro_request(forked_from={'trail_id': parent, 'step_id': 0}))['id']

    with pytest.raises(TrailHasForks) as refused:
      trails_store.delete_trail(parent)
    trails_store.delete_trail(child)
    removed = trails_store.delete_trail(parent)

    assert refused.value.forks == [child]
    assert removed['trail_id'] == parent
    assert removed['extent'] == 2
    assert len(removed['manifest']) > 0
    with pytest.raises(TrailNotFound):
      trails_store.get_trail(parent)
    with pytest.raises(TrailNotFound):
      trails_store.delete_trail(parent)

  def test_missing_rows_raise_store_neutral_errors(self, trails_store):
    with pytest.raises(TrailNotFound):
      trails_store.get_trail('missing')
    with pytest.raises(TrailNotFound):
      trails_store.get_step('missing', 0)
    with pytest.raises(TrailNotFound):
      trails_store.get_steps('missing')
