from pathlib import Path
from unittest.mock import patch

from bro.base.host_config import CredentialSelection, UnboundKinds
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
        dict.fromkeys(selection, 'project') if layers is None else layers,
        scoped.unbound,
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
      layers={'brog': 'project', 'github': 'project-bro'},
      scoped=ScopedSecrets({'brog', 'github'}, {'openai'}),
    )
    assert rc == 0
    assert 'repository: /repo' in out
    assert 'bro:        bro-dev (claude-full)' in out
    assert 'brog+github (project)' in out
    assert 'github+reviewer (project-bro)' in out
    assert 'optional:' in out and 'openai' in out

  def test_marks_a_selection_that_does_not_resolve(self, capsys):
    rc, out, _ = _run(
      capsys,
      selection={'brog': 'github'},
      scoped=ScopedSecrets({'brog'}, set()),
      available=lambda name: False,
    )
    assert rc == 0
    assert 'MISSING' in out

  def test_reads_the_kinds_own_entry_selection(self, capsys):
    _, out, _ = _run(capsys, selection={'brog': None}, scoped=ScopedSecrets({'brog'}, set()))
    assert 'brog (project)' in out

  def test_marks_an_unbound_project_kind_refused(self, capsys):
    _, out, _ = _run(
      capsys,
      selection={},
      scoped=ScopedSecrets({'brog'}, set(), unbound=UnboundKinds(frozenset({'brog'}))),
    )
    assert 'per project, unbound' in out
    assert 'REFUSED' in out

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
