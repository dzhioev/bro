from pathlib import Path

import pytest

from bro.benchmark import cli, publication, retention


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


def test_publish_requires_visibility_and_prints_the_hub_and_record_urls(monkeypatch, capsys):
  captured = {}

  def publish(source, visibility):
    captured.update(source=source, visibility=visibility)
    return publication.Publication(
      visibility,
      'https://hub.harborframework.com/jobs/job-id',
      '2026-09-11T02:30:00Z',
      's3://runs/runs/2026-08-24/job-id/publications/record.json',
    )

  monkeypatch.setattr(cli.publication, 'publish_run', publish)

  assert cli.main(['benchmark', 'publish', 'runs/2026-08-24/job-id', '--public']) == 0
  assert captured == {'source': 'runs/2026-08-24/job-id', 'visibility': 'public'}
  assert capsys.readouterr().out == (
    'https://hub.harborframework.com/jobs/job-id\n'
    's3://runs/runs/2026-08-24/job-id/publications/record.json\n'
  )


def test_publish_refuses_an_implicit_visibility():
  with pytest.raises(SystemExit):
    cli.main(['benchmark', 'publish', 'runs/2026-08-24/job-id'])


def test_query_accepts_inline_sql_or_a_file_and_defaults_to_the_shell(monkeypatch):
  calls = []
  monkeypatch.setattr(cli.query, 'command', lambda sql, sql_file: calls.append((sql, sql_file)))

  assert cli.main(['benchmark', 'query', 'SELECT count(*) FROM runs']) == 0
  assert cli.main(['benchmark', 'query', '--file', 'report.sql']) == 0
  assert cli.main(['benchmark', 'query']) == 0

  assert calls == [
    ('SELECT count(*) FROM runs', None),
    (None, Path('report.sql')),
    (None, None),
  ]


def test_query_refuses_both_inline_sql_and_a_file():
  with pytest.raises(SystemExit):
    cli.main(['benchmark', 'query', 'SELECT * FROM runs', '--file', 'report.sql'])
