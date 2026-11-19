import importlib.metadata
import subprocess
import sys
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

import bro.worker_types as worker_types
from bro.worker_types import LaunchRequest, PeerDescription, WorkerType
from bro.workspace.paths import CONTAINER_ARTIFACTS_ROOT


class AlphaType(WorkerType):
  name = 'alpha'

  def talk(self, request: LaunchRequest):
    return frozenset()

  def launch(self, request: LaunchRequest):
    raise NotImplementedError


class ZetaType(AlphaType):
  name = 'zeta'


class MismatchedType(AlphaType):
  name = 'other'


class NotAType:
  pass


def _entry(name: str, value: str) -> importlib.metadata.EntryPoint:
  return importlib.metadata.EntryPoint(name, value, worker_types.WORKER_TYPE_GROUP)


def test_installed_type_loads_only_the_named_entry(monkeypatch):
  alpha = _entry('alpha', f'{__name__}:AlphaType')
  unread = _entry('unread', 'does.not.exist:Type')
  monkeypatch.setattr(worker_types, '_entry_points', lambda: (unread, alpha))

  assert worker_types.installed_type('alpha') is AlphaType


def test_installed_types_are_sorted(monkeypatch):
  second = _entry('zeta', f'{__name__}:ZetaType')
  first = _entry('alpha', f'{__name__}:AlphaType')
  monkeypatch.setattr(worker_types, '_entry_points', lambda: (second, first))
  assert list(worker_types.installed_types()) == ['alpha', 'zeta']


def test_duplicate_worker_type_raises(monkeypatch):
  entry = _entry('alpha', f'{__name__}:AlphaType')
  monkeypatch.setattr(worker_types, '_entry_points', lambda: (entry, entry))
  with pytest.raises(ValueError, match='duplicate worker type'):
    worker_types.installed_types()


def test_entry_point_and_class_names_must_match(monkeypatch):
  monkeypatch.setattr(
    worker_types,
    '_entry_points',
    lambda: (_entry('alpha', f'{__name__}:MismatchedType'),),
  )
  with pytest.raises(ValueError, match='loads class named'):
    worker_types.installed_type('alpha')


def test_entry_point_must_load_a_worker_type(monkeypatch):
  monkeypatch.setattr(
    worker_types,
    '_entry_points',
    lambda: (_entry('alpha', f'{__name__}:NotAType'),),
  )
  with pytest.raises(TypeError, match='WorkerType subclass'):
    worker_types.installed_type('alpha')


@pytest.mark.parametrize('name', ['', 'Alpha', 'alpha.beta', '-alpha', 'alpha_2'])
def test_worker_type_name_grammar(name):
  with pytest.raises(ValueError, match='expected'):
    worker_types.type_name(name)


def test_unknown_type_lists_installed_names_without_loading_them(monkeypatch):
  monkeypatch.setattr(
    worker_types,
    '_entry_points',
    lambda: (
      _entry('alpha', 'does.not.exist:Type'),
      _entry('zeta', 'also.missing:Type'),
    ),
  )
  with pytest.raises(KeyError, match='installed types: alpha, zeta'):
    worker_types.installed_type('missing')


def test_loading_the_bro_type_does_not_import_broker_machinery():
  result = subprocess.run(
    [
      sys.executable,
      '-c',
      'import sys; from bro.worker_types import installed_type; '
      "installed_type('bro'); "
      "assert not any(name.startswith('bro.broker') for name in sys.modules)",
    ],
    capture_output=True,
    text=True,
  )
  assert result.returncode == 0, result.stderr


@pytest.fixture
def peer(tmp_path):
  tree = tmp_path / 'tree'
  tree.mkdir()
  return PeerDescription('mission', 'workspace', tree, Path('/tree'), 'bro', False, None, (), 0)


def test_wire_path_maps_a_container_view(peer):
  assert worker_types.resolve_wire_path(peer, '/tree/clone/report') == peer.tree / 'clone/report'


@pytest.mark.parametrize('path', ['relative', '/etc/hosts', '/tree/../outside', '/treehouse/file'])
def test_wire_path_rejects_invalid_paths_and_names_them(peer, path):
  with pytest.raises(ValueError) as raised:
    worker_types.resolve_wire_path(peer, path)
  assert repr(path) in str(raised.value)


@pytest.mark.parametrize('boxed', [False, True])
def test_wire_path_refuses_a_symlink_outside_the_tree(peer, tmp_path, boxed):
  (peer.tree / 'link').symlink_to(tmp_path)
  if not boxed:
    peer = replace(peer, tree_view=peer.tree)
  path = str(peer.tree_view / 'link/file')
  with pytest.raises(ValueError, match='escapes the tree') as raised:
    worker_types.resolve_wire_path(peer, path)
  assert repr(path) in str(raised.value)


def test_unboxed_wire_path_accepts_a_symlink_to_the_runtime_root(peer, tmp_path):
  alias = tmp_path / 'alias'
  alias.symlink_to(peer.tree, target_is_directory=True)
  peer = replace(peer, tree_view=peer.tree)
  assert worker_types.resolve_wire_path(peer, str(alias / 'report')) == peer.tree / 'report'


@pytest.mark.parametrize('path', ['/tree/loop', '/tree/nul\0path'])
def test_wire_path_resolution_errors_name_the_peer_path(peer, path):
  (peer.tree / 'loop').symlink_to('loop')
  with pytest.raises(ValueError) as raised:
    worker_types.resolve_wire_path(peer, path)
  assert repr(path) in str(raised.value)


