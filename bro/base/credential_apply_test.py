import base64
import json
import os
from pathlib import Path

import pytest

from bro.base import credential_apply, credentials
from bro.base.credential_environment import overlay


@pytest.fixture
def publication(tmp_path, monkeypatch):
  registry = {
    'github': credentials.CredentialKind(
      'github',
      'git identity',
      install={
        'files': {'token': {'secret': 'github'}},
        'env': {'GIT_TOKEN': {'secret': 'github'}},
      },
    ),
    'brave': credentials.CredentialKind('brave', 'search key'),
  }
  monkeypatch.setattr(credentials, 'default_registry', lambda: registry)
  source = tmp_path / 'source'
  (source / 'creds').mkdir(parents=True)
  for name, value in {'github+one': 'first', 'github+two': 'second', 'brave': 'search'}.items():
    (source / 'creds' / (name + '.cred')).write_text(value)
  store_path = tmp_path / 'session'
  source_store = credentials.Store(registry, source, {'github': 'one'})
  files, kinds = credentials.build_scoped_store(source_store, ['github', 'brave'])
  for name, value in files.items():
    credential_apply.atomic_file(store_path / name, value)
  store = credentials.Store(registry, store_path, {})
  directory = tmp_path / 'environment'
  environment = {'PATH': os.environ['PATH'], 'BRO_INSTALL_DIR': str(directory)}
  environment.update(credentials.install_hooks(registry, kinds, store, directory, environment))

  def request(operation, pick=None):
    selected = credentials.Store(registry, source, {'github': pick or 'two'})
    files, _ = credentials.build_scoped_store(selected, [] if pick is None else ['github'])
    return {
      'operation': operation,
      'changed': ['github'],
      'files': {name: base64.b64encode(value).decode() for name, value in files.items()},
    }

  return credentials.Store(registry, store_path, {}), directory, environment, request


def test_refresh_pick_revoke_grant_and_unrelated_material_stays_in_place(publication):
  store, directory, environment, request = publication
  assert store.get('github') == 'first'
  unrelated = store.material_path('brave')
  inode = unrelated.stat().st_ino
  credential_apply.apply_request(store.store_dir, directory, request('first-operation', 'two'))
  assert store.get('github') == 'second'
  assert store.try_get_instance('github+one') is None
  assert (directory / 'token').read_text() == 'second'
  assert overlay(environment)['GIT_TOKEN'] == 'second'
  assert store.get('brave') == 'search'
  assert unrelated.stat().st_ino == inode
  credential_apply.apply_request(store.store_dir, directory, request('second-operation'))
  assert store.try_get('github') is None
  assert store.try_get_instance('github+two') is None
  assert 'GIT_TOKEN' not in overlay(environment)
  assert not (directory / 'token').exists()
  credential_apply.apply_request(store.store_dir, directory, request('third-operation', 'one'))
  assert store.get('github') == 'first'
  assert overlay(environment)['GIT_TOKEN'] == 'first'
  assert store.generation == 3


def test_postcommit_failure_is_retried_and_revocation_precedes_hooks(publication, monkeypatch):
  store, directory, _, request = publication
  publish = credential_apply.publish_hooks
  calls = []

  def interrupted(*args):
    calls.append(store.generation)
    assert store.try_get_instance('github+one') is None
    if len(calls) == 1:
      raise OSError('interrupted publication')
    return publish(*args)

  monkeypatch.setattr(credential_apply, 'publish_hooks', interrupted)
  operation = request('retry-operation')
  assert credential_apply.apply_request(store.store_dir, directory, operation) == 1
  assert calls == [1, 1]
  assert credential_apply.apply_request(store.store_dir, directory, operation) == 1


def test_precommit_failure_keeps_the_live_store_and_hooks(publication, monkeypatch):
  store, directory, environment, request = publication
  original = credential_apply.atomic_file

  def failed_index(path, *args):
    if path == store.store_dir / credentials.STORE_FILE:
      raise OSError('index write failed')
    return original(path, *args)

  monkeypatch.setattr(credential_apply, 'atomic_file', failed_index)
  with pytest.raises(OSError, match='index write failed'):
    credential_apply.apply_request(store.store_dir, directory, request('failed-operation', 'two'))
  assert store.get('github') == 'first'
  assert overlay(environment)['GIT_TOKEN'] == 'first'
  assert (directory / 'token').read_text() == 'first'
  assert store.generation == 0


def test_read_retries_across_multiple_generations_and_never_substitutes_an_instance(
  publication, monkeypatch
):
  store, directory, _, request = publication
  fetch = credentials._fetch_source
  operations = iter(
    [request('race-one', 'two'), request('race-two', 'one'), request('race-three', 'two')]
  )
  racing = True

  def racing_fetch(source, path, name):
    nonlocal racing
    if racing and name.startswith('github'):
      try:
        operation = next(operations)
      except StopIteration:
        pass
      else:
        racing = False
        credential_apply.apply_request(store.store_dir, directory, operation)
        racing = True
    return fetch(source, path, name)

  monkeypatch.setattr(credentials, '_fetch_source', racing_fetch)
  assert store.get('github') == 'second'
  assert store.generation == 3
  assert store.try_get_instance('github+one') is None


