import contextlib
import hashlib
import json
import stat
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bro.trails import backends
from bro.trails.local import LocalStore
from bro.trails.model import BlazeRequest, canonical_json_bytes, payload_sha256
from bro.trails.record.bro import Recorder
from bro.trails.store import AppendConflict, TrailNotFound, fetch_recorded_trail


def _bro_request(*, bro: str = 'dev', forked_from: dict | None = None) -> BlazeRequest:
  return BlazeRequest(
    harness='bro',
    bro=bro,
    version='test',
    interactive=False,
    surface='ask',
    native={'llm': {'type': 'echo', 'model': 'echo'}},
    body={'records': [{'kind': 'system_prompt', 'body': 'prompt'}]},
    forked_from=forked_from,
  )


def _claude_request(*records: str, context=None, lineage=None) -> BlazeRequest:
  body = {'records': list(records)}
  if context is not None:
    body['launch_context'] = context
  return BlazeRequest(
    harness='claude',
    version='test',
    interactive=True,
    surface='ride',
    native={
      'llm': {},
      'segment': 'segment',
      'ride_command': 'ride along',
      'harness_version': 'test',
    },
    body=body,
    lineage=lineage,
  )


@contextlib.contextmanager
def _read_only_tree(root: Path) -> Iterator[None]:
  paths = [root, *root.rglob('*')]
  modes = {path: stat.S_IMODE(path.stat().st_mode) for path in paths}
  for path, mode in modes.items():
    path.chmod(mode & ~0o222)
  try:
    yield
  finally:
    for path, mode in modes.items():
      path.chmod(mode)


def test_records_and_replays_bro_trails(tmp_path):
  store = LocalStore(tmp_path)
  recorder = Recorder(store)
  trail_id = recorder.start_trail(
    bro='dev',
    llm_spec={'type': 'echo', 'model': 'echo'},
    system_prompt='prompt',
    forked_from=None,
    interactive=False,
    surface='ask',
  )
  assert recorder.step('user_input', 'hello', turn_index=0) == 1
  recorder.end_trail('ok')

  trail = fetch_recorded_trail(LocalStore(tmp_path), trail_id)
  assert [step.kind for step in trail.steps] == ['system_prompt', 'user_input']
  assert trail.header.bro == 'dev'
  assert (tmp_path / 'trails' / trail_id / 'header.json').is_file()
  assert (tmp_path / 'trails' / trail_id / 'steps.jsonl').is_file()


def test_reads_a_store_without_write_access(tmp_path):
  writer = LocalStore(tmp_path)
  raw = json.dumps(
    {
      'type': 'user',
      'uuid': 'uuid-1',
      'timestamp': '2026-01-01T00:00:00Z',
      'message': {'content': 'hello'},
    }
  )
  trail_id = writer.blaze(_claude_request(raw, context={'workspace': 'one'}))['id']

  with _read_only_tree(tmp_path):
    reader = LocalStore(tmp_path)
    assert reader.get_trail(trail_id)['id'] == trail_id
    assert reader.get_step(trail_id, 0)['body'] == raw
    assert reader.get_steps(trail_id)['steps'][0]['body'] == raw
    assert reader.get_messages(trail_id)['messages'][0]['content'] == 'hello'
    assert reader.get_launch_context(trail_id) == {'workspace': 'one'}
    assert reader.list_trails()['trails'][0]['id'] == trail_id


def test_read_creates_a_missing_lock_file(tmp_path):
  store = LocalStore(tmp_path)
  trail_id = store.blaze(_bro_request())['id']
  lock_path = tmp_path / 'trails' / trail_id / '.lock'
  lock_path.unlink()

  assert store.get_trail(trail_id)['id'] == trail_id
  assert lock_path.is_file()


def test_claude_rows_store_body_and_project_messages(tmp_path):
  store = LocalStore(tmp_path)
  raw = json.dumps(
    {
      'type': 'user',
      'uuid': 'uuid-1',
      'timestamp': '2026-01-01T00:00:00Z',
      'message': {'content': 'hello'},
    }
  )
  created = store.blaze(_claude_request(raw, context={'workspace': 'one'}))
  trail_id = created['id']

  assert store.get_step(trail_id, 0)['body'] == raw
  assert store.get_messages(trail_id)['messages'][0]['type'] == 'user_input'
  assert store.get_launch_context(trail_id) == {'workspace': 'one'}


