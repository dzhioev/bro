import pytest

from bro.trails import model
from bro.trails.admin import main
from bro.trails.local import LocalStore
from bro.trails.model import BlazeRequest
from bro.trails.store import PermissionDenied


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


@pytest.fixture
def store(tmp_path, monkeypatch):
  local = LocalStore(tmp_path)
  monkeypatch.setattr('bro.trails.admin.default_store', lambda: local)
  return local


def test_migrate_reports_the_current_format(store, capsys):
  trail_id = _blaze(store)

  code = main(['trails', 'migrate', trail_id])

  assert code == 0
  assert capsys.readouterr().out == (f'{trail_id}: format {model.TRAIL_FORMAT}, 0 rows migrated\n')


def test_delete_takes_a_whole_lineage_whatever_order_it_is_named_in(store, capsys):
  root = _blaze(store)
  child = _blaze(store, forked_from={'trail_id': root, 'step_id': 0})
  grandchild = _blaze(store, forked_from={'trail_id': child, 'step_id': 0})

  code = main(['trails', 'delete', root, child, grandchild])

  assert code == 0
  assert capsys.readouterr().err == ''
  assert store.list_trails()['trails'] == []


def test_delete_reports_what_stayed_and_exits_nonzero(store, capsys):
  root = _blaze(store)
  _blaze(store, forked_from={'trail_id': root, 'step_id': 0})

  code = main(['trails', 'delete', root, 'absent'])

  errors = capsys.readouterr().err
  assert code == 1
  assert f'trail {root} has forks' in errors
  assert 'trail not found: absent' in errors
  assert [trail['id'] for trail in store.list_trails()['trails']] != []


def test_a_credential_without_the_admin_permission_stops_the_run(store, monkeypatch):
  trail_id = _blaze(store)

  def refuse(_: str) -> dict:
    raise PermissionDenied('this trails token may not admin')

  monkeypatch.setattr(store, 'delete_trail', refuse)

  with pytest.raises(SystemExit, match='is refused'):
    main(['trails', 'delete', trail_id])


def test_export_then_import_moves_a_trail_between_stores(store, capsys, tmp_path, monkeypatch):
  trail_id = _blaze(store)
  layout = tmp_path / 'layout'

  exported = main(['trails', 'export', '-o', str(layout), trail_id])

  assert exported == 0
  assert capsys.readouterr().out == f'{trail_id}: 1 steps\n'
  destination = LocalStore(tmp_path / 'destination')
  monkeypatch.setattr('bro.trails.admin.default_store', lambda: destination)

  imported = main(['trails', 'import', str(layout)])

  assert imported == 0
  assert capsys.readouterr().out == f'{trail_id}: 1 steps\n'
  assert destination.get_trail(trail_id) == store.get_trail(trail_id)
