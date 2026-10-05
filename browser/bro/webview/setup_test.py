import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from bro.base import credentials
from bro.webview import profile, setup


class Terminal:
  def isatty(self):
    return True


def _profile(*, indexed_db=False, cookie_value='secret'):
  origin = {
    'origin': 'https://example.com',
    'localStorage': [{'name': 'login', 'value': 'yes'}],
  }
  if indexed_db:
    origin['indexedDB'] = [{'name': 'firebase', 'value': {'token': 'stored'}}]
  return {
    'profile_version': profile.PROFILE_VERSION,
    'cookies': [
      {
        'name': 'session',
        'value': cookie_value,
        'domain': 'example.com',
        'path': '/',
      }
    ],
    'origins': [origin],
  }


def _material(store: Path, instance: str = 'work') -> Path:
  path = store / 'creds' / f'cookies+{instance}.cred'
  path.parent.mkdir(parents=True, exist_ok=True)
  return path


@pytest.fixture
def host_setup(monkeypatch, tmp_path):
  store = tmp_path / 'store'
  store.mkdir()
  monkeypatch.delenv('RIDE_ISOLATION', raising=False)
  monkeypatch.setattr(sys, 'stdin', Terminal())
  monkeypatch.setattr(credentials, 'STORE_DIR', str(store))
  return store


def test_setup_refuses_a_managed_session_and_non_terminal(monkeypatch, tmp_path):
  monkeypatch.setenv('RIDE_ISOLATION', 'boxed')
  with pytest.raises(setup.SetupError, match='outside a managed session'):
    setup.setup_profile('work')

  monkeypatch.delenv('RIDE_ISOLATION')
  monkeypatch.setattr(sys, 'stdin', io.StringIO())
  with pytest.raises(setup.SetupError, match='terminal'):
    setup.setup_profile('work')


def test_setup_refuses_a_non_local_source(host_setup):
  (host_setup / credentials.STORE_FILE).write_text(
    json.dumps({'sources': {'cookies+work': {'type': 'ssm', 'parameter': '/browser/profile'}}})
  )

  with pytest.raises(setup.SetupError, match='non-local'):
    setup.setup_profile('work')


def test_setup_seeds_and_keeps_indexed_db_then_writes_a_private_versioned_profile(
  host_setup, monkeypatch
):
  original = _profile(indexed_db=True, cookie_value='before')
  path = _material(host_setup)
  path.write_text(json.dumps(original))
  captured = {
    'cookies': [{**original['cookies'][0], 'value': 'after'}],
    'origins': original['origins'],
  }
  calls = []

  def capture_profile(**keywords):
    calls.append(keywords)
    return captured

  monkeypatch.setattr(setup, '_capture_profile', capture_profile)

  summary = setup.setup_profile('work', url='https://example.com', port=46080)

  assert calls == [
    {
      'seed': {'cookies': original['cookies'], 'origins': original['origins']},
      'url': 'https://example.com',
      'indexed_db': True,
      'port': 46080,
    }
  ]
  written = json.loads(path.read_text())
  assert written == {'profile_version': profile.PROFILE_VERSION, **captured}
  assert path.stat().st_mode & 0o777 == 0o600
  assert 'after' not in summary
  assert 'stored' not in summary
  assert '1 cookies across 1 sites' in summary
  assert '1 IndexedDB origins' in summary


def test_setup_fresh_drops_the_seed_and_indexed_db(host_setup, monkeypatch):
  path = _material(host_setup)
  path.write_text(json.dumps(_profile(indexed_db=True, cookie_value='before')))
  captured = {
    'cookies': [{**_profile()['cookies'][0], 'value': 'after'}],
    'origins': [_profile()['origins'][0]],
  }
  calls = []
  monkeypatch.setattr(
    setup,
    '_capture_profile',
    lambda **keywords: calls.append(keywords) or captured,
  )

  setup.setup_profile('work', fresh=True)

  assert calls[0]['seed'] is None
  assert calls[0]['indexed_db'] is False
  assert 'indexedDB' not in json.loads(path.read_text())['origins'][0]


