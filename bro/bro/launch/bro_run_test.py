import cw.bro_run
from cw.constants import bro_git_identity_env


def _describe(*args, **kwargs):
  kwargs.setdefault('verb', 'run')
  return cw.bro_run.describe(*args, workspace_name='ws', **kwargs)


def test_describe_composes_the_in_place_pinned_command():
  launch = _describe('ppp-dev', ['hi', '--slow'], verb='run')
  assert launch.command == ['bro', 'run', 'ppp-dev', 'hi', '--slow', '--in-place']


def test_describe_pins_the_chat_verb():
  launch = _describe('ppp-dev', ['hi'], verb='chat')
  assert launch.command == ['bro', 'chat', 'ppp-dev', 'hi', '--in-place']


def test_describe_env_carries_identity_and_bro():
  launch = _describe('ppp-dev', ['hi'])
  assert launch.env == {'CW_BRO': 'ppp-dev', **bro_git_identity_env()}


def test_describe_scopes_to_the_bro():
  launch = _describe('ppp-dev', ['hi'])
  # ppp-dev's manifest (github + brog) + its llm key + the mandatory trails sink
  assert {'github', 'brog', 'trails'} <= set(launch.secrets)
  # ppp-dev doesn't deploy → no docker socket
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
