import json
from pathlib import Path
from unittest.mock import patch

from bro.base import credentials
from bro.base.host_config import (
  PROJECT_PATH_BRO_LAYER,
  PROJECT_PATH_LAYER,
  PROJECT_URL_LAYER,
  CredentialSelection,
)
from bro.llm.llms.echo import LLMSpec as EchoLLMSpec
from bro.workspace.project import ProjectConfig
from ride.scope_report import report_scope
from ride.workspace.store import ScopedSecrets


def _run(
  capsys,
  *,
  selection,
  scoped,
  layers=None,
  present=None,
  bro=None,
  harness='claude',
):
  scoped = ScopedSecrets(scoped.required, scoped.optional, dict(selection))
  with (
    patch(
      'ride.scope_report.project_config',
      return_value=ProjectConfig(default_bro='bro-dev', image_repository='bro/bro-dev'),
    ),
    patch(
      'ride.scope_report.bind_launch_credentials',
      return_value=CredentialSelection(
        selection,
        dict.fromkeys(selection, PROJECT_PATH_LAYER) if layers is None else layers,
      ),
    ),
    patch('ride.scope_report.scoped_secrets', return_value=scoped) as scope,
    patch('ride.scope_report.configured_scope_layers', return_value=()),
    patch(
      'ride.scope_report.effective_launch',
      return_value={'bro': {'bros': frozenset({'reviewer'}), 'party': frozenset({'boxed'})}},
    ),
    patch(
      'ride.scope_report.credentials.Store.instance_names',
      return_value=frozenset(
        present
        if present is not None
        else {
          f'{name}+{selection[name]}' if selection.get(name) else name
          for name in scoped.required | scoped.optional
        }
      ),
    ),
  ):
    rc = report_scope(repo=Path('/repo'), bro=bro, harness=harness)
  return rc, capsys.readouterr().out, scope


