from unittest.mock import Mock

import pytest

import bro.launch.bro_run
from bro.launch.identity import bro_git_identity_env
from bro.workspace.store import ScopedSecrets

_SCOPED = ScopedSecrets(required={'github'}, optional={'openai', 'trails'}, docker_sock=False)


@pytest.fixture(autouse=True)
def trails_mounts(monkeypatch):
  helper = Mock(return_value=())
  monkeypatch.setattr(bro.launch.bro_run, 'local_trails_mounts', helper)
  return helper


def _describe(*args, **kwargs):
  kwargs.setdefault('verb', 'run')
  kwargs.setdefault('scoped', _SCOPED)
  return bro.launch.bro_run.describe(*args, workspace_name='ws', **kwargs)


def test_describe_composes_the_in_place_pinned_command():
  launch = _describe('dev', ['hi', '--fast'], verb='run')
  assert launch.command == ['bro', 'run', 'dev', 'hi', '--fast', '--in-place']


def test_describe_pins_the_chat_verb():
  launch = _describe('dev', ['hi'], verb='chat')
  assert launch.command == ['bro', 'chat', 'dev', 'hi', '--in-place']


def test_describe_env_carries_identity_and_bro():
  launch = _describe('dev', ['hi'])
  assert launch.env == {'RIDE_BRO': 'dev', **bro_git_identity_env('dev')}


def test_describe_carries_the_local_trails_mount_in_the_launch(trails_mounts):
  trails_mounts.return_value = ('/host/trails:/var/ride/trails',)

  launch = _describe('dev', ['hi'])

  trails_mounts.assert_called_once_with(_SCOPED)
  assert launch.extra_mounts == ('/host/trails:/var/ride/trails',)


def test_describe_carries_the_given_scope():
  launch = _describe('dev', ['hi'])
  assert launch.secrets == {'github'}
  assert launch.optional_secrets == {'openai', 'trails'}
  assert launch.docker_sock is False


def test_describe_base_ref_rides_ride_base_ref():
  launch = _describe('dev', ['hi'], base_ref='REF-SHA')
  assert launch.env['RIDE_BASE_REF'] == 'REF-SHA'


def test_describe_encodes_summoner_as_compact_json():
  launch = _describe('dev', ['hi'], summoner={'target': 'bro', 'trail_id': 'trail-123'})
  assert launch.env['RIDE_SUMMONER'] == '{"target":"bro","trail_id":"trail-123"}'


def test_describe_no_trails_drops_secret_and_disables_recording(trails_mounts):
  launch = _describe('dev', ['hi'], trails=False)
  assert launch.optional_secrets == {'openai'}
  assert launch.env['TRAILS_DISABLED'] == '1'
  # a run that records nothing binds no trails root
  trails_mounts.assert_not_called()
  assert launch.extra_mounts == ()


def test_describe_no_trails_drops_a_mapped_trails_instance():
  scoped = ScopedSecrets(required={'github', 'trails+eu'}, optional=set(), docker_sock=False)
  launch = _describe('dev', ['hi'], trails=False, scoped=scoped)
  assert 'trails+eu' not in launch.secrets
  assert launch.env['TRAILS_DISABLED'] == '1'