def test_missing_indexed_file_is_corruption_but_unindexed_material_is_unreadable(publication):
  store, _, _, _ = publication
  (store.store_dir / 'creds' / 'github+hidden.cred').write_text('hidden')
  assert store.try_get_instance('github+hidden') is None
  store.material_path('github').unlink()
  with pytest.raises(ValueError, match='corruption'):
    store.get('github')
  assert 'github+hidden' not in store.instance_names()


def test_environment_overlay_preserves_quoted_newlines_and_unsets(tmp_path):
  from bro.base.credential_environment import ENVIRONMENT_FILE, render

  (tmp_path / ENVIRONMENT_FILE).write_text(render({'TOKEN': "line one\n'line two'", 'OLD': None}))
  result = overlay({'BRO_INSTALL_DIR': str(tmp_path), 'OLD': 'expired', 'OTHER': 'unchanged'})
  assert result['TOKEN'] == "line one\n'line two'"
  assert 'OLD' not in result
  assert result['OTHER'] == 'unchanged'


def test_completed_operation_replay_cannot_undo_a_later_revocation(publication):
  store, directory, _, request = publication
  first = request('completed-one', 'two')
  assert credential_apply.apply_request(store.store_dir, directory, first) == 1
  assert credential_apply.apply_request(store.store_dir, directory, request('completed-two')) == 2
  assert credential_apply.apply_request(store.store_dir, directory, first) == 1
  assert store.try_get('github') is None
  assert store.generation == 2
  assert list((store.store_dir / '.operations').glob('completed-*.json'))
  assert all(
    path.name.endswith('.done.json') for path in (store.store_dir / '.operations').iterdir()
  )


def test_replanning_wrappers_excludes_the_installation_binary_directory(tmp_path):
  directory = tmp_path / 'hooks'
  binaries = directory / 'bin'
  binaries.mkdir(parents=True)
  (binaries / 'gh').write_text('old wrapper')
  (binaries / 'gh').chmod(0o700)
  real = tmp_path / 'real'
  real.mkdir()
  (real / 'gh').write_text('#!/bin/sh\nprintf real-command')
  (real / 'gh').chmod(0o700)
  store = credentials.Store({}, tmp_path, {})
  script = credentials._command_wrapper(
    'github', 'gh', {}, directory, {'PATH': os.pathsep.join([str(binaries), str(real)])}, store
  )
  assert str(real / 'gh') in script
  assert str(binaries / 'gh') not in script


def test_actual_open_race_retries_when_the_index_moves(publication, monkeypatch):
  store, directory, _, request = publication
  path = store.material_path('github')
  read_text = Path.read_text
  once = False

  def removed_before_open(candidate, *args, **kwargs):
    nonlocal once
    if candidate == path and not once:
      once = True
      credential_apply.apply_request(store.store_dir, directory, request('open-race', 'two'))
    return read_text(candidate, *args, **kwargs)

  monkeypatch.setattr(Path, 'read_text', removed_before_open)
  assert store.get('github') == 'second'


def test_empty_material_directory_is_created_before_committing(tmp_path, monkeypatch):
  monkeypatch.setattr(credentials, 'default_registry', dict)
  index = {'generation': 0, 'material': {}, 'sources': {}, 'defaults': [], 'kinds': []}
  store = tmp_path / 'store'
  credential_apply.atomic_file(store / credentials.STORE_FILE, json.dumps(index).encode())
  directory = tmp_path / 'hooks'
  credentials.install_hooks({}, set(), credentials.Store({}, store, {}), directory, {})
  request = {
    'operation': 'empty',
    'changed': [],
    'files': {credentials.STORE_FILE: base64.b64encode(json.dumps(index).encode()).decode()},
  }
  assert credential_apply.apply_request(store, directory, request) == 1
  assert (store / credentials.MATERIAL_DIR).is_dir()


def test_transitive_defaults_are_merged_by_kind_before_index_publication(publication):
  store, directory, _, request = publication
  incoming = request('transitive', 'two')
  index = json.loads(base64.b64decode(incoming['files'][credentials.STORE_FILE]))
  index['defaults'].append('brave+')
  index['material']['brave'] = 'creds/brave.cred'
  incoming['files']['creds/brave.cred'] = base64.b64encode(b'search').decode()
  incoming['files'][credentials.STORE_FILE] = base64.b64encode(json.dumps(index).encode()).decode()
  credential_apply.apply_request(store.store_dir, directory, incoming)
  assert store.get('github') == 'second'
  assert store.get('brave') == 'search'
