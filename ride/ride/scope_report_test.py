from pathlib import Path
from unittest.mock import patch

from bro.workspace.project import ProjectConfig
from bro.workspace.store import ScopedSecrets
from ride.scope_report import report_scope


def _run(
  capsys,
  *,
  selection,
  scoped,
  available=lambda name: True,
  bro=None,
  harness='claude',
  options=None,
):
  with (
    patch('ride.scope_report.project_root', return_value=Path('/repo')),
    patch(
      'ride.scope_report.project_config',
      return_value=ProjectConfig(default_bro='bro-dev', image_repository='bro/bro-dev'),
    ),
    patch('ride.scope_report.bind_project_credentials', return_value=selection),
    patch('ride.scope_report.scoped_secrets', return_value=scoped) as scope,
    patch('ride.scope_report.credentials.available', available),
  ):
    rc = report_scope(
      bro=bro, harness=harness, options=options if options is not None else {'raw': False}
    )
  return rc, capsys.readouterr().out, scope


class TestReportScope:
  def test_names_the_instance_each_selected_kind_reads(self, capsys):
    rc, out, _ = _run(
      capsys,
      selection={'brog': 'github'},
      scoped=ScopedSecrets({'brog', 'github'}, {'openai'}, True),
    )
    assert rc == 0
    assert 'project: /repo' in out
    assert 'bro:     bro-dev (claude-full)' in out
    assert 'brog+github (project)' in out
    # a kind the project doesn't select reads whatever the host registry binds
    assert 'github' in out
    assert 'optional:' in out and 'openai' in out

  def test_marks_a_selection_that_does_not_resolve(self, capsys):
    rc, out, _ = _run(
      capsys,
      selection={'brog': 'github'},
      scoped=ScopedSecrets({'brog'}, set(), True),
      available=lambda name: False,
    )
    assert rc == 0
    assert 'MISSING' in out

  def test_reads_the_kinds_own_entry_selection(self, capsys):
    _, out, _ = _run(capsys, selection={'brog': None}, scoped=ScopedSecrets({'brog'}, set(), True))
    assert 'brog (project)' in out

  def test_raw_scopes_the_raw_flavor_and_bro_overrides_the_default(self, capsys):
    _, out, scope = _run(
      capsys,
      selection={},
      scoped=ScopedSecrets({'trails'}, set(), True),
      bro='dev',
      options={'raw': True},
    )
    assert 'bro:     dev (claude-raw)' in out
    assert scope.call_args.args[0] == 'dev'

  def test_bro_harness_uses_the_native_scope_recipe(self, capsys):
    _, out, scope = _run(
      capsys,
      selection={},
      scoped=ScopedSecrets({'openai'}, {'trails'}, False),
      harness='bro',
      options={},
    )
    assert 'bro:     bro-dev (bro-run)' in out
    assert scope.call_args.args[1].name == 'bro-run'
