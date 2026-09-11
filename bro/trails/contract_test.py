import asyncio
import json
import socket
import threading
from collections.abc import Iterator
from concurrent.futures import Future
from contextlib import ExitStack, contextmanager
from pathlib import Path

import pytest
from aiohttp import web

from bro.trails import formats, model
from bro.trails.local import LocalStore
from bro.trails.model import BlazeRequest, payload_sha256, tools_sha256
from bro.trails.network import NetworkStore
from bro.trails.server.auth import TokenTable
from bro.trails.server.server import create_app
from bro.trails.store import (
  AppendConflict,
  InvalidRequest,
  ToolNotFound,
  TrailCollision,
  TrailHasForks,
  TrailNotFound,
  TrailsStore,
)

_TOKEN = 'contract-token'
_CONTRACT_LOCAL_STORES: dict[int, LocalStore] = {}


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


def _lineage(*records, segment='segment', related=()):
  return {
    'segment': segment,
    'lines': [[json.loads(record)['uuid'], payload_sha256(record)] for record in records],
    'related_segments': list(related),
  }


def _claude_request(*records, context=None, lineage=None, version='test', native=None, **fields):
  body = {'records': list(records)}
  if context is not None:
    body['launch_context'] = context
  return BlazeRequest(
    harness='claude',
    version=version,
    interactive=True,
    surface='ride',
    body=body,
    native={
      'llm': {'type': 'claude'},
      'segment': 'segment',
      'ride_command': 'ride along',
      'harness_version': 'test',
      **(native if native is not None else {}),
    },
    lineage=lineage,
    **fields,
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
    _CONTRACT_LOCAL_STORES[id(network)] = local
    stack.callback(_CONTRACT_LOCAL_STORES.pop, id(network))
    stack.enter_context(network)
    yield network


def _stored_paths(store: TrailsStore, trail_id: str) -> tuple[Path, Path]:
  local = store if isinstance(store, LocalStore) else _CONTRACT_LOCAL_STORES[id(store)]
  directory = local.trails_directory / trail_id
  return directory / 'header.json', directory / 'steps.jsonl'


def _remove_stored_formats(store: TrailsStore, trail_id: str) -> None:
  header_path, rows_path = _stored_paths(store, trail_id)
  header = json.loads(header_path.read_text())
  header.pop('format')
  header_path.write_text(json.dumps(header))
  rows = [json.loads(line) for line in rows_path.read_text().splitlines()]
  for row in rows:
    row.pop('format')
  rows_path.write_text(''.join(json.dumps(row) + '\n' for row in rows))


def _install_synthetic_format(monkeypatch) -> None:
  monkeypatch.setitem(
    formats.UPGRADES,
    1,
    formats.FormatUpgrade(
      header=lambda header: {**header, 'synthetic_header': True},
      row=lambda row: {**row, 'call_id': 'upgraded'},
    ),
  )
  monkeypatch.setattr(model, 'TRAIL_FORMAT', 2)


class TestTrailsStoreContract:
  def test_writes_the_current_format_on_the_header_and_every_row(self, trails_store):
    trail_id = trails_store.blaze(_bro_request())['id']
    trails_store.append_records(trail_id, 1, [{'kind': 'user_input', 'body': 'hello'}])

    header_path, rows_path = _stored_paths(trails_store, trail_id)
    header = json.loads(header_path.read_text())
    stored_rows = [json.loads(line) for line in rows_path.read_text().splitlines()]

    assert header['format'] == model.TRAIL_FORMAT
    assert [row['format'] for row in stored_rows] == [model.TRAIL_FORMAT, model.TRAIL_FORMAT]

  def test_upgrades_a_formatless_layout_in_memory_for_every_read_path(
    self, trails_store, monkeypatch
  ):
    trail_id = trails_store.blaze(_bro_request())['id']
    _remove_stored_formats(trails_store, trail_id)
    _install_synthetic_format(monkeypatch)

    header = trails_store.get_trail(trail_id)
    row = trails_store.get_step(trail_id, 0)
    messages = trails_store.get_messages(trail_id)

    assert (header['format'], header['synthetic_header']) == (2, True)
    assert (row['format'], row['call_id']) == (2, 'upgraded')
    assert messages['messages'][0]['call_id'] == 'upgraded'
    header_path, rows_path = _stored_paths(trails_store, trail_id)
    assert 'format' not in json.loads(header_path.read_text())
    assert 'format' not in json.loads(rows_path.read_text().splitlines()[0])

  def test_refuses_a_format_newer_than_the_reader_names(self, trails_store):
    trail_id = trails_store.blaze(_bro_request())['id']
    header_path, rows_path = _stored_paths(trails_store, trail_id)
    header = json.loads(header_path.read_text())
    header['format'] = model.TRAIL_FORMAT + 1
    header_path.write_text(json.dumps(header))

    with pytest.raises(ValueError, match='trail header uses trail format 2'):
      trails_store.get_trail(trail_id)

    header['format'] = model.TRAIL_FORMAT
    header_path.write_text(json.dumps(header))
    [row] = [json.loads(line) for line in rows_path.read_text().splitlines()]
    row['format'] = model.TRAIL_FORMAT + 1
    rows_path.write_text(json.dumps(row) + '\n')
    with pytest.raises(ValueError, match=f'trail row {trail_id}/0'):
      trails_store.get_step(trail_id, 0)

  def test_append_migrates_an_older_layout_before_writing(self, trails_store, monkeypatch):
    trail_id = trails_store.blaze(_bro_request())['id']
    _remove_stored_formats(trails_store, trail_id)
    _install_synthetic_format(monkeypatch)

    trails_store.append_records(trail_id, 1, [{'kind': 'user_input', 'body': 'hello'}])

    header_path, rows_path = _stored_paths(trails_store, trail_id)
    header = json.loads(header_path.read_text())
    stored_rows = [json.loads(line) for line in rows_path.read_text().splitlines()]
    assert (header['format'], header['synthetic_header']) == (2, True)
    assert [(row['format'], row['body'], row.get('call_id')) for row in stored_rows] == [
      (2, 'prompt', 'upgraded'),
      (2, 'hello', None),
    ]

  def test_row_upgrade_refuses_an_in_place_nested_body_change(self, monkeypatch):
    row = {'trail_id': 'trail', 'step_id': 0, 'body': {'text': 'original'}}

    def change_body(upgrading: dict) -> dict:
      upgrading['body']['text'] = 'changed'
      return upgrading

    monkeypatch.setitem(
      formats.UPGRADES,
      1,
      formats.FormatUpgrade(header=lambda header: header, row=change_body),
    )
    monkeypatch.setattr(model, 'TRAIL_FORMAT', 2)

    with pytest.raises(ValueError, match='format upgrade changed its immutable body'):
      formats.upgrade_row(row)
    assert row['body'] == {'text': 'original'}

  def test_migration_refuses_a_row_upgrade_that_changes_the_recorded_body(
    self, trails_store, monkeypatch
  ):
    trail_id = trails_store.blaze(_bro_request())['id']
    _remove_stored_formats(trails_store, trail_id)
    monkeypatch.setitem(
      formats.UPGRADES,
      1,
      formats.FormatUpgrade(
        header=lambda header: header,
        row=lambda row: {**row, 'body': 'changed'},
      ),
    )
    monkeypatch.setattr(model, 'TRAIL_FORMAT', 2)

    with pytest.raises(ValueError, match='format upgrade changed its immutable body'):
      trails_store.migrate_trail(trail_id)

    header_path, rows_path = _stored_paths(trails_store, trail_id)
    assert 'format' not in json.loads(header_path.read_text())
    assert json.loads(rows_path.read_text().splitlines()[0])['body'] == 'prompt'

  def test_attach_migrates_an_older_layout_before_reopening(self, trails_store, monkeypatch):
    first = json.dumps({'type': 'system', 'uuid': 'uuid-1'})
    second = json.dumps({'type': 'user', 'uuid': 'uuid-2', 'message': {'content': 'hello'}})
    trail_id = trails_store.blaze(_claude_request(first))['id']
    _remove_stored_formats(trails_store, trail_id)
    _install_synthetic_format(monkeypatch)

    attached = trails_store.blaze(_claude_request(lineage=_lineage(first, second)))

    assert attached['id'] == trail_id
    header_path, rows_path = _stored_paths(trails_store, trail_id)
    assert json.loads(header_path.read_text())['format'] == 2
    assert json.loads(rows_path.read_text().splitlines()[0])['format'] == 2

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
    resumed = trails_store.blaze(_claude_request(lineage=_lineage(first, second, third)))
    copied = trails_store.blaze(
      _claude_request(
        lineage=_lineage(first, second, third, segment='copy', related=('segment',)),
        native={'segment': 'copy'},
      )
    )

    assert declined == {'adopted': False, 'reason': 'no line past the recorded extent yet'}
    assert resumed['adopted'] is True
    assert (resumed['id'], resumed['attached'], resumed['extent']) == (trail_id, True, 2)
    assert resumed['chunks'] == [[2, 2]]
    assert copied['id'] != trail_id
    assert copied['forked_from'] == {'trail_id': trail_id, 'step_id': 1}
    assert trails_store.get_trail(copied['id'])['forked_from'] == copied['forked_from']

  def test_attaching_reopens_the_segments_trail_for_the_new_lifetime(self, trails_store):
    first = json.dumps({'type': 'system', 'uuid': 'uuid-1'})
    second = json.dumps({'type': 'user', 'uuid': 'uuid-2', 'message': {'content': 'hello'}})
    opened = trails_store.blaze(
      _claude_request(first, second, summoned_by={'trail_id': 'summoner', 'step_id': 1})
    )
    trails_store.end_trail(opened['id'], 'ok')
    third = json.dumps({'type': 'user', 'uuid': 'uuid-3', 'message': {'content': 'again'}})

    attached = trails_store.blaze(
      _claude_request(
        lineage=_lineage(first, second, third),
        version='next',
        hold='unattended',
        location={'host': 'elsewhere'},
        native={'llm': {'type': 'claude', 'model': 'other'}, 'ride_command': 'ride along again'},
      )
    )
    trails_store.append_records(opened['id'], attached['extent'], [third])

    assert attached['id'] == opened['id']
    header = trails_store.get_trail(opened['id'])
    assert header['end'] is None
    assert header['extent'] == 3
    assert (header['version'], header['hold']) == ('next', 'unattended')
    assert header['location'] == {'host': 'elsewhere'}
    assert header['native']['llm'] == {'type': 'claude', 'model': 'other'}
    assert header['native']['ride_command'] == 'ride along again'
    # what the rows folded is the trail's, not the attaching writer's to reset
    assert header['native']['step_counts_by_kind'] == {'system': 1, 'user': 2}
    # the attribution stays with the run that opened the trail
    assert header['summoned_by'] == {'trail_id': 'summoner', 'step_id': 1}

  def test_a_lost_blaze_response_converges_on_the_trail_it_minted(self, trails_store):
    first = json.dumps({'type': 'system', 'uuid': 'uuid-1'})
    second = json.dumps({'type': 'user', 'uuid': 'uuid-2', 'message': {'content': 'hello'}})
    # the response the caller never saw: a trail awarded the whole file, holding
    # no row yet to be verified against
    orphan = trails_store.blaze(_claude_request(lineage=_lineage(first, second)))

    retry = trails_store.blaze(_claude_request(lineage=_lineage(first, second)))
    trails_store.append_records(retry['id'], retry['extent'], [first, second])

    assert retry['id'] == orphan['id']
    assert (retry['attached'], retry['extent'], retry['chunks']) == (True, 0, [[0, 0]])
    assert [step['body'] for step in trails_store.iter_steps(orphan['id'])] == [first, second]
    assert len(list(trails_store.iter_trails(harness='claude'))) == 1

  def test_every_backend_refuses_a_body_no_reader_could_render(self, trails_store):
    trail_id = trails_store.blaze(_bro_request())['id']

    with pytest.raises(InvalidRequest, match='user_input body must be a string'):
      trails_store.append_records(trail_id, 1, [{'kind': 'user_input', 'body': {'text': 'ping'}}])
    trails_store.append_records(trail_id, 1, [{'kind': 'error', 'body': {'message': 'boom'}}])

    assert trails_store.get_trail(trail_id)['extent'] == 2

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


_TOOLS = [{'type': 'function', 'name': 'read'}]
_TOOLS_DIGEST = tools_sha256(_TOOLS)


def _recorded(store: TrailsStore, trail_id: str) -> tuple[dict, list[dict], object]:
  """What an export reads through the contract: the served header, the served
  rows with their bodies resolved, and the launch context."""
  header = store.get_trail(trail_id)
  rows = [
    {**row, 'body': store.resolve_body(row.get('body'))} for row in store.iter_steps(trail_id)
  ]
  return header, rows, store.get_launch_context(trail_id)


def _record_source(root: Path, **overrides) -> tuple[LocalStore, str]:
  source = LocalStore(root)
  trail_id = source.blaze(_bro_request(subject='recorded', **overrides))['id']
  source.append_records(
    trail_id,
    1,
    [
      {'kind': 'user_input', 'body': 'hello'},
      {'kind': 'tool_result', 'body': 'result', 'call_id': 'call-1', 'tools_sha256': _TOOLS_DIGEST},
    ],
    tools={_TOOLS_DIGEST: _TOOLS},
  )
  source.end_trail(trail_id, 'ok')
  return source, trail_id


class TestImportContract:
  def test_reads_a_tool_blob_by_digest(self, trails_store):
    trail_id = trails_store.blaze(_bro_request())['id']
    trails_store.append_records(
      trail_id,
      1,
      [{'kind': 'tool_result', 'body': 'result', 'tools_sha256': _TOOLS_DIGEST}],
      tools={_TOOLS_DIGEST: _TOOLS},
    )

    assert trails_store.get_tool(_TOOLS_DIGEST) == _TOOLS
    with pytest.raises(ToolNotFound) as missing:
      trails_store.get_tool('0' * 64)
    assert missing.value.sha256 == '0' * 64

  def test_an_imported_trail_is_served_as_it_was_recorded(self, trails_store, tmp_path):
    source, trail_id = _record_source(tmp_path / 'source')
    header, rows, context = _recorded(source, trail_id)

    result = trails_store.import_trail(
      header, rows, launch_context=context, tools={_TOOLS_DIGEST: _TOOLS}
    )

    assert result == {'trail_id': trail_id, 'extent': 3}
    assert trails_store.get_trail(trail_id) == header
    assert list(trails_store.iter_steps(trail_id)) == rows
    assert trails_store.get_tool(_TOOLS_DIGEST) == _TOOLS
    assert [message['type'] for message in trails_store.iter_messages(trail_id)] == [
      'system_prompt',
      'user_input',
      'tool_result',
    ]

  def test_an_import_restarts_over_what_the_store_already_holds(self, trails_store, tmp_path):
    source, trail_id = _record_source(tmp_path / 'source')
    header, rows, _ = _recorded(source, trail_id)
    trails_store.begin_import(header)
    trails_store.import_rows(trail_id, 0, rows[:2], tools={_TOOLS_DIGEST: _TOOLS})

    resumed = trails_store.begin_import(header)
    rechunked = trails_store.import_rows(trail_id, 0, rows)
    repeated = trails_store.import_rows(trail_id, 1, rows[1:])
    with pytest.raises(AppendConflict):
      trails_store.import_rows(trail_id, 5, [{**rows[0], 'step_id': 5}])
    sealed = trails_store.seal_import(trail_id)
    whole_again = trails_store.import_trail(header, rows)

    assert resumed == {'trail_id': trail_id, 'extent': 2, 'created': False}
    assert rechunked == {'extent': 3, 'appended': 1}
    assert repeated == {'extent': 3, 'appended': 0, 'duplicate': True}
    assert sealed == {'trail_id': trail_id, 'extent': 3}
    assert whole_again == {'trail_id': trail_id, 'extent': 3, 'duplicate': True}
    assert trails_store.get_trail(trail_id) == header

  def test_a_sealed_trail_answers_only_the_import_it_was_recorded_as(self, trails_store, tmp_path):
    source, trail_id = _record_source(tmp_path / 'source')
    header, rows, _ = _recorded(source, trail_id)
    trails_store.import_trail(header, rows, tools={_TOOLS_DIGEST: _TOOLS})
    other = LocalStore(tmp_path / 'other')
    other_id = other.blaze(_bro_request())['id']
    other_header, other_rows, _ = _recorded(other, other_id)
    trails_store.begin_import(other_header)

    with pytest.raises(TrailCollision, match='3 rows stored, 1 recorded'):
      trails_store.import_trail({**header, 'extent': 1}, rows[:1])
    with pytest.raises(ValueError, match='header records 3 rows, 1 given'):
      trails_store.import_trail(header, rows[:1])
    with pytest.raises(TrailCollision, match='end differs'):
      trails_store.begin_import({**header, 'end': None})
    with pytest.raises(TrailCollision, match='rows past its sealed extent 3'):
      trails_store.import_rows(trail_id, 3, [{**rows[2], 'step_id': 3}])
    with pytest.raises(TrailCollision, match='another import of it is under way'):
      trails_store.begin_import({**other_header, 'extent': 5})
    with pytest.raises(ValueError, match='holds 0 of the 1 rows recorded'):
      trails_store.seal_import(other_id)
    with pytest.raises(ValueError, match='recorded with 1 rows, 2 sent'):
      trails_store.import_rows(other_id, 0, [other_rows[0], {**other_rows[0], 'step_id': 1}])

    assert trails_store.get_trail(trail_id) == header
    assert trails_store.get_trail(other_id)['importing'] == {'extent': 1, 'end': None}

  def test_a_different_trail_under_the_same_id_is_a_collision(self, trails_store, tmp_path):
    source, trail_id = _record_source(tmp_path / 'source')
    header, rows, _ = _recorded(source, trail_id)
    trails_store.import_trail(header, rows, tools={_TOOLS_DIGEST: _TOOLS})
    changed = {**rows[1], 'body': 'changed', 'payload_sha256': payload_sha256('changed')}

    with pytest.raises(TrailCollision, match='header differs'):
      trails_store.begin_import({**header, 'bro': 'other'})
    with pytest.raises(TrailCollision, match='row 1 differs'):
      trails_store.import_rows(trail_id, 0, [rows[0], changed, rows[2]])
    with pytest.raises(TrailCollision, match='launch context differs'):
      trails_store.begin_import(header, launch_context={'cwd': '/elsewhere'})

  def test_an_import_requires_parents_and_tool_blobs(self, trails_store, tmp_path):
    source = LocalStore(tmp_path / 'source')
    parent = source.blaze(_bro_request())['id']
    _, child = _record_source(tmp_path / 'source', forked_from={'trail_id': parent, 'step_id': 0})
    parent_header, parent_rows, _ = _recorded(source, parent)
    child_header, child_rows, _ = _recorded(source, child)

    with pytest.raises(ValueError, match=f'forked from {parent}, which must be imported first'):
      trails_store.begin_import(child_header)
    trails_store.import_trail(parent_header, parent_rows)
    trails_store.begin_import(child_header)
    with pytest.raises(ValueError, match='neither carried nor stored'):
      trails_store.import_rows(child, 0, child_rows)
    trails_store.import_rows(child, 0, child_rows, tools={_TOOLS_DIGEST: _TOOLS})
    trails_store.seal_import(child)
    with pytest.raises(ValueError, match='invalid trail id'):
      trails_store.begin_import(
        {**parent_header, 'id': 'other', 'forked_from': {'trail_id': 'tools', 'step_id': 0}}
      )

    assert trails_store.get_trail(child)['forked_from'] == {'trail_id': parent, 'step_id': 0}
    assert [trail['id'] for trail in trails_store.list_trails(forked_from=parent)['trails']] == [
      child
    ]

  def test_an_import_refuses_a_corrupt_stored_tool_blob(self, trails_store, tmp_path):
    source, trail_id = _record_source(tmp_path / 'source')
    header, rows, _ = _recorded(source, trail_id)
    trails_store.begin_import(header)
    local = (
      trails_store
      if isinstance(trails_store, LocalStore)
      else _CONTRACT_LOCAL_STORES[id(trails_store)]
    )
    tool_path = local.trails_directory / 'tools' / f'{_TOOLS_DIGEST}.json'
    tool_path.parent.mkdir(exist_ok=True)
    tool_path.write_bytes(b'{}')

    with pytest.raises(ValueError, match=f'tool blob hash mismatch: {_TOOLS_DIGEST}'):
      trails_store.import_rows(trail_id, 0, rows)
    with pytest.raises(ValueError, match=f'tool blob hash mismatch: {_TOOLS_DIGEST}'):
      trails_store.import_rows(trail_id, 0, rows, tools={_TOOLS_DIGEST: _TOOLS})

  def test_an_import_refuses_rows_the_adapter_refuses(self, trails_store, tmp_path):
    source, trail_id = _record_source(tmp_path / 'source')
    header, rows, _ = _recorded(source, trail_id)
    trails_store.begin_import(header)

    with pytest.raises(InvalidRequest, match='user_input body must be a string'):
      trails_store.import_rows(trail_id, 0, [rows[0], {**rows[1], 'body': {'text': 'ping'}}])
    with pytest.raises(InvalidRequest, match='row carries step 2'):
      trails_store.import_rows(trail_id, 0, [rows[0], rows[2]])

    assert trails_store.get_trail(trail_id)['extent'] == 0

  def test_an_import_validates_an_older_header_in_its_upgraded_shape(
    self, trails_store, tmp_path, monkeypatch
  ):
    source, trail_id = _record_source(tmp_path / 'source')
    _remove_stored_formats(source, trail_id)
    header_path, _ = _stored_paths(source, trail_id)
    header = json.loads(header_path.read_text())
    expected_native = header['native']
    header['native'] = {'recipe': expected_native['llm']}
    header_path.write_text(json.dumps(header))
    recorded_header = source.stored_header(trail_id)
    recorded_rows = source.stored_rows(trail_id)

    def upgrade_header(upgrading: dict) -> dict:
      native = dict(upgrading['native'])
      native['llm'] = native.pop('recipe')
      return {**upgrading, 'native': native}

    monkeypatch.setitem(
      formats.UPGRADES,
      1,
      formats.FormatUpgrade(header=upgrade_header, row=lambda row: row),
    )
    monkeypatch.setattr(model, 'TRAIL_FORMAT', 2)

    trails_store.import_trail(
      recorded_header,
      recorded_rows,
      tools={_TOOLS_DIGEST: _TOOLS},
    )

    imported_header_path, _ = _stored_paths(trails_store, trail_id)
    assert 'format' not in json.loads(imported_header_path.read_text())
    assert trails_store.get_trail(trail_id)['native'] == expected_native

  def test_an_import_requires_the_parent_named_by_an_upgraded_pointer(
    self, trails_store, tmp_path, monkeypatch
  ):
    source, parent = _record_source(tmp_path / 'source')
    _, child = _record_source(
      tmp_path / 'source',
      forked_from={'trail_id': parent, 'step_id': 0},
    )
    parent_header, parent_rows, _ = _recorded(source, parent)
    child_header, child_rows, _ = _recorded(source, child)
    child_header['forked_from'] = {'parent': parent, 'ordinal': 0}

    def upgrade_header(header: dict) -> dict:
      pointer = header.get('forked_from')
      if pointer is None:
        return header
      return {
        **header,
        'forked_from': {'trail_id': pointer['parent'], 'step_id': pointer['ordinal']},
      }

    monkeypatch.setitem(
      formats.UPGRADES,
      1,
      formats.FormatUpgrade(header=upgrade_header, row=lambda row: row),
    )
    monkeypatch.setattr(model, 'TRAIL_FORMAT', 2)
    trails_store.import_trail(parent_header, parent_rows, tools={_TOOLS_DIGEST: _TOOLS})

    trails_store.import_trail(child_header, child_rows, tools={_TOOLS_DIGEST: _TOOLS})

    assert trails_store.get_trail(child)['forked_from'] == {
      'trail_id': parent,
      'step_id': 0,
    }

  def test_an_import_keeps_the_recorded_formats_and_refuses_a_newer_one(
    self, trails_store, tmp_path
  ):
    source, trail_id = _record_source(tmp_path / 'source')
    _remove_stored_formats(source, trail_id)
    header = source.stored_header(trail_id)
    rows = source.stored_rows(trail_id)

    trails_store.import_trail(header, rows, tools={_TOOLS_DIGEST: _TOOLS})

    header_path, rows_path = _stored_paths(trails_store, trail_id)
    assert 'format' not in json.loads(header_path.read_text())
    assert 'format' not in json.loads(rows_path.read_text().splitlines()[2])
    assert trails_store.get_trail(trail_id)['format'] == model.TRAIL_FORMAT
    assert trails_store.get_trail(trail_id)['extent'] == 3
    with pytest.raises(ValueError, match=f'trail format {model.TRAIL_FORMAT + 1}'):
      trails_store.begin_import({**header, 'id': 'newer', 'format': model.TRAIL_FORMAT + 1})

  def test_an_import_keeps_the_minted_lineage_cuts(self, trails_store, tmp_path):
    source = LocalStore(tmp_path / 'source')
    first = json.dumps({'type': 'system', 'uuid': 'uuid-1'})
    second = json.dumps({'type': 'user', 'uuid': 'uuid-2', 'message': {'content': 'hello'}})
    minted = source.blaze(
      _claude_request(context={'cwd': '/workspace'}, lineage=_lineage(first, second))
    )
    source.append_records(minted['id'], 0, [first, second])
    header, rows, context = _recorded(source, minted['id'])

    trails_store.import_trail(header, rows, launch_context=context)

    imported = trails_store.get_trail(minted['id'])
    assert imported['native']['lineage_head']['cuts'] == minted['chunks']
    assert imported == header
    assert trails_store.get_launch_context(minted['id']) == {'cwd': '/workspace'}
