from unittest.mock import Mock

import pytest

import ride.bro_run
from bro.workspace.store import ScopedSecrets
from ride.identity import bro_git_identity_env

_SCOPED = ScopedSecrets(required={'github'}, optional={'openai', 'trails'}, docker_sock=False)


@pytest.fixture(autouse=True)
def trails_mounts(monkeypatch):
  helper = Mock(return_value=())
  monkeypatch.setattr(ride.bro_run, 'local_trails_mounts', helper)
  return helper


_COMMAND = ['bro', 'run', 'dev', 'hi', '--in-place']


def _describe(*args, **kwargs):
  kwargs.setdefault('scoped', _SCOPED)
  return ride.bro_run.describe(*args, workspace_name='ws', **kwargs)


def test_describe_carries_the_given_command():
  launch = _describe('dev', _COMMAND)
  assert launch.command == _COMMAND


def test_describe_env_carries_identity_and_bro():
  launch = _describe('dev', _COMMAND)
  assert launch.env == {'RIDE_BRO': 'dev', **bro_git_identity_env('dev')}


def test_describe_carries_the_local_trails_mount_in_the_launch(trails_mounts):
  trails_mounts.return_value = ('/host/trails:/var/ride/trails',)

  launch = _describe('dev', _COMMAND)

  trails_mounts.assert_called_once_with(_SCOPED)
  assert launch.extra_mounts == ('/host/trails:/var/ride/trails',)


def test_describe_carries_the_given_scope():
  launch = _describe('dev', _COMMAND)
  assert launch.secrets == {'github'}
  assert launch.optional_secrets == {'openai', 'trails'}
  assert launch.docker_sock is False


def test_describe_base_ref_rides_ride_base_ref():
  launch = _describe('dev', _COMMAND, base_ref='REF-SHA')
  assert launch.env['RIDE_BASE_REF'] == 'REF-SHA'


def test_describe_encodes_summoner_as_compact_json():
  launch = _describe('dev', _COMMAND, summoner={'target': 'bro', 'trail_id': 'trail-123'})
  assert launch.env['RIDE_SUMMONER'] == '{"target":"bro","trail_id":"trail-123"}'
