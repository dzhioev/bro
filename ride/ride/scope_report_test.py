from pathlib import Path
from unittest.mock import patch

from bro.base.host_config import (
  PROJECT_PATH_BRO_LAYER,
  PROJECT_PATH_LAYER,
  PROJECT_URL_LAYER,
  CredentialSelection,
)
from bro.workspace.project import ProjectConfig
from ride.scope_report import report_scope
from ride.workspace.store import ScopedSecrets


def _run(
  capsys,
  *,
  selection,
  scoped,
  layers=None,
  available=lambda name: True,
  bro=None,
  harness='claude',
  options=None,
):
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
    patch('ride.scope_report.credentials.available', available),
  ):
    rc = report_scope(
      repo=Path('/repo'),
      bro=bro,
      harness=harness,
      options=options if options is not None else {'raw': False},
    )
  return rc, capsys.readouterr().out, scope


class TestReportScope:
  def test_names_the_instance_each_selected_kind_reads(self, capsys):
    rc, out, _ = _run(
      capsys,
      selection={'brog': 'github', 'github': 'reviewer'},
      layers={'brog': PROJECT_URL_LAYER, 'github': PROJECT_PATH_BRO_LAYER},
      scoped=ScopedSecrets({'brog', 'github'}, {'openai'}),
    )
    assert rc == 0
    assert 'repository: /repo' in out
    assert 'bro:        bro-dev (claude-full)' in out
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

  def test_marks_a_selection_that_does_not_resolve(self, capsys):
    rc, out, _ = _run(
      capsys,
      selection={'brog': 'github'},
      scoped=ScopedSecrets({'brog'}, set()),
      available=lambda name: False,
    )
    assert rc == 0
    assert 'MISSING' in out

  def test_reads_the_kinds_empty_instance(self, capsys):
    _, out, _ = _run(capsys, selection={'brog': ''}, scoped=ScopedSecrets({'brog'}, set()))
    assert f'brog ({PROJECT_PATH_LAYER})' in out

  def test_reports_a_kind_no_layer_selects(self, capsys):
    _, out, _ = _run(capsys, selection={}, scoped=ScopedSecrets({'brog'}, set()))
    assert 'brog' in out
    assert 'REFUSED' not in out

  def test_raw_scopes_the_raw_flavor_and_bro_overrides_the_default(self, capsys):
    _, out, scope = _run(
      capsys,
      selection={},
      scoped=ScopedSecrets({'trails'}, set()),
      bro='dev',
      options={'raw': True},
    )
    assert 'bro:        dev (claude-raw)' in out
    assert scope.call_args.args[0] == 'dev'

  def test_bro_harness_uses_the_native_scope_recipe(self, capsys):
    _, out, scope = _run(
      capsys,
      selection={},
      scoped=ScopedSecrets({'openai'}, {'trails'}),
      harness='bro',
      options={},
    )
    assert 'bro:        bro-dev (bro-run)' in out
    assert scope.call_args.args[1].name == 'bro-run'
