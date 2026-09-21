import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from bro.webview import worker
from bro.worker_types import Host, LaunchDenied, LaunchRequest, PeerDescription, installed_type


def _owner(
  tmp_path: Path,
  *,
  mission: str = 'owner',
  type: str = 'bro',
  permits: frozenset[str] = frozenset(),
) -> PeerDescription:
  return PeerDescription(
    mission=mission,
    workspace='workspace',
    tree=tmp_path,
    type=type,
    bro='bro-dev' if type == 'bro' else None,
    permits=permits,
    member=None,
    expected=False,
    artifact_view=None,
    published_ports=(),
    depth=0,
  )


def _request(
  tmp_path: Path,
  args: dict[str, Any],
  *,
  mission: str = 'owner',
  type: str = 'bro',
  permits: frozenset[str] = frozenset(),
  request_id: str = 'webview-1',
) -> LaunchRequest:
  return LaunchRequest(
    id=request_id,
    type=worker.WEBVIEW,
    args=args,
    owner=_owner(tmp_path, mission=mission, type=type, permits=permits),
    requested_talk=frozenset(),
    timeout=None,
    share=(),
    manual=False,
  )


def _worker_type() -> worker.WebviewType:
  return worker.WebviewType(cast(Host, object()))


def _denial(tmp_path: Path, args: dict[str, Any], **request_options: Any) -> str:
  with pytest.raises(LaunchDenied) as raised:
    _worker_type().launch(_request(tmp_path, args, **request_options))
  return str(raised.value)


class TestWebviewType:
  def test_registered_type_and_declarations(self):
    worker_type = _worker_type()

    assert installed_type(worker.WEBVIEW) is worker.WebviewType
    assert worker_type.name == 'webview'
    assert worker_type.permits == frozenset({'vnc'})
    assert worker_type.default_timeout is None
    assert worker_type.widens_talk is False
    assert worker_type.manual is False
    assert worker_type.talk(cast(LaunchRequest, object())) == frozenset(
      {'owner.question', 'worker.say'}
    )

  def test_default_launch_builds_the_isolated_container(self, tmp_path):
    run = _worker_type().launch(_request(tmp_path, {}))

    assert run.spec.command == ('webview', 'serve')
    assert run.spec.published_ports == ()
    assert run.spec.artifact_view == worker.ARTIFACT_VIEW
    assert set(run.spec.files) == {'Dockerfile'}
    assert json.loads(run.spec.env['WEBVIEW_OPTIONS']) == {
      'vnc': False,
      'allowed_origins': [],
      'blocked_origins': [],
    }
    assert run.extension == worker.WebviewFacts(False, (), ())
    assert run.permits == frozenset()

  def test_vnc_and_origins_reach_the_container_facts(self, tmp_path):
    run = _worker_type().launch(
      _request(
        tmp_path,
        {
          'vnc': True,
          'allowed_origins': ['https://one.example', '*.trusted.example'],
          'blocked_origins': ['https://blocked.example'],
        },
        permits=frozenset({worker.VNC_PERMIT}),
      )
    )

    assert run.spec.published_ports == (worker.VNC_PORT,)
    assert json.loads(run.spec.env['WEBVIEW_OPTIONS']) == {
      'vnc': True,
      'allowed_origins': ['https://one.example', '*.trusted.example'],
      'blocked_origins': ['https://blocked.example'],
    }
    assert run.extension == worker.WebviewFacts(
      True,
      ('https://one.example', '*.trusted.example'),
      ('https://blocked.example',),
    )

  @pytest.mark.parametrize(
    ('args', 'message'),
    [
      ({'unknown': True}, 'unknown webview field'),
      ({'vnc': 1}, "'vnc' must be a boolean"),
      ({'allowed_origins': 'https://example.com'}, "'allowed_origins' must be a list"),
      ({'allowed_origins': ['']}, 'non-empty strings'),
      ({'blocked_origins': ['https://a;https://b']}, "without ';'"),
      ({'blocked_origins': [1]}, 'non-empty strings'),
    ],
  )
  def test_invalid_arguments_are_denied(self, tmp_path, args, message):
    assert message in _denial(tmp_path, args)

  def test_vnc_needs_its_leaf_permit_and_reports_what_is_held(self, tmp_path):
    message = _denial(
      tmp_path,
      {'vnc': True},
      permits=frozenset({'webview.other', 'bro.party.join'}),
    )

    assert ':webview.vnc' in message
    assert ':bro.party.join, :webview.other' in message

  def test_webview_owner_is_a_leaf(self, tmp_path):
    assert 'cannot open another webview' in _denial(tmp_path, {}, type=worker.WEBVIEW)

  def test_one_live_webview_per_owner_tracks_acceptance_and_end(self, tmp_path):
    worker_type = _worker_type()
    subscriber = worker_type.subscribers()[0]
    first = _request(tmp_path, {}, request_id='first')
    first_run = worker_type.launch(first)
    record = SimpleNamespace(parent='owner', mission_id='first')

    subscriber(SimpleNamespace(type=worker.WEBVIEW, transition='accepted'), record)
    with pytest.raises(LaunchDenied, match='live webview first'):
      worker_type.launch(_request(tmp_path, {}, request_id='second'))

    subscriber(SimpleNamespace(type=worker.WEBVIEW, transition='ended'), record)
    second_run = worker_type.launch(_request(tmp_path, {}, request_id='second'))
    assert second_run.spec == first_run.spec

  def test_unrelated_journal_events_do_not_change_live_ownership(self, tmp_path):
    worker_type = _worker_type()
    subscriber = worker_type.subscribers()[0]
    record = SimpleNamespace(parent='owner', mission_id='other')

    subscriber(SimpleNamespace(type='bro', transition='accepted'), record)
    subscriber(SimpleNamespace(type=worker.WEBVIEW, transition='started'), record)

    worker_type.launch(_request(tmp_path, {}))

  def test_audit_fields_are_json_facts(self):
    facts = worker.WebviewFacts(True, ('https://allowed',), ('https://blocked',))

    assert _worker_type().audit_fields(facts) == {
      'vnc': True,
      'allowed_origins': ['https://allowed'],
      'blocked_origins': ['https://blocked'],
    }

  def test_audit_fields_reject_an_unrelated_extension(self):
    with pytest.raises(TypeError, match='WebviewFacts'):
      _worker_type().audit_fields(object())

  def test_packaged_dockerfile_is_a_stable_worker_image_input(self, tmp_path):
    first = _worker_type().launch(_request(tmp_path, {})).spec
    second = _worker_type().launch(_request(tmp_path, {})).spec

    dockerfile = first.files['Dockerfile'].decode()
    assert dockerfile.splitlines()[:2] == ['ARG RUNTIME_IMAGE', 'FROM ${RUNTIME_IMAGE}']
    assert re.search(r'npm install -g @playwright/mcp@\d+\.\d+\.\d+', dockerfile) is not None
    assert first.image_hash('runtime:one') == second.image_hash('runtime:one')
    assert first.image_hash('runtime:one') != first.image_hash('runtime:two')


def test_worker_import_stays_dependency_light():
  script = """
import sys
import bro.webview.worker
loaded = sorted(name for name in sys.modules if name == 'mcp' or name.startswith('bro.broker'))
if loaded:
  raise SystemExit(','.join(loaded))
"""
  result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True)
  assert result.returncode == 0, result.stderr