def test_the_lineage_head_folds_across_appends(tmp_path):
  store = LocalStore(tmp_path)
  first = json.dumps({'type': 'user', 'uuid': 'uuid-1', 'message': {'content': 'hello'}})
  second = json.dumps({'type': 'mode', 'mode': 'normal'})
  trail_id = store.blaze(_claude_request(first))['id']

  store.append_records(trail_id, 1, [second])

  assert store.get_trail(trail_id)['native']['lineage_head'] == {
    'chain_first_uuid': 'uuid-1',
    'tail': [[0, 'uuid-1', payload_sha256(first)]],
    'last_row_digest': payload_sha256(second),
    'cuts': None,
  }
  bro_trail = store.blaze(_bro_request())['id']
  assert 'lineage_head' not in store.get_trail(bro_trail)['native']


def test_an_attach_declines_when_the_trail_advanced_under_the_verdict(tmp_path, monkeypatch):
  store = LocalStore(tmp_path)
  lines = [
    json.dumps({'type': 'user', 'uuid': f'uuid-{index}', 'message': {'content': 'hello'}})
    for index in range(3)
  ]
  trail_id = store.blaze(_claude_request(lines[0]))['id']
  lineage = {
    'segment': 'segment',
    'lines': [[json.loads(raw)['uuid'], payload_sha256(raw)] for raw in lines],
  }
  stale = store.find_segment_trails({'segment'})
  store.append_records(trail_id, 1, lines[1:2])
  monkeypatch.setattr(store, 'find_segment_trails', lambda segments: stale)

  assert store.blaze(_claude_request(lineage=lineage)) == {
    'adopted': False,
    'reason': backends.ATTACH_CONTENDED,
  }


def test_an_attach_leaves_the_segment_its_mint_stamped(tmp_path):
  store = LocalStore(tmp_path)
  lines = [
    json.dumps({'type': 'user', 'uuid': f'uuid-{index}', 'message': {'content': 'hello'}})
    for index in range(2)
  ]
  trail_id = store.blaze(_claude_request(lines[0]))['id']
  request = _claude_request(
    lineage={
      'segment': 'segment',
      'lines': [[json.loads(raw)['uuid'], payload_sha256(raw)] for raw in lines],
    }
  )
  # a writer whose native disagrees with the evidence it was verified on
  request.native['segment'] = 'elsewhere'

  assert store.blaze(request)['id'] == trail_id
  assert store.get_trail(trail_id)['native']['segment'] == 'segment'


def test_a_writer_may_not_send_the_folded_head(tmp_path):
  store = LocalStore(tmp_path)
  raw = json.dumps({'type': 'user', 'uuid': 'uuid-1', 'message': {'content': 'hello'}})
  request = _claude_request(raw)
  request.native['lineage_head'] = {'chain_first_uuid': 'uuid-9'}

  with pytest.raises(ValueError, match='lineage_head are server-derived'):
    store.blaze(request)


def test_the_lineage_index_answers_by_segment_and_by_record(tmp_path):
  store = LocalStore(tmp_path)
  raw = json.dumps({'type': 'user', 'uuid': 'uuid-1', 'message': {'content': 'hello'}})
  trail_id = store.blaze(_claude_request(raw))['id']

  [header] = store.find_segment_trails({'segment'})
  assert header['id'] == trail_id
  assert store.find_segment_trails({'elsewhere'}) == []
  assert store.find_segment_trails(set()) == []
  assert store.holds_record({trail_id}, 'uuid-1') is True
  assert store.holds_record({trail_id}, 'uuid-2') is False
  assert store.holds_record(set(), 'uuid-1') is False


def test_large_bodies_stay_inline(tmp_path):
  store = LocalStore(tmp_path)
  large = 'x' * (60 * 1024)
  trail_id = store.blaze(_bro_request())['id']
  store.append_records(trail_id, 1, [{'kind': 'user_input', 'body': large}])

  row = store.get_step(trail_id, 1)
  assert row['body'] == large
  assert 'body_s3' not in row


def test_appends_are_serialized_and_conflicts_are_typed(tmp_path):
  store = LocalStore(tmp_path)
  trail_id = store.blaze(_bro_request())['id']
  barrier = threading.Barrier(3)
  results: list[object] = []

  def append(body: str) -> None:
    barrier.wait()
    try:
      results.append(store.append_records(trail_id, 1, [{'kind': 'user_input', 'body': body}]))
    except Exception as exception:
      results.append(exception)

  threads = [threading.Thread(target=append, args=(body,)) for body in ('one', 'two')]
  for thread in threads:
    thread.start()
  barrier.wait()
  for thread in threads:
    thread.join()

  assert sum(isinstance(result, AppendConflict) for result in results) == 1
  assert sum(isinstance(result, dict) for result in results) == 1
  assert store.get_trail(trail_id)['extent'] == 2


