import json
from pathlib import Path

import pytest

from bro.trails import formats, model, transfer
from bro.trails.local import LocalStore
from bro.trails.model import BlazeRequest, tools_sha256
from bro.trails.store import TrailNotFound

_TOOLS = [{'type': 'function', 'name': 'read'}]
_TOOLS_DIGEST = tools_sha256(_TOOLS)


def _blaze(store: LocalStore, **overrides) -> str:
  return store.blaze(
    BlazeRequest(
      harness='bro',
      bro='dev',
      version='test',
      interactive=False,
      surface='ask',
      native={'llm': {'type': 'echo', 'model': 'echo'}},
      body={'records': [{'kind': 'system_prompt', 'body': 'prompt'}]},
      **overrides,
    )
  )['id']


def _served(store: LocalStore, trail_id: str) -> tuple[dict, list[dict]]:
  return store.get_trail(trail_id), list(store.iter_steps(trail_id))


def _lineage(source: LocalStore) -> tuple[str, str, str]:
  """A root, a fork of it, and a trail summoned from the fork, whose rows name
  a tool blob."""
  root = _blaze(source)
  fork = _blaze(source, forked_from={'trail_id': root, 'step_id': 0})
  summoned = _blaze(source, summoned_by={'trail_id': fork, 'step_id': 0})
  source.append_records(
    summoned,
    1,
    [{'kind': 'tool_result', 'body': 'result', 'tools_sha256': _TOOLS_DIGEST}],
    tools={_TOOLS_DIGEST: _TOOLS},
  )
  source.end_trail(summoned, 'ok')
  return root, fork, summoned


def test_export_writes_the_ancestry_parents_first_and_import_reads_it_back(tmp_path):
  source = LocalStore(tmp_path / 'source')
  root, fork, summoned = _lineage(source)
  unrelated = _blaze(source)
  layout = tmp_path / 'layout'

  exported = transfer.export_trails(source, [summoned], layout)

  assert [result['trail_id'] for result in exported] == [root, fork, summoned]
  written = LocalStore(layout)
  assert written.stored_trail_ids() == sorted([root, fork, summoned])
  assert unrelated not in written.stored_trail_ids()
  assert _served(written, summoned) == _served(source, summoned)
  assert written.get_tool(_TOOLS_DIGEST) == _TOOLS

  destination = LocalStore(tmp_path / 'destination')
  imported = transfer.import_layout(layout, destination)

  assert [result['trail_id'] for result in imported] == [root, fork, summoned]
  for trail_id in (root, fork, summoned):
    assert _served(destination, trail_id) == _served(source, trail_id)
  assert destination.get_tool(_TOOLS_DIGEST) == _TOOLS


def test_export_fails_on_an_ancestor_the_store_does_not_hold(tmp_path):
  source = LocalStore(tmp_path / 'source')
  orphan = _blaze(source, summoned_by={'trail_id': 'elsewhere', 'step_id': 0})

  with pytest.raises(TrailNotFound) as missing:
    transfer.export_trails(source, [orphan], tmp_path / 'layout')

  assert missing.value.trail_id == 'elsewhere'


def test_import_layout_refuses_an_external_parent_by_default(tmp_path):
  source = LocalStore(tmp_path / 'source')
  child = _blaze(source, summoned_by={'trail_id': 'elsewhere', 'step_id': 0})

  with pytest.raises(ValueError, match=f'trail {child} is summoned by elsewhere'):
    transfer.import_layout(tmp_path / 'source', LocalStore(tmp_path / 'destination'))


def test_import_layout_into_an_external_parents_store_keeps_the_pointer(tmp_path):
  # a summoned child adopted into a store its summoner did not record to keeps
  # its cross-backend summon pointer
  source = LocalStore(tmp_path / 'source')
  child = _blaze(source, summoned_by={'trail_id': 'elsewhere', 'step_id': 0})
  destination = LocalStore(tmp_path / 'destination', external_parents_ok=True)

  imported = transfer.import_layout(tmp_path / 'source', destination)

  assert [result['trail_id'] for result in imported] == [child]
  assert destination.get_trail(child)['summoned_by']['trail_id'] == 'elsewhere'


