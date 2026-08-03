import bro.launch.bro_run
from bro.launch.identity import bro_git_identity_env
from bro.workspace.store import ScopedSecrets

_SCOPED = ScopedSecrets(required={'github', 'trails'}, optional={'openai'}, docker_sock=False)


def _describe(*args, **kwargs):
  kwargs.setdefault('verb', 'run')
  kwargs.setdefault('scoped', _SCOPED)
  return bro.launch.bro_run.describe(*args, workspace_name='ws', **kwargs)


def test_describe_composes_the_in_place_pinned_command():
  launch = _describe('ppp-dev', ['hi', '--fast'], verb='run')
  assert launch.command == ['bro', 'run', 'ppp-dev', 'hi', '--fast', '--in-place']


def test_describe_pins_the_chat_verb():
  launch = _describe('ppp-dev', ['hi'], verb='chat')
  assert launch.command == ['bro', 'chat', 'ppp-dev', 'hi', '--in-place']


def test_describe_env_carries_identity_and_bro():
  launch = _describe('ppp-dev', ['hi'])
  assert launch.env == {'CW_BRO': 'ppp-dev', **bro_git_identity_env('ppp-dev')}


def test_describe_carries_the_given_scope():
  launch = _describe('ppp-dev', ['hi'])
  assert launch.secrets == {'github', 'trails'}
  assert launch.optional_secrets == {'openai'}
  assert launch.docker_sock is False


def test_describe_base_ref_rides_cw_base_ref():
  launch = _describe('ppp-dev', ['hi'], base_ref='REF-SHA')
  assert launch.env['CW_BASE_REF'] == 'REF-SHA'


def test_describe_encodes_summoner_as_compact_json():
  launch = _describe('ppp-dev', ['hi'], summoner={'target': 'pm', 'trail_id': 'trail-123'})
  assert launch.env['CW_SUMMONER'] == '{"target":"pm","trail_id":"trail-123"}'


def test_describe_no_trails_drops_secret_and_disables_recording():
  launch = _describe('ppp-dev', ['hi'], trails=False)
  assert 'trails' not in launch.secrets
  assert launch.env['TRAILS_DISABLED'] == '1'


def test_describe_no_trails_drops_a_mapped_trails_instance():
  scoped = ScopedSecrets(required={'github', 'trails+eu'}, optional=set(), docker_sock=False)
  launch = _describe('ppp-dev', ['hi'], trails=False, scoped=scoped)
  assert 'trails+eu' not in launch.secrets
  assert launch.env['TRAILS_DISABLED'] == '1'