def test_committed_append_retry_is_idempotent(tmp_path):
  store = LocalStore(tmp_path)
  trail_id = store.blaze(_bro_request())['id']
  records = [{'kind': 'user_input', 'body': 'hello'}]
  assert store.append_records(trail_id, 1, records)['appended'] == 1
  assert store.append_records(trail_id, 1, records) == {
    'extent': 2,
    'appended': 0,
    'duplicate': True,
  }


def test_tool_blobs_are_content_addressed(tmp_path):
  store = LocalStore(tmp_path)
  trail_id = store.blaze(_bro_request())['id']
  tools = [{'name': 'read'}]
  digest = hashlib.sha256(canonical_json_bytes(tools)).hexdigest()
  store.append_records(
    trail_id,
    1,
    [{'kind': 'user_input', 'body': 'hello'}],
    tools={digest: tools},
  )
  assert json.loads((tmp_path / 'trails' / 'tools' / f'{digest}.json').read_text()) == tools


def test_listing_preserves_selectors_and_cursor_pagination(tmp_path, monkeypatch):
  store = LocalStore(tmp_path)
  ids = iter(('trail-a', 'trail-b', 'trail-c'))
  monkeypatch.setattr('bro.trails.local.lulid', lambda: next(ids))
  parent = store.blaze(_bro_request(bro='parent'))['id']
  first = store.blaze(_bro_request(bro='dev', forked_from={'trail_id': parent, 'step_id': 0}))['id']
  second = store.blaze(_bro_request(bro='dev'))['id']

  page = store.list_trails(bro='dev', limit=1)
  assert len(page['trails']) == 1
  assert page['next'] is not None
  following = store.list_trails(bro='dev', limit=1, cursor=page['next'])
  assert {page['trails'][0]['id'], following['trails'][0]['id']} == {first, second}
  assert [item['id'] for item in store.list_trails(forked_from=parent)['trails']] == [first]
  with pytest.raises(ValueError, match='only one'):
    store.list_trails(bro='dev', harness='bro')


def test_stale_open_trail_infers_unreported_end_on_read(tmp_path):
  store = LocalStore(tmp_path)
  trail_id = store.blaze(_bro_request())['id']
  header_path = tmp_path / 'trails' / trail_id / 'header.json'
  header = json.loads(header_path.read_text())
  header['last_alive_at'] = (datetime.now(UTC) - timedelta(hours=2)).strftime(
    '%Y-%m-%dT%H:%M:%S.%fZ'
  )
  header_path.write_text(json.dumps(header))

  assert store.get_trail(trail_id)['end'] == {
    'at': header['last_alive_at'],
    'inference': 'unreported',
  }


def test_missing_trails_raise_store_neutral_error(tmp_path):
  store = LocalStore(tmp_path)
  with pytest.raises(TrailNotFound):
    store.get_trail('missing')


def test_delete_manifests_the_trail_before_removing_its_directory(tmp_path):
  store = LocalStore(tmp_path)
  trail_id = store.blaze(_claude_request(json.dumps({'type': 'system'}), context={'cwd': '/w'}))[
    'id'
  ]

  result = store.delete_trail(trail_id)

  manifest = json.loads(Path(result['manifest']).read_text())
  assert manifest['operation'] == 'delete'
  assert manifest['trail_id'] == trail_id
  assert manifest['header']['id'] == trail_id
  assert [step['step_id'] for step in manifest['steps']] == [0]
  assert not (tmp_path / 'trails' / trail_id).exists()
  assert Path(result['manifest']).parent == tmp_path / 'manifests' / 'delete'


def test_delete_leaves_shared_tool_blobs_alone(tmp_path):
  store = LocalStore(tmp_path)
  trail_id = store.blaze(_bro_request())['id']
  body = [{'type': 'function', 'name': 'read'}]
  sha256 = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
  store.append_records(trail_id, 1, [], tools={sha256: body})

  store.delete_trail(trail_id)

  assert (tmp_path / 'trails' / 'tools' / f'{sha256}.json').is_file()