def test_transfer_refuses_a_source_with_an_incomplete_import(tmp_path):
  recorded = LocalStore(tmp_path / 'recorded')
  trail_id = _blaze(recorded)
  recorded.append_records(trail_id, 1, [{'kind': 'user_input', 'body': 'hello'}])
  partial = LocalStore(tmp_path / 'partial')
  header, rows = _served(recorded, trail_id)
  partial.begin_import(header)
  partial.import_rows(trail_id, 0, rows[:1])

  with pytest.raises(ValueError, match=f'trail {trail_id} has an import under way'):
    transfer.import_layout(partial.root, LocalStore(tmp_path / 'imported'))
  with pytest.raises(ValueError, match=f'trail {trail_id} has an import under way'):
    transfer.export_trails(partial, [trail_id], tmp_path / 'exported')


def test_import_layout_carries_each_record_in_the_format_it_was_written_in(tmp_path, monkeypatch):
  source = LocalStore(tmp_path / 'source')
  trail_id = _blaze(source)
  source.append_records(trail_id, 1, [{'kind': 'user_input', 'body': 'hello'}])
  header_path = source.trails_directory / trail_id / 'header.json'
  rows_path = source.trails_directory / trail_id / 'steps.jsonl'
  header = json.loads(header_path.read_text())
  header.pop('format')
  header_path.write_text(json.dumps(header))
  stored_rows = [json.loads(line) for line in rows_path.read_text().splitlines()]
  stored_rows[0].pop('format')
  rows_path.write_text(''.join(json.dumps(row) + '\n' for row in stored_rows))
  monkeypatch.setitem(
    formats.UPGRADES,
    1,
    formats.FormatUpgrade(
      header=lambda header: {**header, 'synthetic_header': True},
      row=lambda row: {**row, 'call_id': 'upgraded'},
    ),
  )
  monkeypatch.setattr(model, 'TRAIL_FORMAT', 2)
  destination = LocalStore(tmp_path / 'destination')

  transfer.import_layout(tmp_path / 'source', destination)

  written_header = json.loads((destination.trails_directory / trail_id / 'header.json').read_text())
  written_rows = [
    json.loads(line)
    for line in (destination.trails_directory / trail_id / 'steps.jsonl').read_text().splitlines()
  ]
  assert 'format' not in written_header
  assert [row.get('format') for row in written_rows] == [None, 1]
  assert [row.get('call_id') for row in written_rows] == [None, None]
  served = destination.get_trail(trail_id)
  assert (served['format'], served['synthetic_header'], served['extent']) == (2, True, 2)
  assert destination.get_step(trail_id, 1)['call_id'] == 'upgraded'


def test_import_layout_requires_the_layout(tmp_path):
  with pytest.raises(ValueError, match='no trails store layout'):
    transfer.import_layout(tmp_path / 'absent', LocalStore(tmp_path / 'destination'))


def test_parents_first_uses_upgraded_parent_pointers(monkeypatch):
  headers = [
    {'id': 'child', 'forked_from': {'parent': 'parent', 'ordinal': 0}},
    {'id': 'parent'},
  ]

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

  assert [header['id'] for header in transfer.parents_first(headers)] == ['parent', 'child']


def test_parents_first_follows_both_pointers_and_refuses_a_cycle():
  headers = [
    {'id': 'grandchild', 'summoned_by': {'trail_id': 'child'}},
    {'id': 'child', 'forked_from': {'trail_id': 'root', 'step_id': 0}},
    {'id': 'root', 'forked_from': {'trail_id': 'elsewhere', 'step_id': 3}},
  ]

  ordered = transfer.parents_first(headers)

  assert [header['id'] for header in ordered] == ['root', 'child', 'grandchild']
  with pytest.raises(ValueError, match='cycles through'):
    transfer.parents_first(
      [
        {'id': 'a', 'forked_from': {'trail_id': 'b', 'step_id': 0}},
        {'id': 'b', 'summoned_by': {'trail_id': 'a'}},
      ]
    )
  with pytest.raises(ValueError, match='malformed summoned_by'):
    transfer.parents_first([{'id': 'a', 'summoned_by': 'b'}])


def test_a_layout_export_is_a_store_rewind_can_read(tmp_path):
  source = LocalStore(tmp_path / 'source')
  trail_id = _blaze(source, subject='named')
  layout = Path(tmp_path / 'layout')

  transfer.export_trails(source, [trail_id], layout)

  assert [trail['subject'] for trail in LocalStore(layout).iter_trails(bro='dev')] == ['named']