class TestReportScope:
  def test_real_report_classifies_names_without_loading_them(self, capsys, monkeypatch, tmp_path):
    (tmp_path / 'pyproject.toml').write_text('[tool.bro]\ndefault = "bro-dev"\n')
    store = tmp_path / 'store'
    material = store / credentials.MATERIAL_DIR
    material.mkdir(parents=True)
    (material / 'github+reviewer.cred').write_text('github-token')
    config = tmp_path / 'bro.json'
    config.write_text(
      json.dumps(
        {
          'projects': {
            str(tmp_path): {
              'creds': ['github+reviewer', 'brog+missing', 'openai+work'],
            }
          }
        }
      )
    )
    monkeypatch.setattr(credentials, 'STORE_DIR', str(store))
    monkeypatch.setattr('bro.base.host_config.HOST_CONFIG_FILE', str(config))

    assert report_scope(repo=tmp_path, bro='bro-dev', harness='claude') == 0

    rows = {
      line.split()[0]: line
      for line in capsys.readouterr().out.splitlines()
      if line.startswith('  ')
    }
    assert 'github+reviewer' in rows['github']
    assert rows['github'].endswith('PRESENT')
    assert rows['brog'].endswith('MISSING')
    assert rows['claude_code'].endswith('MISSING')
    assert rows['openai'].endswith('MISSING')
    assert 'trails (unpicked)' in rows['trails']
    assert rows['trails'].endswith('SKIPPED')

  def test_a_malformed_project_config_is_reported_without_escaping(self, capsys, tmp_path):
    (tmp_path / 'pyproject.toml').write_text('[tool.bro]\nharness = "unknown"\n')

    assert report_scope(repo=tmp_path, bro='bro-dev', harness='claude') == 1
    assert 'cannot compute the scope:' in capsys.readouterr().out

  def test_names_the_instance_each_selected_kind_reads(self, capsys):
    rc, out, _ = _run(
      capsys,
      selection={'brog': 'github', 'github': 'reviewer'},
      layers={'brog': PROJECT_URL_LAYER, 'github': PROJECT_PATH_BRO_LAYER},
      scoped=ScopedSecrets({'brog', 'github'}, {'openai'}),
    )
    assert rc == 0
    assert 'repository: /repo' in out
    assert 'bro:        bro-dev (claude)' in out
    assert 'launch:     :launch.bro.party.boxed, @reviewer' in out
    assert f'brog+github ({PROJECT_URL_LAYER})' in out
    assert f'github+reviewer ({PROJECT_PATH_BRO_LAYER})' in out
    assert 'optional:' in out and 'openai' in out

  def test_the_state_column_clears_the_widest_selection(self, capsys):
    _, out, _ = _run(
      capsys,
      selection={'github': 'reviewer'},
      layers={'github': PROJECT_PATH_BRO_LAYER},
      scoped=ScopedSecrets({'github'}, {'openai'}),
    )

    rows = [line for line in out.splitlines() if line.startswith('  ')]
    assert len(rows) == 2
    columns = set()
    for row in rows:
      state = row.split()[-1]
      assert row.endswith(f'  {state}')
      columns.add(row.rindex(state))
    assert len(columns) == 1

  def test_reports_presence_skip_and_failure_by_stored_name(self, capsys):
    rc, out, _ = _run(
      capsys,
      selection={'brog': 'github', 'openai': 'work'},
      scoped=ScopedSecrets({'brog', 'github'}, {'openai', 'trails'}),
      present={'brog+github'},
    )
    assert rc == 0
    rows = {line.split()[0]: line for line in out.splitlines() if line.startswith('  ')}
    assert rows['brog'].endswith('PRESENT')
    assert rows['github'].endswith('MISSING')
    assert rows['openai'].endswith('MISSING')
    assert rows['trails'].endswith('SKIPPED')

  def test_reads_the_kinds_empty_instance(self, capsys):
    _, out, _ = _run(capsys, selection={'brog': ''}, scoped=ScopedSecrets({'brog'}, set()))
    assert f'brog ({PROJECT_PATH_LAYER})' in out

  def test_reports_a_kind_no_layer_selects(self, capsys):
    _, out, _ = _run(capsys, selection={}, scoped=ScopedSecrets({'brog'}, set()))
    assert 'brog' in out
    assert 'REFUSED' not in out

  def test_an_explicit_bro_overrides_the_default(self, capsys):
    _, out, scope = _run(capsys, selection={}, scoped=ScopedSecrets({'trails'}, set()), bro='dev')
    assert 'bro:        dev (claude)' in out
    assert scope.call_args.args[0] == 'dev'

  def test_the_scope_follows_the_hosts_per_bro_recipe(self, capsys, monkeypatch, tmp_path):
    config = tmp_path / 'bro.json'
    config.write_text(json.dumps({'projects': {'/repo': {'bros': {'bro-dev': {'llm': 'echo'}}}}}))
    monkeypatch.setattr('bro.base.host_config.HOST_CONFIG_FILE', str(config))

    _, _, scope = _run(capsys, selection={}, scoped=ScopedSecrets(set(), set()), harness='bro')

    assert scope.call_args.kwargs['llm_spec'] == EchoLLMSpec()

  def test_an_unknown_bro_is_reported_under_the_bro_harness(self, capsys):
    rc, out, _ = _run(
      capsys,
      selection={},
      scoped=ScopedSecrets(set(), set()),
      bro='no-such-bro',
      harness='bro',
    )
    assert rc == 1
    assert "cannot compute the scope: unknown bro 'no-such-bro'" in out

  def test_bro_harness_uses_the_native_scope_recipe(self, capsys):
    _, out, scope = _run(
      capsys,
      selection={},
      scoped=ScopedSecrets({'openai'}, {'trails'}),
      harness='bro',
    )
    assert 'bro:        bro-dev (bro-run)' in out
    assert scope.call_args.args[1].name == 'bro-run'