def test_setup_structural_no_write_ignores_cookie_and_origin_order(host_setup, monkeypatch):
  stored = {
    'profile_version': profile.PROFILE_VERSION,
    'cookies': [
      {'name': 'b', 'domain': 'b.example', 'path': '/', 'value': '2'},
      {'name': 'a', 'domain': 'a.example', 'path': '/', 'value': '1'},
    ],
    'origins': [
      {'origin': 'https://b.example', 'localStorage': []},
      {'origin': 'https://a.example', 'localStorage': [{'name': 'x', 'value': '1'}]},
    ],
  }
  path = _material(host_setup)
  original_bytes = json.dumps(stored, indent=2).encode()
  path.write_bytes(original_bytes)
  captured = {
    'cookies': list(reversed(stored['cookies'])),
    'origins': list(reversed(stored['origins'])),
  }
  monkeypatch.setattr(setup, '_capture_profile', lambda **_keywords: captured)

  assert setup.setup_profile('work') == 'profile unchanged; wrote nothing'
  assert path.read_bytes() == original_bytes


def test_setup_aborts_when_material_changes_during_capture(host_setup, monkeypatch):
  path = _material(host_setup)
  path.write_text(json.dumps(_profile(cookie_value='before')))

  def capture_profile(**_keywords):
    path.write_text(json.dumps(_profile(cookie_value='hand-edit')))
    current = _profile(cookie_value='capture')
    return {'cookies': current['cookies'], 'origins': current['origins']}

  monkeypatch.setattr(setup, '_capture_profile', capture_profile)

  with pytest.raises(setup.SetupError, match='changed since it was read'):
    setup.setup_profile('work')
  assert json.loads(path.read_text())['cookies'][0]['value'] == 'hand-edit'


def test_setup_refuses_an_empty_capture_without_writing(host_setup, monkeypatch):
  monkeypatch.setattr(
    setup,
    '_capture_profile',
    lambda **_keywords: {'cookies': [], 'origins': []},
  )

  with pytest.raises(setup.SetupError, match='empty'):
    setup.setup_profile('work')
  assert not _material(host_setup).exists()


def test_dependency_light_webview_modules_do_not_load_ride():
  completed = subprocess.run(
    [
      sys.executable,
      '-c',
      (
        'import sys; import bro.webview.worker, bro.webview.serve; '
        "assert not any(name == 'ride' or name.startswith('ride.') for name in sys.modules)"
      ),
    ],
    capture_output=True,
    text=True,
  )

  assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
  'messages',
  [
    [{'event': 'state', 'state': {'secret': 'profile-secret-marker'}}],
    [
      {'event': 'ready'},
      {
        'event': 'state',
        'state': {'cookies': [{'value': 'profile-secret-marker'}], 'origins': []},
        'unexpected': True,
      },
    ],
  ],
)
def test_setup_malformed_replies_do_not_expose_profile_values(messages, monkeypatch):
  encoded = b''.join(json.dumps(message).encode() + b'\n' for message in messages)
  process = SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(encoded), wait=lambda: 0)

  @contextlib.contextmanager
  def foreground_run(_worker_type, _spec):
    yield SimpleNamespace(process=process, published_ports=((46080, setup.VNC_PORT),))

  monkeypatch.setattr('ride.worker_container.foreground_worker_run', foreground_run)
  monkeypatch.setattr('builtins.input', lambda _prompt: '')

  with pytest.raises(setup.SetupError) as raised:
    setup._capture_profile(seed=None, url=None, indexed_db=False, port=None)

  assert 'profile-secret-marker' not in str(raised.value)


def test_setup_bounded_reader_refuses_oversize_and_eof(monkeypatch):
  monkeypatch.setattr(setup, 'EXCHANGE_MAX_BYTES', 8)
  with pytest.raises(setup.SetupError, match='exceeds'):
    setup._read_line(io.BytesIO(b'{"event":"ready"}\n'), 'readiness')
  with pytest.raises(setup.SetupError, match='ended'):
    setup._read_line(io.BytesIO(), 'readiness')