def test_wire_path_resolves_a_symlinked_tree(peer, tmp_path):
  alias = tmp_path / 'alias'
  alias.symlink_to(peer.tree, target_is_directory=True)
  assert (
    worker_types.resolve_wire_path(replace(peer, tree=alias), '/tree/report')
    == peer.tree / 'report'
  )


_CONTAINER_FILES = {
  'Dockerfile': b'# syntax=docker/dockerfile:1\nARG RUNTIME_IMAGE\nFROM ${RUNTIME_IMAGE}\n',
  'worker/data.bin': b'payload',
}


def _worker_container(**changes):
  values = {
    'files': _CONTAINER_FILES,
    'command': ('worker', '--serve'),
    'env': {'WORKER_MODE': 'test'},
    'published_ports': {8080: None, 9090: 49090},
  }
  values.update(changes)
  return worker_types.WorkerContainer(**values)


def test_worker_container_copies_its_mappings_and_has_a_stable_image_hash():
  files = dict(_CONTAINER_FILES)
  env = {'WORKER_MODE': 'test'}
  ports = {8080: None}
  container = _worker_container(files=files, env=env, published_ports=ports)
  expected = container.image_hash('bro/ride-runtime:one')
  files['worker/data.bin'] = b'changed'
  env['WORKER_MODE'] = 'changed'
  ports[9090] = None

  assert container.files['worker/data.bin'] == b'payload'
  assert container.env == {'WORKER_MODE': 'test'}
  assert container.published_ports == {8080: None}
  assert (
    _worker_container(files=dict(reversed(_CONTAINER_FILES.items()))).image_hash(
      'bro/ride-runtime:one'
    )
    == expected
  )
  assert container.image_hash('bro/ride-runtime:two') != expected
  assert (
    _worker_container(files={**_CONTAINER_FILES, 'worker/data.bin': b'changed'}).image_hash(
      'bro/ride-runtime:one'
    )
    != expected
  )
  assert container.artifact_view == PurePosixPath(CONTAINER_ARTIFACTS_ROOT)
  assert (
    _worker_container(artifact_view=PurePosixPath('/tree/artifacts')).image_hash(
      'bro/ride-runtime:one'
    )
    == expected
  )


@pytest.mark.parametrize(
  'artifact_view',
  [
    PurePosixPath('artifacts'),
    '/tree/../artifacts',
    '/tree//artifacts',
    '/tree/artifacts/',
    '//tree/artifacts',
    '/tree/./artifacts',
    '/tree/artifacts\0hidden',
  ],
)
def test_worker_container_refuses_non_normalized_artifact_views(artifact_view):
  with pytest.raises(ValueError, match='absolute normalized POSIX path'):
    _worker_container(artifact_view=artifact_view)


@pytest.mark.parametrize(
  'path',
  [
    '',
    '/Dockerfile',
    '../Dockerfile',
    'worker/../Dockerfile',
    'worker//Dockerfile',
    './Dockerfile',
    'worker\0hidden',
  ],
)
def test_worker_container_refuses_non_normalized_file_paths(path):
  files = dict(_CONTAINER_FILES)
  files[path] = b'data'
  with pytest.raises(ValueError, match='relative POSIX path'):
    _worker_container(files=files)


@pytest.mark.parametrize(
  'dockerfile',
  [
    None,
    b'FROM python:3.12\n',
    b'FROM ${RUNTIME_IMAGE}\n',
    b'ARG RUNTIME_IMAGE\nRUN true\n',
    b'ARG RUNTIME_IMAGE\nFROM ${RUNTIME_IMAGE} AS worker\n',
  ],
)
def test_worker_container_requires_the_runtime_image_dockerfile_prelude(dockerfile):
  files = dict(_CONTAINER_FILES)
  if dockerfile is None:
    files.pop('Dockerfile')
  else:
    files['Dockerfile'] = dockerfile
  with pytest.raises(ValueError, match='Dockerfile'):
    _worker_container(files=files)


@pytest.mark.parametrize('command', [(), ('',), ('worker', 'bad\0argument'), ['worker']])
def test_worker_container_refuses_invalid_commands(command):
  with pytest.raises(ValueError, match='command'):
    _worker_container(command=command)


def test_worker_container_allows_an_empty_argument_after_the_executable():
  assert _worker_container(command=('worker', '')).command == ('worker', '')


@pytest.mark.parametrize(
  'name', ['BROKER_CHANNEL', 'RIDE_WORKSPACE', 'BRO_FEATURE', 'HOME', 'PATH']
)
def test_worker_container_refuses_host_owned_environment(name):
  with pytest.raises(ValueError, match='host-owned'):
    _worker_container(env={name: 'value'})


@pytest.mark.parametrize('name', ['', '9NAME', 'NAME-WITH-DASH', 'HOME=value', 'BAD\0NAME'])
def test_worker_container_refuses_invalid_environment_names(name):
  with pytest.raises(ValueError, match='environment variable names'):
    _worker_container(env={name: 'value'})


def test_worker_container_refuses_a_nul_in_an_environment_value():
  with pytest.raises(ValueError, match='NUL-free'):
    _worker_container(env={'WORKER_MODE': 'bad\0value'})


@pytest.mark.parametrize(
  'ports',
  [
    {0: None},
    {65536: None},
    {True: None},
    {8080: 1023},
    {8080: 65536},
    {8080: True},
    {8080: 49090, 9090: 49090},
    (8080,),
  ],
)
def test_worker_container_refuses_invalid_published_ports(ports):
  with pytest.raises(ValueError, match='published ports'):
    _worker_container(published_ports=ports)
