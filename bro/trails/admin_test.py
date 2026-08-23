import pytest

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

  with pytest.raises(SystemExit, match='administers nothing'):
    main(['trails', 'delete', trail_id])
