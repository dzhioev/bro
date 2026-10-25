import json

import pytest

from bro.files.confinement import MAXIMUM_LANDLOCK_ABI, REQUIRED_PROTECTIONS
from bro.files.protocol import Job, encode_frame
from bro.files.runner import validate_reply
from bro.files.types_test_helper import make_type


def request(tmp_path):
  return Job(make_type(tmp_path), 'info', {}, b'document')


def report():
  return {
    'backend': 'linux',
    'arch': 'x86_64',
    'kernel_abi': MAXIMUM_LANDLOCK_ABI,
    'landlock_abi': MAXIMUM_LANDLOCK_ABI,
    'landlock': ['fs', 'tcp', 'scope'],
    'seccomp': True,
    'rlimits': ['as', 'cpu', 'fsize', 'nofile', 'core'],
  }


def response(**changes):
  return {
    'ok': {'title': 'Invoice', 'page_count': 2},
    'engine': 'test-engine 1',
    'sandbox': report(),
    **changes,
  }


@pytest.mark.parametrize('protection', sorted(REQUIRED_PROTECTIONS))
def test_missing_required_protection_is_an_invalid_reply(tmp_path, protection):
  sandbox = report()
  if protection == 'seccomp':
    sandbox['seccomp'] = False
  else:
    category, name = protection.split('.')
    sandbox[category].remove(name)
  with pytest.raises(RuntimeError, match='invalid file reply'):
    validate_reply(encode_frame(response(sandbox=sandbox)), request(tmp_path))


@pytest.mark.parametrize(
  'value',
  [
    None,
    [],
    {'ok': {}},
    response(engine=1),
    response(ok=[]),
    response(extra=True),
    response(engine='bad\x00'),
    response(ok={'title': '\ud800', 'page_count': 1}),
    response(ok={'title': 'ok', 'page_count': '1'}),
    {'error': {'kind': 'engine', 'message': 'bad', 'extra': True}},
    {'error': {'kind': [], 'message': 'bad'}},
  ],
)
def test_reply_shapes_and_strings_are_strict(tmp_path, value):
  data = json.dumps(value).encode()
  with pytest.raises(RuntimeError, match='invalid file reply'):
    validate_reply(len(data).to_bytes(8, 'big') + data, request(tmp_path))


@pytest.mark.parametrize('data', [b'{"x":1,"x":2}', b'{"ok":NaN}', b'[] trailing', b'\xff'])
def test_malformed_json_is_not_accepted(tmp_path, data):
  with pytest.raises(RuntimeError, match='invalid file reply'):
    validate_reply(len(data).to_bytes(8, 'big') + data, request(tmp_path))


def test_nonfinite_exponent_is_not_a_json_ready_value(tmp_path):
  data = json.dumps(response()).replace('"page_count": 2', '"page_count": 1e999').encode()
  with pytest.raises(RuntimeError, match='invalid file reply'):
    validate_reply(len(data).to_bytes(8, 'big') + data, request(tmp_path))
