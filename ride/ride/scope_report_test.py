import json

import pytest

from bro.base import credentials, host_config
from bro.bro import BaseBro
from bro.native.echo_declaration import LLMSpec
from ride.scope_report import report_scope


class ReportBro(BaseBro):
  name = 'report-test'
  extra_secrets = ('github',)
  may_summon = ('reviewer',)
  llm_spec = LLMSpec()


@pytest.fixture
def report_environment(tmp_path, monkeypatch, register_test_bros):
  register_test_bros(ReportBro)
  (tmp_path / 'pyproject.toml').write_text('[tool.bro]\ndefault = "report-test"\n')
  store = tmp_path / 'store'
  material = store / credentials.MATERIAL_DIR
  material.mkdir(parents=True)
  monkeypatch.setattr(credentials, 'STORE_DIR', str(store))
  monkeypatch.setattr(host_config, 'HOST_CONFIG_FILE', str(tmp_path / 'host.json'))
  return tmp_path, store, material


def test_report_shows_descriptor_usable_give_only_and_presence(report_environment, capsys):
  repository, store, material = report_environment
  (material / 'github+reviewer.cred').write_text('token')
  (store / credentials.STORE_FILE).write_text(json.dumps({'defaults': ['github+reviewer']}))
  (repository / 'host.json').write_text(
    json.dumps(
      {
        'defaults': {
          'grant': [
            'webview.cred=cookies+absent to @reviewer',
          ]
        }
      }
    )
  )
  assert report_scope(repository, None, 'claude') == 0
  output = capsys.readouterr().out
  descriptor = json.loads(output.splitlines()[0].removeprefix('descriptor: '))
  assert descriptor['target'] == 'report-test'
  assert descriptor['cred.github'] == 'reviewer'
  assert 'summon_entries: @reviewer for *' in output
  assert 'permits: ' in output and 'bro.placement=boxed for *' in output
  assert 'may_give: webview.cred=cookies+absent to ' in output
  assert 'github+reviewer: PRESENT' in output
  assert 'claude_code+: MISSING' in output
  assert 'trails+: SKIPPED' in output
  assert 'authority instance cookies+absent: MISSING' in output


def test_report_checks_presence_without_loading_material(report_environment, capsys, monkeypatch):
  repository, _, material = report_environment
  (material / 'github.cred').write_bytes(b'\xff')
  monkeypatch.setattr(credentials.LocalSource, 'fetch', lambda *_: pytest.fail('material read'))
  assert report_scope(repository, 'report-test', 'claude') == 0
  assert 'github+: PRESENT' in capsys.readouterr().out


def test_explicit_pick_of_optional_kind_is_missing_not_skipped(report_environment, capsys):
  repository, _, _ = report_environment
  (repository / 'host.json').write_text(
    json.dumps(
      {
        'projects': {
          str(repository): {'creds': ['trails+absent']},
        }
      }
    )
  )
  assert report_scope(repository, 'report-test', 'claude') == 0
  assert 'trails+absent: MISSING' in capsys.readouterr().out


def test_authority_instance_presence_is_independent_of_kind_hold(report_environment, capsys):
  repository, _, material = report_environment
  (material / 'cookies+reviewer.cred').write_text('profile')
  (repository / 'host.json').write_text(
    json.dumps(
      {
        'defaults': {
          'grant': [
            'webview.cred=cookies+reviewer to @reviewer',
          ]
        }
      }
    )
  )
  assert report_scope(repository, 'report-test', 'claude') == 0
  assert 'authority instance cookies+reviewer: PRESENT' in capsys.readouterr().out


def test_detached_report_requires_an_explicit_target(capsys):
  assert report_scope(None, None, 'claude') == 1
  assert 'requires --bro when detached' in capsys.readouterr().out


def test_unknown_target_is_reported_as_an_existence_error(report_environment, capsys):
  repository, _, _ = report_environment
  assert report_scope(repository, 'uninstalled', 'claude') == 1
  assert "target 'uninstalled' is not installed" in capsys.readouterr().out


@pytest.mark.parametrize(
  'policy',
  [
    {'grant': [':launch.bro']},
    {'grant': ['unknown']},
    {'limit': ['@reviewer']},
  ],
)
def test_invalid_policy_is_reported_without_escaping(report_environment, capsys, policy):
  repository, _, _ = report_environment
  (repository / 'host.json').write_text(json.dumps({'defaults': policy}))
  assert report_scope(repository, 'report-test', 'claude') == 1
  assert 'cannot compute the scope:' in capsys.readouterr().out


def test_explicit_target_overrides_the_project_default(report_environment, capsys):
  repository, _, _ = report_environment
  (repository / 'pyproject.toml').write_text('[tool.bro]\ndefault = "uninstalled"\n')
  assert report_scope(repository, 'report-test', 'bro') == 0
  descriptor = json.loads(capsys.readouterr().out.splitlines()[0].removeprefix('descriptor: '))
  assert descriptor['target'] == 'report-test'
  assert descriptor['provider'] == 'echo'
