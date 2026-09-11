from pathlib import Path

from bro.benchmark import cli, retention


def test_bundle_is_a_subcommand(monkeypatch):
  captured = {}
  monkeypatch.setattr(
    cli.bundle,
    'command',
    lambda output: captured.update(output=output),
  )

  assert cli.main(['benchmark', 'bundle', '--output', 'built']) is None
  assert captured == {'output': 'built'}


def test_the_dotted_bundle_entrypoint_remains_callable(monkeypatch):
  captured = {}
  monkeypatch.setattr(
    cli.bundle,
    'command',
    lambda output: captured.update(output=output),
  )

  assert cli.bundle.main(['bro.benchmark.bundle', '--output', 'built']) is None
  assert captured == {'output': 'built'}


def test_retain_prints_the_flat_run_location(monkeypatch, capsys):
  job = Path('/tmp/job')
  monkeypatch.setattr(cli.retention, 'resolve_job', lambda source: job)
  monkeypatch.setattr(
    cli.retention,
    'retain_job',
    lambda path: retention.RetainedRun('runs', 'runs/2026-08-24/job-id'),
  )

  assert cli.main(['benchmark', 'retain', 'sha256:' + 'a' * 64]) == 0
  assert capsys.readouterr().out == 's3://runs/runs/2026-08-24/job-id/\n'
